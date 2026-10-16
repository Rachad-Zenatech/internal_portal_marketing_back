"""Travel budget on events, what the travel system reads, and which initiatives can be deleted."""
import json
import uuid
from datetime import date, timedelta

import pytest

from services.service_security_policy import generate_scoped_outbound_token
from tests.conftest import run_sql

TOO_HIGH = "The travel budget has to be smaller than the event budget."
NEEDS_BUDGET = "Set the event budget before the travel budget."
IN_TRAVEL = "This event may already be in the travel system. Cancel it instead."


def _event(client, admin, ref, *, budget=20000, travel=None, status="PLANNED", expect=201, **overrides):
    start = date.today() + timedelta(days=30)
    event = {"city": "Las Vegas", "country_code": "US"}
    if travel is not None:
        event["travel_budget_amount"] = travel
    body = {
        "name": f"Pytest Travel Expo {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
        "budget_amount": budget,
        "currency": "USD",
        "status": status,
        "event": event,
        **overrides,
    }
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == expect, response.text
    return response.json()


def _patch(client, admin, initiative_id, payload):
    return client.patch(f"/api/initiatives/{initiative_id}", json=payload, headers=admin["headers"])


def _feed(client, **params):
    token = generate_scoped_outbound_token("marketing", ["events:read"], source_service="travel-management")
    response = client.get("/api/integrations/travel/events", params=params, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return {e["event_code"]: e for e in response.json()}


# --- Saving it ---


def test_create_with_travel_budget(client, admin, ref):
    created = _event(client, admin, ref, travel=6000)

    assert created["travel_budget_amount"] == 6000
    body = client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).json()
    assert body["travel_budget_amount"] == 6000
    listed = client.get("/api/initiatives", params={"search": created["event_code"]}, headers=admin["headers"]).json()
    assert listed["items"][0]["travel_budget_amount"] == 6000
    [audit] = run_sql(
        "SELECT new_value FROM audit_logs WHERE entity_type = 'Initiative' AND entity_id = $1 AND action = 'CREATE'", created["id"]
    )
    assert json.loads(audit["new_value"])["event"]["travel_budget_amount"] == 6000


def test_create_refuses_a_travel_budget_not_below_the_event_budget(client, admin, ref):
    for travel in (20000, 25000):
        response = _event(client, admin, ref, travel=travel, expect=422)
        assert response["detail"] == TOO_HIGH


def test_create_refuses_a_travel_budget_without_an_event_budget(client, admin, ref):
    assert _event(client, admin, ref, budget=None, travel=500, expect=422)["detail"] == NEEDS_BUDGET


def test_negative_travel_budget_is_refused(client, admin, ref):
    _event(client, admin, ref, travel=-1, expect=422)


def test_drafts_can_skip_the_event_budget_but_not_the_limit(client, admin, ref):
    draft = _event(client, admin, ref, budget=None, travel=500, status="DRAFT")
    assert draft["travel_budget_amount"] == 500
    assert _event(client, admin, ref, budget=1000, travel=1000, status="DRAFT", expect=422)["detail"] == TOO_HIGH

    # Finishing the draft needs the event budget
    finished = _patch(client, admin, draft["id"], {"status": "PLANNED"})
    assert (finished.status_code, finished.json()["detail"]) == (422, NEEDS_BUDGET)
    assert _patch(client, admin, draft["id"], {"status": "PLANNED", "budget_amount": 600}).status_code == 200


def test_update_travel_budget_is_checked_and_audited(client, admin, ref):
    created = _event(client, admin, ref, travel=5000)

    too_high = _patch(client, admin, created["id"], {"event": {"travel_budget_amount": 20000}})
    assert (too_high.status_code, too_high.json()["detail"]) == (422, TOO_HIGH)

    response = _patch(client, admin, created["id"], {"event": {"travel_budget_amount": 7500}})
    assert response.status_code == 200, response.text
    assert response.json()["travel_budget_amount"] == 7500
    audits = run_sql(
        "SELECT old_value, new_value FROM audit_logs WHERE entity_type = 'Initiative' AND entity_id = $1 AND action = 'UPDATE'",
        created["id"],
    )
    [audit] = audits
    assert json.loads(audit["old_value"])["event"] == {"travel_budget_amount": 5000}
    assert json.loads(audit["new_value"])["event"]["travel_budget_amount"] == 7500

    cleared = _patch(client, admin, created["id"], {"event": {"travel_budget_amount": None}})
    assert cleared.json()["travel_budget_amount"] is None


def test_lowering_or_clearing_the_event_budget_below_the_travel_budget_is_refused(client, admin, ref):
    created = _event(client, admin, ref, travel=5000)

    lowered = _patch(client, admin, created["id"], {"budget_amount": 5000})
    assert (lowered.status_code, lowered.json()["detail"]) == (422, TOO_HIGH)
    cleared = _patch(client, admin, created["id"], {"budget_amount": None})
    assert (cleared.status_code, cleared.json()["detail"]) == (422, NEEDS_BUDGET)
    # Both in one request is fine when the result fits
    both = _patch(client, admin, created["id"], {"budget_amount": 4000, "event": {"travel_budget_amount": 3000}})
    assert both.status_code == 200, both.text
    assert (both.json()["budget_amount"], both.json()["travel_budget_amount"]) == (4000, 3000)


