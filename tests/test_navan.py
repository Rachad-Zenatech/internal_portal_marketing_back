"""Navan travel totals: adding up bookings per Navan event, linking them to our events, and the sync.

Navan itself is never called: the bookings are made up and passed in place of fetch_navan_bookings. Any real Navan
costs in the database are put back after each test.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from services import navan_cost_sync_job
from services.navan_cost_sync_job import link_events
from services.navan_service import summarize_events
from tests.conftest import run_sql


@pytest.fixture(autouse=True)
def _keep_real_navan_costs():
    """The sync owns every NAVAN line, so a test run would remove real ones: keep them aside and put them back."""
    run_sql("DROP TABLE IF EXISTS pytest_navan_lines")
    run_sql("CREATE TABLE pytest_navan_lines AS SELECT * FROM spend_lines WHERE source = 'NAVAN'")
    run_sql("DELETE FROM spend_lines WHERE source = 'NAVAN'")  # each test starts with none
    run_sql("DROP TABLE IF EXISTS pytest_navan_state")
    run_sql("CREATE TABLE pytest_navan_state AS SELECT * FROM integration_sync_state WHERE name = 'navan_costs'")
    started = run_sql("SELECT now() AS t")[0]["t"]
    yield
    # The sync's own audit entries (no actor; only the sync writes those during these tests), so local audit
    # logs don't fill up with test entries
    run_sql(
        "DELETE FROM audit_logs WHERE entity_type IN ('SpendLine', 'Initiative') AND actor_user_id IS NULL "
        "AND created_at >= $1",
        started,
    )
    # Events the sync added for made-up Navan events (nobody is recorded as creating them)
    run_sql(
        "DELETE FROM initiatives WHERE created_by IS NULL AND id IN "
        "(SELECT initiative_id FROM event_details WHERE navan_event_name LIKE 'Pytest Navan%')"
    )
    run_sql("DELETE FROM spend_lines WHERE source = 'NAVAN'")
    run_sql("INSERT INTO spend_lines SELECT * FROM pytest_navan_lines")
    run_sql("DELETE FROM integration_sync_state WHERE name = 'navan_costs'")
    run_sql("INSERT INTO integration_sync_state SELECT * FROM pytest_navan_state")
    run_sql("DROP TABLE pytest_navan_lines")
    run_sql("DROP TABLE pytest_navan_state")


def _booking(event_name, total, *, status="TICKETED", currency="USD", start="2090-03-10", kind="FLIGHT"):
    return {"bookingId": uuid.uuid4().hex, "eventName": event_name, "grandTotal": total, "currency": currency,
            "bookingStatus": status, "startDate": f"{start}T09:00:00", "bookingType": kind}


def _event(client, admin, ref, name=None, navan_event_name=None, expect=201):
    start = date.today() + timedelta(days=30)
    event = {"city": "Phoenix", "country_code": "US"}
    if navan_event_name is not None:
        event["navan_event_name"] = navan_event_name
    response = client.post("/api/initiatives", json={
        "name": name or f"Pytest Navan Event {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
        "budget_amount": 20000,
        "currency": "USD",
        "status": "PLANNED",
        "event": event,
    }, headers=admin["headers"])
    assert response.status_code == expect, response.text
    return response.json()


def _sync(client, admin, monkeypatch, bookings):
    async def fake_fetch():
        return bookings

    monkeypatch.setattr(navan_cost_sync_job, "fetch_navan_bookings", fake_fetch)
    response = client.post("/api/integrations/navan/sync-costs", headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def _navan_lines(initiative_id):
    return run_sql(
        "SELECT amount, currency, status, description, vendor, incurred_on, external_ref FROM spend_lines "
        "WHERE source = 'NAVAN' AND initiative_id = $1 ORDER BY currency",
        uuid.UUID(initiative_id),
    )


# --- Adding up and linking (no database) ---


def test_summarize_events():
    events = summarize_events([
        {**_booking("DaaS Grand Opening", 5365.90, start="2090-10-20"),
         "destination": {"city": "Phoenix", "country": "United States"}},
        {**_booking("DaaS Grand Opening", 1693.17, kind="HOTEL", start="2090-10-21"), "endDate": "2090-10-22",
         "destination": {"city": "Mesa", "country": "United States"}},
        _booking("DaaS Grand Opening", 378.45, kind="CAR", status="CONFIRMED", start="2090-10-19"),
        _booking("DaaS Grand Opening", 900, status="VOIDED"),
        _booking("daas grand opening", 50, currency="CAD"),
        _booking(None, 300),  # ordinary business travel: no event
        _booking("All Cancelled", 400, status="CANCELED"),
    ])

    assert set(events) == {"daas grand opening", "all cancelled"}
    daas = events["daas grand opening"]
    assert daas["name"] == "DaaS Grand Opening"
    assert daas["bookings"] == 4
    assert daas["totals"] == {"USD": Decimal("7437.52"), "CAD": Decimal("50")}
    assert daas["first_travel"] == date(2090, 3, 10)  # the CAD booking's default start
    assert daas["last_travel"] == date(2090, 10, 22)  # the hotel's check-out
    assert (daas["city"], daas["country_code"]) == ("Mesa", "US")  # where the hotel is, over where flights land
    assert events["all cancelled"]["totals"] == {}


def test_link_events():
    navan = {"nexuscon": {}, "daas grand opening": {}, "myrtle beach": {}, "twin": {}}
    ours = [
        {"id": 1, "name": "NexusCon", "navan_event_name": None},
        {"id": 2, "name": "DaaS 2026", "navan_event_name": "DaaS Grand Opening"},
        {"id": 3, "name": "DaaS Grand Opening", "navan_event_name": None},  # the name is claimed by event 2
        {"id": 4, "name": "Twin", "navan_event_name": None},
        {"id": 5, "name": "twin", "navan_event_name": None},  # two events with the name: neither is guessed
    ]

    links = link_events(navan, ours)

    assert links["nexuscon"] == {"initiative_id": 1, "initiative_name": "NexusCon", "link": "NAME_MATCH"}
    assert links["daas grand opening"] == {"initiative_id": 2, "initiative_name": "DaaS 2026", "link": "SET"}
    assert "myrtle beach" not in links and "twin" not in links


# --- The sync ---


def test_sync_keeps_one_total_per_event(client, admin, ref, monkeypatch):
    monkeypatch.setenv("NAVAN_CREATE_EVENTS", "false")  # adding events is tested on its own below
    by_name = _event(client, admin, ref)
    navan_name = f"Pytest Navan Offsite {uuid.uuid4().hex[:6]}"
    by_link = _event(client, admin, ref, navan_event_name=navan_name)
    unlinked = f"Pytest Navan Unknown {uuid.uuid4().hex[:6]}"
    bookings = [
        _booking(by_name["name"], 1200.10, start="2090-04-02"),
        _booking(by_name["name"], 300, kind="HOTEL", start="2090-04-01"),
        _booking(by_name["name"], 999, status="VOIDED"),
        _booking(navan_name.upper(), 450),  # names match ignoring case
        _booking(unlinked, 75),
    ]

    summary = _sync(client, admin, monkeypatch, bookings)

    assert (summary["bookings"], summary["created"], summary["updated"], summary["removed"]) == (5, 2, 0, 0)
    seen = {e["name"]: e for e in summary["events"]}
    assert seen[by_name["name"]]["link"] == "NAME_MATCH"
    assert seen[by_name["name"]]["totals"] == [{"currency": "USD", "amount": 1500.1}]
    assert seen[navan_name.upper()]["initiative_id"] == by_link["id"]
    assert seen[navan_name.upper()]["link"] == "SET"
    assert seen[unlinked]["initiative_id"] is None
    line = _navan_lines(by_name["id"])
    assert [(r["amount"], r["status"], r["description"], r["vendor"], r["incurred_on"]) for r in line] == [
        (Decimal("1500.10"), "INVOICED", "Navan travel (2 bookings)", "Navan", date(2090, 4, 1)),
    ]
    assert [r["amount"] for r in _navan_lines(by_link["id"])] == [Decimal("450.00")]

    # Read-only in the cost list, and counted as travel on the event
    costs = client.get(f"/api/initiatives/{by_name['id']}/spend-lines", headers=admin["headers"]).json()
    navan_cost = next(c for c in costs if c["source"] == "NAVAN")
    assert navan_cost["lock_reason"] == "NAVAN"
    overview = client.get("/api/events/overview", params={"search": by_name["event_code"]}, headers=admin["headers"]).json()
    assert (overview[0]["travel_expected"], overview[0]["travel_spent"]) == (1500.1, 1500.1)

    # A booking cancelled and another added: same line, new total. All of an event's bookings cancelled: removed.
    bookings[0]["bookingStatus"] = "CANCELED"
    bookings.append(_booking(by_name["name"], 80, kind="CAR", start="2090-04-03"))
    bookings[3]["bookingStatus"] = "VOIDED"
    summary = _sync(client, admin, monkeypatch, bookings)

    assert (summary["created"], summary["updated"], summary["removed"]) == (0, 1, 1)
    assert [(r["amount"], r["description"]) for r in _navan_lines(by_name["id"])] == [
        (Decimal("380.00"), "Navan travel (2 bookings)"),
    ]
    assert _navan_lines(by_link["id"]) == []

    # Nothing changed: nothing written
    summary = _sync(client, admin, monkeypatch, bookings)
    assert (summary["created"], summary["updated"], summary["removed"]) == (0, 0, 0)

    status = client.get("/api/integrations/navan/sync-status", headers=admin["headers"]).json()
    assert status["last_error"] is None
    assert status["last_run_summary"]["bookings"] == 6


def test_sync_keeps_totals_of_navan_events_it_did_not_read(client, admin, ref, monkeypatch):
    """Bookings are only read for the lookback window: a Navan event missing from a run keeps its event's cost,
    unless the event was linked to another Navan event since the last run."""
    monkeypatch.setenv("NAVAN_CREATE_EVENTS", "false")
    by_name = _event(client, admin, ref)
    still_read = _event(client, admin, ref, navan_event_name=f"Pytest Navan Read {uuid.uuid4().hex[:6]}")
    moved = _event(client, admin, ref, navan_event_name=f"Pytest Navan Before {uuid.uuid4().hex[:6]}")
    bookings = [
        _booking(by_name["name"], 500),
        _booking(still_read["navan_event_name"], 200),
        _booking(moved["navan_event_name"], 300),
    ]
    summary = _sync(client, admin, monkeypatch, bookings)
    assert summary["created"] == 3

    # The event is now linked to another Navan event, with no bookings read yet
    response = client.patch(
        f"/api/initiatives/{moved['id']}",
        json={"event": {"navan_event_name": f"Pytest Navan After {uuid.uuid4().hex[:6]}"}},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text

    # Only one Navan event's bookings are still in the window
    summary = _sync(client, admin, monkeypatch, [_booking(still_read["navan_event_name"], 250)])

    assert (summary["created"], summary["updated"], summary["removed"]) == (0, 1, 1)
    assert [r["amount"] for r in _navan_lines(by_name["id"])] == [Decimal("500.00")]  # not read: kept
    assert [r["amount"] for r in _navan_lines(still_read["id"])] == [Decimal("250.00")]
    assert _navan_lines(moved["id"]) == []  # linked elsewhere: removed

    # Still kept on the next run; once its bookings are read again and all cancelled, removed
    summary = _sync(client, admin, monkeypatch, [_booking(still_read["navan_event_name"], 250)])
    assert (summary["created"], summary["updated"], summary["removed"]) == (0, 0, 0)
    summary = _sync(client, admin, monkeypatch, [_booking(by_name["name"], 500, status="CANCELED")])
    assert _navan_lines(by_name["id"]) == []
    assert [r["amount"] for r in _navan_lines(still_read["id"])] == [Decimal("250.00")]


def test_sync_adds_missing_events(client, admin, ref, monkeypatch):
    """A Navan event with active bookings and no event here gets one, linked to it and with its travel cost."""
    name = f"Pytest Navan Show {uuid.uuid4().hex[:6]}"
    cancelled_only = f"Pytest Navan Called Off {uuid.uuid4().hex[:6]}"
    bookings = [
        {**_booking(name, 640.25, kind="HOTEL", start="2090-05-04"), "endDate": "2090-05-06",
         "destination": {"city": "Tampa", "country": "United States"}},
        _booking(name, 210, start="2090-05-04"),
        _booking(cancelled_only, 300, status="CANCELED"),  # nothing active: no event
    ]

    summary = _sync(client, admin, monkeypatch, bookings)

    assert (summary["events_created"], summary["created"]) == (1, 1)
    seen = {e["name"]: e for e in summary["events"]}
    assert seen[name]["link"] == "SET"
    assert seen[cancelled_only]["initiative_id"] is None
    created = client.get(f"/api/initiatives/{seen[name]['initiative_id']}", headers=admin["headers"]).json()
    assert (created["name"], created["brand_name"], created["channel_name"], created["status"]) == (
        name, "TBD/All", "Events", "PLANNED",
    )
    assert (created["start_date"], created["end_date"], created["city"], created["country_code"]) == (
        "2090-05-04", "2090-05-06", "Tampa", "US",
    )
    assert created["navan_event_name"] == name
    assert [r["amount"] for r in _navan_lines(created["id"])] == [Decimal("850.25")]

    # The next run finds it linked and adds nothing
    summary = _sync(client, admin, monkeypatch, bookings)
    assert (summary["events_created"], summary["created"], summary["updated"]) == (0, 0, 0)

    # Switched off: Navan events stay unlinked
    monkeypatch.setenv("NAVAN_CREATE_EVENTS", "false")
    other = f"Pytest Navan Other Show {uuid.uuid4().hex[:6]}"
    summary = _sync(client, admin, monkeypatch, [*bookings, _booking(other, 99)])
    assert summary["events_created"] == 0
    assert {e["name"]: e for e in summary["events"]}[other]["initiative_id"] is None


def test_one_navan_event_per_event(client, admin, ref):
    name = f"Pytest Navan Shared {uuid.uuid4().hex[:6]}"
    _event(client, admin, ref, navan_event_name=name)

    response = client.post("/api/initiatives", json={
        "name": f"Pytest Navan Other {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "currency": "USD",
        "status": "DRAFT",
        "event": {"navan_event_name": name.lower()},
    }, headers=admin["headers"])

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Another event is already linked to that Navan event."


def test_navan_unavailable_is_stored(client, admin, monkeypatch):
    from services.navan_service import NavanUnavailable

    async def broken():
        raise NavanUnavailable("Navan didn't return the bookings (status 403).")

    monkeypatch.setattr(navan_cost_sync_job, "fetch_navan_bookings", broken)
    response = client.post("/api/integrations/navan/sync-costs", headers=admin["headers"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Navan didn't return the bookings (status 403)."
    status = client.get("/api/integrations/navan/sync-status", headers=admin["headers"]).json()
    assert status["last_error"] == "Navan didn't return the bookings (status 403)."
