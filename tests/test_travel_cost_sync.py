"""Travel cost sync: services/travel_integration_service.py, services/travel_cost_sync_job.py and
/api/integrations/travel/sync-costs + sync-status. The travel system is replaced by an httpx.MockTransport."""
import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx
import jwt
import pytest

import postgresql_db.database as database
import services.travel_cost_sync_job as job
import services.travel_integration_service as travel
from services.auth_service import ALLOWED_JWT_ISSUERS, JWT_ALGORITHM, JWT_SECRET
from tests.conftest import DATABASE_URL, run_sql

ALREADY_RUNNING = "A travel sync is already running. Try again in a minute."

SERVER_TIME_1 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SERVER_TIME_2 = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)


class FakeTravel:
    """Stands in for the travel system: answers with the costs set on it and remembers each request."""

    def __init__(self):
        self.costs: list[dict] = []
        self.server_time = SERVER_TIME_1
        self.status_code = 200
        self.requests: list[httpx.Request] = []
        # Called on each request before answering, e.g. to look at the database pool mid-fetch
        self.on_request = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request:
            self.on_request(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"detail": "boom"})
        return httpx.Response(200, json={"costs": self.costs, "server_time": self.server_time.isoformat()})


@pytest.fixture
def fake_travel(monkeypatch):
    fake = FakeTravel()
    monkeypatch.setenv("TRAVEL_BACKEND_URL", "http://travel.test")
    monkeypatch.delenv("TRAVEL_COST_SYNC_ENABLED", raising=False)
    monkeypatch.setattr(travel, "_transport", httpx.MockTransport(fake.handler))
    travel.reset_travel_integration_state()
    yield fake
    travel.reset_travel_integration_state()


# --- Service: token, headers and circuit breaker (no database needed) ---


def test_token_and_headers(fake_travel):
    response = asyncio.run(travel.fetch_travel_costs(SERVER_TIME_1, event_code="EVT-2026-0003", correlation_id="corr-1"))
    asyncio.run(travel.fetch_travel_costs(None))
    assert response.server_time == SERVER_TIME_1

    first, second = fake_travel.requests
    assert first.url.path == "/api/travel/integrations/marketing/costs"
    assert first.url.params["updated_since"] == "2026-10-15T12:00:00Z"
    assert first.url.params["event_code"] == "EVT-2026-0003"
    assert "updated_since" not in second.url.params
    assert first.headers["X-Source-Service"] == "marketing-portal"
    assert first.headers["X-Correlation-ID"] == "corr-1"
    assert second.headers["X-Correlation-ID"]
    assert uuid.UUID(first.headers["X-Request-ID"]) != uuid.UUID(second.headers["X-Request-ID"])

    scheme, token = first.headers["Authorization"].split(" ", 1)
    assert scheme == "Bearer"
    claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM], issuer=ALLOWED_JWT_ISSUERS)
    assert claims["service"] == "marketing"
    assert claims["target_service"] == "travel-management"
    assert claims["scopes"] == ["travel:costs_read"]
    assert claims["is_service_token"] is True
    assert "is_super_admin" not in claims


def test_not_configured(fake_travel, monkeypatch):
    monkeypatch.delenv("TRAVEL_BACKEND_URL")
    with pytest.raises(travel.TravelUnavailable, match="not set up"):
        asyncio.run(travel.fetch_travel_costs(None))
    assert fake_travel.requests == []


def test_circuit_opens_after_five_failures_and_recovers(fake_travel, monkeypatch):
    fake_travel.status_code = 500
    for _ in range(5):
        with pytest.raises(travel.TravelUnavailable):
            asyncio.run(travel.fetch_travel_costs(None))
    assert travel.travel_status()["state"] == "OPEN"
    assert travel.travel_status()["last_error"] == "The travel system returned HTTP 500"

    # While open, calls fail fast without reaching the travel system
    with pytest.raises(travel.TravelUnavailable, match="not responding"):
        asyncio.run(travel.fetch_travel_costs(None))
    assert len(fake_travel.requests) == 5

    # After the recovery time one probe goes through; success closes the circuit
    monkeypatch.setattr(travel.travel_circuit_breaker, "recovery_time_seconds", 0)
    assert travel.travel_status()["state"] == "HALF_OPEN"
    fake_travel.status_code = 200
    asyncio.run(travel.fetch_travel_costs(None))
    status = travel.travel_status()
    assert (status["state"], status["last_error"]) == ("CLOSED", None)
    assert status["last_success_at"] is not None


