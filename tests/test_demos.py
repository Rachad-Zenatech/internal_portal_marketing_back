"""Demos at events: /api/initiatives/{id}/demos, /demos/summary and /api/demos/{demo_id}."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import run_sql


@pytest.fixture(scope="module")
def demo_ref():
    """Demo type and outcome ids by code, plus spend category ids."""
    return {
        "types": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM demo_types")},
        "outcomes": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM demo_outcomes")},
        "categories": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM spend_categories")},
    }


def _create_initiative(client, admin, ref, **overrides):
    body = {
        "name": f"Pytest Demo Expo {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "start_date": "2026-11-04",
        "end_date": "2026-11-06",
        "budget_amount": 20000,
        "currency": "USD",
        "event": {"city": "Las Vegas", "country_code": "US"},
        **overrides,
    }
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def event(client, admin, ref):
    return _create_initiative(client, admin, ref)


@pytest.fixture
def initiative_viewer(viewer):
    """The temporary regular user, given only the "View Initiatives" permission group."""
    code = f"PYTEST_INITIATIVE_VIEWER_{uuid.uuid4().hex[:8].upper()}"
    role_id = run_sql(
        "INSERT INTO roles (code, name, is_active) VALUES ($1, 'Pytest Initiative Viewer', true) RETURNING id", code
    )[0]["id"]
    run_sql(
        "INSERT INTO role_permission_groups (role_id, permission_group_id) "
        "SELECT $1, id FROM permission_groups WHERE code = 'MARKETING_VIEW_INITIATIVES'",
        role_id,
    )
    run_sql("INSERT INTO user_roles (user_id, role_id, is_active) VALUES ($1, $2, true)", viewer["id"], role_id)
    return viewer


def _log(client, user, initiative_id, demo_ref, demo_type="BOOTH", **fields):
    body = {"demo_type_id": demo_ref["types"][demo_type], **fields}
    return client.post(f"/api/initiatives/{initiative_id}/demos", json=body, headers=user["headers"])


def _add_cost(client, admin, initiative_id, demo_ref, amount, currency=None):
    body = {"category_id": demo_ref["categories"]["BOOTH_SPACE"], "amount": amount, "incurred_on": "2026-10-01"}
    if currency:
        body["currency"] = currency
    response = client.post(f"/api/initiatives/{initiative_id}/spend-lines", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text


def test_log_demo_with_defaults(client, admin, event, demo_ref):
    response = _log(client, admin, event["id"], demo_ref, company="  Northwind Facilities  ")

    assert response.status_code == 201, response.text
    demo = response.json()
    assert demo["demo_type_code"] == "BOOTH"
    assert demo["company"] == "Northwind Facilities"
    # Brand defaults to the event's, the staff member to whoever logs it, the outcome to "No outcome yet"
    assert demo["brand_id"] == event["brand_id"]
    assert demo["given_by"] == str(admin["id"])
    assert demo["outcome_code"] == "PENDING"
    assert demo["source"] == "MANUAL"
    assert demo["demo_at"]

    audit = run_sql("SELECT action FROM audit_logs WHERE entity_type = 'Demo' AND entity_id = $1", demo["id"])
    assert [a["action"] for a in audit] == ["CREATE"]


def test_mobile_demo_source(client, admin, event, demo_ref):
    response = _log(client, admin, event["id"], demo_ref, "LIVE", source="MOBILE")
    assert response.status_code == 201, response.text
    assert response.json()["source"] == "MOBILE"


def test_demos_only_on_events(client, admin, ref, demo_ref):
    ad = _create_initiative(
        client, admin, ref,
        channel_id=ref["channels"]["PAID_SEARCH"], sub_channel_id=ref["sub_channels"]["GOOGLE_ADS"], event=None,
    )
    response = _log(client, admin, ad["id"], demo_ref)
    assert response.status_code == 422
    assert response.json()["detail"] == "Demos can only be logged on events."


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"hubspot_url": "http://app.hubspot.com/contacts/1"}, None),  # Pydantic: must be https
        ({"demo_type_id": 999999}, "Unknown demo type."),
        ({"outcome_id": 999999}, "Unknown outcome."),
        ({"brand_id": 999999}, "Unknown brand."),
    ],
)
def test_log_demo_rejects_invalid_input(client, admin, event, demo_ref, fields, expected):
    body = {"demo_type_id": demo_ref["types"]["BOOTH"], **fields}
    response = client.post(f"/api/initiatives/{event['id']}/demos", json=body, headers=admin["headers"])
    assert response.status_code == 422, response.text
    if expected:
        assert response.json()["detail"] == expected


def test_record_outcome_after_the_event(client, admin, event, demo_ref):
    demo = _log(client, admin, event["id"], demo_ref, company="Harbor Health").json()
    assert demo["outcome_updated_at"] is None

    response = client.patch(
        f"/api/demos/{demo['id']}",
        json={"outcome_id": demo_ref["outcomes"]["DEAL_OPENED"], "note": "Pilot for 3 floors"},
        headers=admin["headers"],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome_code"] == "DEAL_OPENED"
    assert body["outcome_rank"] == 3
    assert body["outcome_updated_at"] is not None
    assert body["note"] == "Pilot for 3 floors"

    audit = run_sql(
        "SELECT old_value, new_value FROM audit_logs WHERE entity_type = 'Demo' AND entity_id = $1 AND action = 'UPDATE'",
        demo["id"],
    )
    assert len(audit) == 1
    old, new = json.loads(audit[0]["old_value"]), json.loads(audit[0]["new_value"])
    assert set(new) == {"initiative_id", "outcome_id", "note"}
    assert old["outcome_id"] == demo_ref["outcomes"]["PENDING"]


def test_update_cannot_clear_type_or_outcome(client, admin, event, demo_ref):
    demo = _log(client, admin, event["id"], demo_ref).json()
    for field in ("demo_type_id", "outcome_id"):
        response = client.patch(f"/api/demos/{demo['id']}", json={field: None}, headers=admin["headers"])
        assert response.status_code == 422, response.text


def test_delete_demo(client, admin, event, demo_ref):
    demo = _log(client, admin, event["id"], demo_ref).json()

    assert client.delete(f"/api/demos/{demo['id']}", headers=admin["headers"]).status_code == 204
    assert client.get(f"/api/initiatives/{event['id']}/demos", headers=admin["headers"]).json() == []
    assert client.delete(f"/api/demos/{demo['id']}", headers=admin["headers"]).status_code == 404


def test_list_is_newest_first(client, admin, event, demo_ref):
    _log(client, admin, event["id"], demo_ref, company="Early", demo_at="2026-11-04T10:00:00-08:00")
    _log(client, admin, event["id"], demo_ref, company="Late", demo_at="2026-11-05T15:00:00-08:00")

    demos = client.get(f"/api/initiatives/{event['id']}/demos", headers=admin["headers"]).json()
    assert [d["company"] for d in demos] == ["Late", "Early"]


def test_ceo_example(client, admin, event, demo_ref):
    """A $20,000 trade show with 20 demos, 2 of which became customers: $1,000 per demo, $10,000 per customer."""
    _add_cost(client, admin, event["id"], demo_ref, 20000)
    outcomes = (
        ["CUSTOMER"] * 2 + ["DEAL_OPENED"] * 3 + ["DEMO_SCHEDULED"] * 2 + ["FOLLOW_UP"] * 5
        + ["NOT_A_FIT"] * 4 + ["PENDING"] * 4
    )
    types = ["BOOTH"] * 14 + ["SCHEDULED"] * 4 + ["LIVE"] * 2
    for outcome, demo_type in zip(outcomes, types):
        response = _log(client, admin, event["id"], demo_ref, demo_type, outcome_id=demo_ref["outcomes"][outcome])
        assert response.status_code == 201, response.text

    summary = client.get(f"/api/initiatives/{event['id']}/demos/summary", headers=admin["headers"]).json()

    assert summary["total"] == 20
    assert summary["expected_cost"] == 20000
    assert summary["currency"] == "USD"
    assert summary["cost_per_demo"] == 1000
    assert summary["cost_per_customer"] == 10000
    assert summary["customer_rate"] == 10.0
    assert summary["funnel"] == {"demos": 20, "with_outcome": 16, "follow_ups": 12, "deals": 5, "customers": 2}
    # Follow-ups or further out of demos whose outcome is known: 12 of 16
    assert summary["follow_up_rate"] == 75.0
    assert [(t["key"], t["count"]) for t in summary["by_type"]] == [("BOOTH", 14), ("SCHEDULED", 4), ("LIVE", 2)]
    assert {o["key"]: o["count"] for o in summary["by_outcome"]}["CUSTOMER"] == 2
    assert summary["by_brand"] == [{"key": str(event["brand_id"]), "name": "DeskFlex", "count": 20}]
    assert summary["by_given_by"][0]["count"] == 20
    assert summary["mixed_currencies"] is False


def test_summary_without_demos_or_costs(client, admin, event):
    summary = client.get(f"/api/initiatives/{event['id']}/demos/summary", headers=admin["headers"]).json()
    assert summary["total"] == 0
    assert summary["cost_per_demo"] is None
    assert summary["cost_per_customer"] is None
    assert summary["customer_rate"] is None
    assert summary["follow_up_rate"] is None
    assert summary["by_type"] == []


def test_costs_in_other_currencies_are_not_counted(client, admin, event, demo_ref):
    _add_cost(client, admin, event["id"], demo_ref, 1000)
    _add_cost(client, admin, event["id"], demo_ref, 500, currency="GBP")
    _log(client, admin, event["id"], demo_ref)

    summary = client.get(f"/api/initiatives/{event['id']}/demos/summary", headers=admin["headers"]).json()
    assert summary["expected_cost"] == 1000
    assert summary["cost_per_demo"] == 1000
    assert summary["mixed_currencies"] is True


def test_viewer_can_read_but_not_change(client, admin, event, demo_ref, initiative_viewer):
    demo = _log(client, admin, event["id"], demo_ref).json()

    assert client.get(f"/api/initiatives/{event['id']}/demos", headers=initiative_viewer["headers"]).status_code == 200
    assert client.get(f"/api/initiatives/{event['id']}/demos/summary", headers=initiative_viewer["headers"]).status_code == 200
    assert _log(client, initiative_viewer, event["id"], demo_ref).status_code == 403
    assert client.patch(f"/api/demos/{demo['id']}", json={"note": "x"}, headers=initiative_viewer["headers"]).status_code == 403
    assert client.delete(f"/api/demos/{demo['id']}", headers=initiative_viewer["headers"]).status_code == 403


def test_user_without_marketing_access_is_refused(client, event, viewer):
    assert client.get(f"/api/initiatives/{event['id']}/demos", headers=viewer["headers"]).status_code == 403


def test_missing_initiative_and_demo(client, admin, demo_ref):
    missing = uuid.uuid4()
    assert client.get(f"/api/initiatives/{missing}/demos", headers=admin["headers"]).status_code == 404
    assert client.get(f"/api/initiatives/{missing}/demos/summary", headers=admin["headers"]).status_code == 404
    assert _log(client, admin, missing, demo_ref).status_code == 404
    assert client.patch(f"/api/demos/{missing}", json={"note": "x"}, headers=admin["headers"]).status_code == 404


def test_deleting_the_event_removes_its_demos(client, admin, event, demo_ref):
    _log(client, admin, event["id"], demo_ref)
    run_sql("DELETE FROM initiatives WHERE id = $1", uuid.UUID(event["id"]))
    assert run_sql("SELECT count(*) AS n FROM event_demos WHERE initiative_id = $1", uuid.UUID(event["id"]))[0]["n"] == 0


def test_reference_data_has_demo_lists(client, admin):
    data = client.get("/api/reference-data", headers=admin["headers"]).json()
    assert [t["code"] for t in data["demo_types"]] == ["BOOTH", "SCHEDULED", "LIVE"]
    ranks = {o["code"]: o["funnel_rank"] for o in data["demo_outcomes"]}
    assert ranks == {
        "PENDING": 0, "NO_SHOW": 0, "NOT_A_FIT": 0, "FOLLOW_UP": 1, "DEMO_SCHEDULED": 2, "DEAL_OPENED": 3, "CUSTOMER": 4,
    }
    assert "DEMO_EQUIPMENT" in {c["code"] for c in data["spend_categories"]}


def _summary(client, admin, initiative_id):
    response = client.get(f"/api/initiatives/{initiative_id}/demos/summary", headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def test_only_demos_that_happened_count(client, admin, event, demo_ref):
    """A demo booked for later counts once it has happened; a no-show never does."""
    _add_cost(client, admin, event["id"], demo_ref, 1000)
    _log(client, admin, event["id"], demo_ref)
    _log(client, admin, event["id"], demo_ref)
    later = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert _log(client, admin, event["id"], demo_ref, "SCHEDULED", demo_at=later).status_code == 201
    assert _log(client, admin, event["id"], demo_ref, "SCHEDULED", outcome_id=demo_ref["outcomes"]["NO_SHOW"]).status_code == 201

    summary = _summary(client, admin, event["id"])

    assert (summary["total"], summary["upcoming"], summary["no_shows"]) == (2, 1, 1)
    assert summary["cost_per_demo"] == 500
    assert [(t["key"], t["count"]) for t in summary["by_type"]] == [("BOOTH", 2)]
    # A no-show isn't an outcome of a demo given, so it doesn't lower the follow-up rate
    assert summary["funnel"]["with_outcome"] == 0


def test_companies_demoed(client, admin, event, demo_ref):
    _add_cost(client, admin, event["id"], demo_ref, 4000)
    for company in ["Acme", " acme ", "Globex", None, None]:
        _log(client, admin, event["id"], demo_ref, company=company)

    summary = _summary(client, admin, event["id"])

    # Acme twice is one company; demos with no company recorded count one each
    assert (summary["total"], summary["companies_demoed"]) == (5, 4)
    assert (summary["cost_per_demo"], summary["cost_per_company"]) == (800, 1000)


def test_costs_on_expected_and_actual(client, admin, ref, demo_ref):
    past = _create_initiative(client, admin, ref, start_date="2026-01-10", end_date="2026-01-12")
    _add_cost(client, admin, past["id"], demo_ref, 1000)  # committed, not invoiced yet
    paid = client.post(
        f"/api/initiatives/{past['id']}/spend-lines",
        json={"category_id": demo_ref["categories"]["BOOTH_SPACE"], "amount": 3000, "incurred_on": "2026-01-05",
              "status": "PAID", "paid_on": "2026-01-20"},
        headers=admin["headers"],
    )
    assert paid.status_code == 201, paid.text
    _log(client, admin, past["id"], demo_ref, outcome_id=demo_ref["outcomes"]["CUSTOMER"])
    _log(client, admin, past["id"], demo_ref)

    summary = _summary(client, admin, past["id"])

    assert (summary["expected_cost"], summary["actual_cost"], summary["event_ended"]) == (4000, 3000, True)
    assert (summary["cost_per_demo"], summary["cost_per_demo_actual"]) == (2000, 1500)
    assert (summary["cost_per_customer"], summary["cost_per_customer_actual"]) == (4000, 3000)


def test_upcoming_event_has_not_ended(client, admin, ref):
    future = _create_initiative(client, admin, ref, start_date="2099-01-10", end_date="2099-01-12")

    assert _summary(client, admin, future["id"])["event_ended"] is False


def test_results_count_only_demos_that_happened(client, admin, event, demo_ref):
    _log(client, admin, event["id"], demo_ref, demo_at="2025-09-05T10:00:00Z")
    _log(client, admin, event["id"], demo_ref, demo_at="2025-09-06T10:00:00Z", outcome_id=demo_ref["outcomes"]["NO_SHOW"])
    _log(client, admin, event["id"], demo_ref, "SCHEDULED", demo_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat())

    outcomes = client.get(f"/api/initiatives/{event['id']}/outcomes", headers=admin["headers"]).json()
    performance = client.get(f"/api/initiatives/{event['id']}/performance", headers=admin["headers"]).json()

    assert [(v["period_start"], v["value"]) for v in outcomes["values"] if v["metric_code"] == "DEMOS"] == [("2025-09-01", 1)]
    assert performance["totals"]["DEMOS"] == 1