def test_changing_the_currency_keeps_the_travel_budget_in_the_same_currency(client, admin, ref):
    created = _event(client, admin, ref, travel=5000)

    response = _patch(client, admin, created["id"], {"currency": "CAD"})

    assert response.status_code == 200, response.text
    assert (response.json()["currency"], response.json()["travel_budget_amount"]) == ("CAD", 5000)
    assert _feed(client)[created["event_code"]]["currency"] == "CAD"
    # And a currency change can't sneak a too-large travel budget through
    both = _patch(client, admin, created["id"], {"currency": "EUR", "budget_amount": 5000})
    assert (both.status_code, both.json()["detail"]) == (422, TOO_HIGH)


# --- Travel system feed ---


def test_feed_includes_travel_budget_and_currency(client, admin, ref):
    with_budget = _event(client, admin, ref, travel=6000)
    without = _event(client, admin, ref)

    feed = _feed(client)

    assert (feed[with_budget["event_code"]]["travel_budget_amount"], feed[with_budget["event_code"]]["currency"]) == (6000, "USD")
    assert feed[without["event_code"]]["travel_budget_amount"] is None


def test_changing_the_travel_budget_moves_the_event_in_the_feed(client, admin, ref):
    created = _event(client, admin, ref, travel=6000)
    since = run_sql("SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') AS t")[0]["t"]
    assert created["event_code"] not in _feed(client, updated_since=since)

    assert _patch(client, admin, created["id"], {"event": {"travel_budget_amount": 6500}}).status_code == 200

    feed = _feed(client, updated_since=since)
    assert feed[created["event_code"]]["travel_budget_amount"] == 6500


# --- Event cards ---


def _travel_cost(initiative_id, amount, status, currency="USD"):
    category = run_sql("SELECT id FROM spend_categories WHERE code = 'BOOTH_SPACE'")[0]["id"]
    run_sql(
        "INSERT INTO spend_lines (initiative_id, category_id, status, amount, currency, incurred_on, paid_on, source, external_ref) "
        "VALUES ($1, $2, $3::varchar, $4, $5, CURRENT_DATE, CASE WHEN $3::varchar = 'PAID' THEN CURRENT_DATE END, 'TRAVEL_FEED', $6)",
        uuid.UUID(initiative_id), category, status, amount, currency, f"receipt:{uuid.uuid4()}",
    )


def test_overview_shows_travel_budget_and_spend(client, admin, ref):
    created = _event(client, admin, ref, travel=6000)
    _travel_cost(created["id"], 1200, "PAID")
    _travel_cost(created["id"], 300, "INVOICED")
    _travel_cost(created["id"], 400, "COMMITTED")
    _travel_cost(created["id"], 999, "PAID", currency="CAD")  # another currency: not counted
    category = run_sql("SELECT id FROM spend_categories WHERE code = 'BOOTH_SPACE'")[0]["id"]
    client.post(
        f"/api/initiatives/{created['id']}/spend-lines",
        json={"category_id": category, "amount": 8000, "incurred_on": date.today().isoformat(), "status": "PAID",
              "paid_on": date.today().isoformat()},
        headers=admin["headers"],
    )

    [card] = client.get("/api/events/overview", params={"search": created["event_code"]}, headers=admin["headers"]).json()

    assert card["travel_budget_amount"] == 6000
    assert card["travel_spent"] == 1500
    assert card["travel_expected"] == 1900


def test_overview_without_travel(client, admin, ref):
    created = _event(client, admin, ref)
    [card] = client.get("/api/events/overview", params={"search": created["event_code"]}, headers=admin["headers"]).json()
    assert (card["travel_budget_amount"], card["travel_spent"], card["travel_expected"]) == (None, 0, 0)


# --- Deleting ---


@pytest.mark.parametrize("status", ["PLANNED", "ACTIVE", "CANCELLED"])
def test_an_event_past_draft_cant_be_deleted(client, admin, ref, status):
    created = _event(client, admin, ref, status="PLANNED")
    if status != "PLANNED":
        assert _patch(client, admin, created["id"], {"status": status}).status_code == 200

    response = client.delete(f"/api/initiatives/{created['id']}", headers=admin["headers"])

    assert (response.status_code, response.json()["detail"]) == (422, IN_TRAVEL)
    assert client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).status_code == 200


def test_a_draft_event_can_be_deleted(client, admin, ref):
    draft = _event(client, admin, ref, status="DRAFT", travel=500)

    assert client.delete(f"/api/initiatives/{draft['id']}", headers=admin["headers"]).status_code == 204

    assert client.get(f"/api/initiatives/{draft['id']}", headers=admin["headers"]).status_code == 404
    assert run_sql("SELECT count(*) AS n FROM event_details WHERE initiative_id = $1", uuid.UUID(draft["id"]))[0]["n"] == 0
    [audit] = run_sql("SELECT old_value FROM audit_logs WHERE entity_type = 'Initiative' AND entity_id = $1 AND action = 'DELETE'", draft["id"])
    assert json.loads(audit["old_value"])["event_code"] == draft["event_code"]


def test_other_initiatives_can_be_deleted(client, admin, ref):
    created = client.post(
        "/api/initiatives",
        json={"name": "Pytest Search Delete", "brand_id": ref["brands"]["DESKFLEX"], "channel_id": ref["channels"]["PAID_SEARCH"],
              "sub_channel_id": ref["sub_channels"]["GOOGLE_ADS"], "status": "ACTIVE"},
        headers=admin["headers"],
    ).json()

    assert client.delete(f"/api/initiatives/{created['id']}", headers=admin["headers"]).status_code == 204
    assert client.delete(f"/api/initiatives/{created['id']}", headers=admin["headers"]).status_code == 404


def test_deleting_needs_permission(client, admin, ref, viewer):
    draft = _event(client, admin, ref, status="DRAFT")
    assert client.delete(f"/api/initiatives/{draft['id']}", headers=viewer["headers"]).status_code == 403