def test_last_known_good_cache_for_reads(fake_travel):
    fake_travel.costs = [{"id": "x"}]
    asyncio.run(travel.fetch_travel_costs(None, allow_cached=True))
    fake_travel.status_code = 503
    cached = asyncio.run(travel.fetch_travel_costs(None, allow_cached=True))
    assert cached.costs == [{"id": "x"}]
    # The sync never takes a cached answer
    with pytest.raises(travel.TravelUnavailable):
        asyncio.run(travel.fetch_travel_costs(None))


def test_bad_response_is_reported(fake_travel, monkeypatch):
    monkeypatch.setattr(travel, "_transport", httpx.MockTransport(lambda request: httpx.Response(200, json={"costs": "nope"})))
    with pytest.raises(travel.TravelResponseError, match="unexpected format"):
        asyncio.run(travel.fetch_travel_costs(None))


def test_recovery_listener_keeps_one_pending_and_cancels(fake_travel, monkeypatch):
    started = []

    async def slow_refresh():
        started.append(1)
        await asyncio.sleep(3600)

    monkeypatch.setattr(travel, "_refresh_on_recovery", slow_refresh)
    breaker = travel.travel_circuit_breaker

    def close_circuit(from_state):
        breaker.state = from_state
        breaker.record_success()

    async def scenario():
        # Two recoveries in a row: only one catch-up is queued
        close_circuit("OPEN")
        close_circuit("HALF_OPEN")
        assert len(travel._recovery_tasks) == 1
        task = next(iter(travel._recovery_tasks))
        await asyncio.sleep(0)
        assert started == [1]

        await travel.cancel_recovery_tasks()
        assert task.cancelled()
        assert not travel._recovery_tasks
        await travel.cancel_recovery_tasks()  # nothing pending is fine

        # While a sync runs, its own fetch closing the circuit doesn't queue a duplicate
        lock = asyncio.Lock()
        monkeypatch.setattr(job, "_run_lock", lock)
        async with lock:
            close_circuit("HALF_OPEN")
            assert not travel._recovery_tasks
        assert started == [1]

    asyncio.run(scenario())


# --- Sync through the API (needs the local database) ---


@pytest.fixture
def api(client, fake_travel):
    """Keeps the stored sync state as it was before the test."""
    saved = run_sql("SELECT * FROM integration_sync_state WHERE name = 'travel_costs'")
    run_sql("DELETE FROM integration_sync_state WHERE name = 'travel_costs'")
    ref_prefix = f"PYTEST-{uuid.uuid4().hex[:8]}-"
    try:
        yield {"travel": fake_travel, "prefix": ref_prefix}
    finally:
        run_sql(
            "DELETE FROM audit_logs WHERE entity_type = 'SpendLine' AND actor_user_id IS NULL "
            "AND COALESCE(new_value, old_value)->>'external_ref' LIKE $1",
            ref_prefix + "%",
        )
        run_sql("DELETE FROM spend_lines WHERE source = 'TRAVEL_FEED' AND external_ref LIKE $1", ref_prefix + "%")
        run_sql("DELETE FROM integration_sync_state WHERE name = 'travel_costs'")
        for row in saved:
            run_sql(
                "INSERT INTO integration_sync_state (name, cursor, last_attempt_at, last_success_at, last_error, "
                "last_run_summary) VALUES ($1, $2, $3, $4, $5, $6)",
                row["name"], row["cursor"], row["last_attempt_at"], row["last_success_at"], row["last_error"],
                row["last_run_summary"],
            )


@pytest.fixture
def event(client, admin, ref):
    body = {
        "name": f"Pytest Travel Expo {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "currency": "USD",
        "event": {"city": "Toronto", "country_code": "CA"},
    }
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def initiative_viewer(viewer):
    """The temporary regular user with only "View Initiatives" (role removed with the user)."""
    code = f"PYTEST_INITIATIVE_VIEWER_{uuid.uuid4().hex[:8].upper()}"
    role_id = run_sql("INSERT INTO roles (code, name, is_active) VALUES ($1, 'Pytest Initiative Viewer', true) RETURNING id", code)[0]["id"]
    run_sql(
        "INSERT INTO role_permission_groups (role_id, permission_group_id) "
        "SELECT $1, id FROM permission_groups WHERE code = 'MARKETING_VIEW_INITIATIVES'",
        role_id,
    )
    run_sql("INSERT INTO user_roles (user_id, role_id, is_active) VALUES ($1, $2, true)", viewer["id"], role_id)
    return viewer


def _cost(api, event, suffix, **fields):
    return {
        "id": api["prefix"] + suffix,
        "event_code": event["event_code"],
        "assignment_id": None,
        "category": "FLIGHT",
        "description": None,
        "vendor": "Air Canada",
        "amount": 812.40,
        "currency": "CAD",
        "incurred_on": "2026-11-02",
        "status": "APPROVED",
        "paid_on": None,
        "updated_at": "2026-10-15T10:00:00Z",
        **fields,
    }


def _sync(client, admin, **params):
    return client.post("/api/integrations/travel/sync-costs", params=params, headers=admin["headers"])


def _lines(api):
    rows = run_sql(
        """
        SELECT s.id, s.external_ref, s.initiative_id, sc.code AS category, s.description, s.vendor, s.status,
               s.amount::float8 AS amount, s.currency, s.incurred_on, s.paid_on, s.source, s.created_by
        FROM spend_lines s JOIN spend_categories sc ON sc.id = s.category_id
        WHERE s.source = 'TRAVEL_FEED' AND s.external_ref LIKE $1
        """,
        api["prefix"] + "%",
    )
    return {r["external_ref"].removeprefix(api["prefix"]): dict(r) for r in rows}


def _audit_actions(api):
    rows = run_sql(
        "SELECT action, actor_user_id FROM audit_logs WHERE entity_type = 'SpendLine' "
        "AND COALESCE(new_value, old_value)->>'external_ref' LIKE $1 ORDER BY created_at",
        api["prefix"] + "%",
    )
    assert all(r["actor_user_id"] is None for r in rows)
    return [r["action"] for r in rows]


def _state():
    rows = run_sql("SELECT * FROM integration_sync_state WHERE name = 'travel_costs'")
    return dict(rows[0]) if rows else None


def test_create_update_remove_and_idempotent(client, admin, api, event):
    fake = api["travel"]
    fake.costs = [
        _cost(api, event, "flight"),
        _cost(api, event, "hotel", category="LODGING", vendor="Fairmont", description="Hotel, 3 nights", amount=1250,
              currency="USD", status="PAID", paid_on="2026-11-10"),
        _cost(api, event, "taxi", category="GROUND_TRANSPORT", vendor=None, amount=45),
        _cost(api, event, "dinner", category="MEALS", status="REJECTED"),
    ]
    response = _sync(client, admin)
    assert response.status_code == 200, response.text
    assert response.json() == {"fetched": 4, "created": 3, "updated": 0, "removed": 0, "skipped_unknown_event": 0,
                               "conflicts": 0, "invalid": 0, "skipped": []}

    lines = _lines(api)
    assert set(lines) == {"flight", "hotel", "taxi"}
    flight, hotel, taxi = lines["flight"], lines["hotel"], lines["taxi"]
    assert str(flight["initiative_id"]) == event["id"]
    assert (flight["category"], flight["status"], flight["amount"], flight["currency"]) == ("FLIGHTS", "INVOICED", 812.4, "CAD")
    assert (flight["description"], flight["incurred_on"], flight["created_by"]) == ("Flight · Air Canada", date(2026, 11, 2), None)
    assert (hotel["category"], hotel["status"], hotel["paid_on"], hotel["currency"]) == ("HOTELS", "PAID", date(2026, 11, 10), "USD")
    assert hotel["description"] == "Hotel, 3 nights"
    assert (taxi["category"], taxi["description"]) == ("GROUND_TRANSPORT", "Ground transport")
    assert _audit_actions(api) == ["CREATE"] * 3

    # The same answer again changes nothing
    again = _sync(client, admin).json()
    assert (again["created"], again["updated"], again["removed"]) == (0, 0, 0)
    assert _audit_actions(api) == ["CREATE"] * 3

    # Flight paid, taxi cancelled
    fake.costs = [
        _cost(api, event, "flight", status="PAID", paid_on="2026-11-20", amount=799.99),
        _cost(api, event, "taxi", category="GROUND_TRANSPORT", vendor=None, amount=45, status="CANCELLED"),
    ]
    summary = _sync(client, admin).json()
    assert (summary["created"], summary["updated"], summary["removed"]) == (0, 1, 1)
    lines = _lines(api)
    assert set(lines) == {"flight", "hotel"}
    assert (lines["flight"]["status"], lines["flight"]["paid_on"], lines["flight"]["amount"]) == ("PAID", date(2026, 11, 20), 799.99)
    actions = _audit_actions(api)
    assert actions[:3] == ["CREATE"] * 3
    assert sorted(actions[3:]) == ["DELETE", "UPDATE"]

    # Cancelled again, and the same update again: still nothing to do
    summary = _sync(client, admin).json()
    assert (summary["created"], summary["updated"], summary["removed"]) == (0, 0, 0)
    assert len(_audit_actions(api)) == 5


def test_skipped_costs_are_listed_and_fetched_again(client, admin, api, event):
    fake = api["travel"]
    ghost = _cost(api, event, "ghost", event_code="EVT-1999-9999", updated_at="2026-10-15T09:00:00Z")
    broken = {"id": api["prefix"] + "broken", "amount": "lots", "updated_at": "2026-10-15T11:00:00Z"}
    fake.costs = [ghost, _cost(api, event, "real"), broken]
    summary = _sync(client, admin).json()
    assert (summary["fetched"], summary["created"], summary["skipped_unknown_event"], summary["invalid"]) == (3, 1, 1, 1)
    assert set(_lines(api)) == {"real"}
    reasons = {s["id"].removeprefix(api["prefix"]): s["reason"] for s in summary["skipped"]}
    assert reasons["ghost"] == "unknown event code EVT-1999-9999"
    assert reasons["broken"].startswith("does not match the contract")

    # The cursor stops just before the earliest skipped cost, not at the server time
    state = _state()
    assert state["cursor"] == datetime(2026, 10, 15, 8, 59, 59, tzinfo=timezone.utc)
    assert json.loads(state["last_run_summary"])["skipped"] == summary["skipped"]

    # So the next run asks for it again; once the event code is right it is applied
    fake.server_time = SERVER_TIME_2
    fake.costs = [{**ghost, "event_code": event["event_code"]}]
    summary = _sync(client, admin).json()
    assert fake.requests[-1].url.params["updated_since"] == "2026-10-15T08:54:59Z"
    assert (summary["created"], summary["skipped"]) == (1, [])
    assert set(_lines(api)) == {"real", "ghost"}
    assert _state()["cursor"] == SERVER_TIME_2


def test_invalid_cost_without_updated_at_holds_the_cursor(client, admin, api, event):
    fake = api["travel"]
    fake.costs = [_cost(api, event, "real")]
    _sync(client, admin)
    assert _state()["cursor"] == SERVER_TIME_1

    fake.server_time = SERVER_TIME_2
    fake.costs = [_cost(api, event, "real"), {"id": api["prefix"] + "broken", "updated_at": "not a date"}]
    summary = _sync(client, admin).json()
    assert summary["invalid"] == 1
    assert [s["id"] for s in summary["skipped"]] == [api["prefix"] + "broken"]
    state = _state()
    assert (state["cursor"], state["last_error"]) == (SERVER_TIME_1, None)


def test_sync_already_running_in_this_process_returns_409(client, admin, api, event, monkeypatch):
    lock = asyncio.Lock()
    asyncio.run(lock.acquire())
    monkeypatch.setattr(job, "_run_lock", lock)
    api["travel"].costs = [_cost(api, event, "flight")]

    response = _sync(client, admin)
    assert (response.status_code, response.json()["detail"]) == (409, ALREADY_RUNNING)
    assert api["travel"].requests == []
    assert _state() is None

    lock.release()
    assert _sync(client, admin).status_code == 200
    assert set(_lines(api)) == {"flight"}


def test_sync_applying_on_another_server_returns_409(client, admin, api, event):
    api["travel"].costs = [_cost(api, event, "flight")]
    # Another server holds the apply lock
    loop = asyncio.new_event_loop()
    other = loop.run_until_complete(asyncpg.connect(DATABASE_URL, timeout=5))
    try:
        loop.run_until_complete(other.execute("SELECT pg_advisory_lock(hashtext($1))", job._SYNC_LOCK_KEY))
        response = _sync(client, admin)
        assert (response.status_code, response.json()["detail"]) == (409, ALREADY_RUNNING)
        assert len(api["travel"].requests) == 1
        assert _lines(api) == {}
        assert _state() is None
    finally:
        loop.run_until_complete(other.close())
        loop.close()

    assert _sync(client, admin).status_code == 200
    assert set(_lines(api)) == {"flight"}


def test_no_database_connection_is_held_while_asking_travel(client, admin, api, event):
    checked_out = []

    def count_checked_out(request):
        pool = database.get_pool()
        checked_out.append(pool.get_size() - pool.get_idle_size())

    api["travel"].on_request = count_checked_out
    api["travel"].costs = [_cost(api, event, "flight")]
    _sync(client, admin)  # first run: no cursor yet
    _sync(client, admin)  # cursor read first, then released
    assert checked_out == [0, 0]
    assert set(_lines(api)) == {"flight"}


def test_recovery_during_a_sync_does_not_start_another(client, admin, api, event, monkeypatch):
    monkeypatch.setenv("TRAVEL_COST_SYNC_ENABLED", "true")
    calls = []

    async def counting_refresh():
        calls.append(1)

    monkeypatch.setattr(travel, "_refresh_on_recovery", counting_refresh)
    fake = api["travel"]
    fake.status_code = 500
    for _ in range(5):
        assert _sync(client, admin).status_code == 503
    assert travel.travel_status()["state"] == "OPEN"

    # The sync's own fetch is the probe that closes the circuit: no extra catch-up sync is queued
    monkeypatch.setattr(travel.travel_circuit_breaker, "recovery_time_seconds", 0)
    fake.status_code = 200
    fake.costs = [_cost(api, event, "flight")]
    assert _sync(client, admin).status_code == 200
    assert travel.travel_status()["state"] == "CLOSED"
    assert calls == []
    assert not travel._recovery_tasks


def test_reconciled_lines_are_left_alone(client, admin, api, event):
    fake = api["travel"]
    fake.costs = [_cost(api, event, "flight"), _cost(api, event, "hotel", category="LODGING")]
    _sync(client, admin)
    run_sql("UPDATE spend_lines SET reconciled_month = '2026-11-01' WHERE external_ref LIKE $1", api["prefix"] + "%")

    # Cancelling a reconciled cost and changing a reconciled amount are conflicts; paying it is allowed
    fake.costs = [
        _cost(api, event, "flight", status="CANCELLED"),
        _cost(api, event, "hotel", category="LODGING", amount=999, status="PAID", paid_on="2026-11-30"),
    ]
    summary = _sync(client, admin).json()
    assert (summary["removed"], summary["updated"], summary["conflicts"]) == (0, 0, 2)
    lines = _lines(api)
    assert set(lines) == {"flight", "hotel"}
    assert (lines["hotel"]["amount"], lines["hotel"]["status"]) == (812.4, "INVOICED")

    fake.costs = [_cost(api, event, "hotel", category="LODGING", status="PAID", paid_on="2026-11-30")]
    summary = _sync(client, admin).json()
    assert (summary["updated"], summary["conflicts"]) == (1, 0)
    assert (_lines(api)["hotel"]["status"], _lines(api)["hotel"]["paid_on"]) == ("PAID", date(2026, 11, 30))


def test_cursor_moves_only_after_a_successful_run(client, admin, api, event):
    fake = api["travel"]
    fake.costs = [_cost(api, event, "flight")]
    _sync(client, admin)
    assert "updated_since" not in fake.requests[-1].url.params
    state = _state()
    assert state["cursor"] == SERVER_TIME_1
    assert json.loads(state["last_run_summary"])["created"] == 1

    # The next run asks for changes since the cursor, minus 5 minutes
    fake.server_time = SERVER_TIME_2
    _sync(client, admin)
    assert fake.requests[-1].url.params["updated_since"] == "2026-10-15T11:55:00Z"
    assert _state()["cursor"] == SERVER_TIME_2

    # A failed run keeps the cursor and records the error
    fake.status_code = 502
    response = _sync(client, admin)
    assert response.status_code == 503
    state = _state()
    assert state["cursor"] == SERVER_TIME_2
    assert state["last_error"] == "The travel system had a problem. Try again in a few minutes."
    assert state["last_attempt_at"] > state["last_success_at"]

    # A one-event run fetches that event's full history, applies it and leaves the stored state untouched
    fake.status_code = 200
    fake.server_time = SERVER_TIME_2 + timedelta(hours=6)
    fake.costs = [_cost(api, event, "flight"), _cost(api, event, "hotel", category="LODGING")]
    before = _state()
    response = _sync(client, admin, event_code=event["event_code"].lower())
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    params = fake.requests[-1].url.params
    assert (params["event_code"], "updated_since" in params) == (event["event_code"], False)
    assert _state() == before
    assert set(_lines(api)) == {"flight", "hotel"}

    # A failed one-event run doesn't record its error either
    fake.status_code = 502
    assert _sync(client, admin, event_code=event["event_code"]).status_code == 503
    assert _state() == before


def test_circuit_breaker_makes_the_sync_unavailable(client, admin, api, event):
    fake = api["travel"]
    fake.status_code = 500
    for _ in range(5):
        assert _sync(client, admin).status_code == 503
    response = _sync(client, admin)
    assert (response.status_code, response.json()["detail"]) == (503, "The travel system is not responding. Try again in a few minutes.")
    assert len(fake.requests) == 5

    status = client.get("/api/integrations/travel/sync-status", headers=admin["headers"]).json()
    assert status["circuit_state"] == "OPEN"
    assert status["configured"] is True
    assert status["enabled"] is False
    assert status["last_error"] == "The travel system is not responding. Try again in a few minutes."


def test_not_configured_returns_503(client, admin, api, monkeypatch):
    monkeypatch.delenv("TRAVEL_BACKEND_URL")
    response = _sync(client, admin)
    assert (response.status_code, response.json()["detail"]) == (
        503, "The travel system is not set up. Ask an administrator to set TRAVEL_BACKEND_URL.")
    status = client.get("/api/integrations/travel/sync-status", headers=admin["headers"]).json()
    assert status["configured"] is False


def test_status_after_a_run(client, admin, api, event, monkeypatch):
    monkeypatch.setenv("TRAVEL_COST_SYNC_ENABLED", "true")
    api["travel"].costs = [_cost(api, event, "flight")]
    assert _sync(client, admin).status_code == 200
    status = client.get("/api/integrations/travel/sync-status", headers=admin["headers"]).json()
    assert status["enabled"] is True
    assert status["circuit_state"] == "CLOSED"
    assert status["last_error"] is None
    assert status["last_success_at"] is not None
    assert status["last_run_summary"]["created"] == 1


def test_permissions(client, api, viewer, initiative_viewer):
    # initiative_viewer is the same temporary user as viewer, now with "View Initiatives" only
    assert client.get("/api/integrations/travel/sync-status", headers=initiative_viewer["headers"]).status_code == 200
    assert _sync(client, initiative_viewer).status_code == 403
    assert api["travel"].requests == []


def test_permissions_without_any(client, api, viewer):
    assert client.get("/api/integrations/travel/sync-status", headers=viewer["headers"]).status_code == 403
    assert _sync(client, viewer).status_code == 403
    assert client.get("/api/integrations/travel/sync-status").status_code == 401
    assert client.post("/api/integrations/travel/sync-costs").status_code == 401
