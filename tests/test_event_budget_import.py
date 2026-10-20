"""Uploading many events' budgets at once: /api/initiatives/event-budgets/preview and /apply."""
import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import run_sql

PREVIEW = "/api/initiatives/event-budgets/preview"
APPLY = "/api/initiatives/event-budgets/apply"


def _event(client, admin, ref, name=None, budget=20000, **event):
    start = date.today() + timedelta(days=40)
    body = {
        "name": name or f"Pytest Budget Expo {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
        "budget_amount": budget,
        "currency": "USD",
        "event": {"city": "Austin", "country_code": "US", **event},
    }
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def _saved(initiative_id):
    return dict(run_sql(
        """SELECT i.budget_amount::float8 AS budget, i.currency, ed.travel_budget_amount::float8 AS travel
           FROM initiatives i LEFT JOIN event_details ed ON ed.initiative_id = i.id WHERE i.id = $1""",
        uuid.UUID(initiative_id),
    )[0])


def _upload(client, user, csv_text, name="budgets.csv"):
    return client.post(PREVIEW, files={"file": (name, csv_text.encode(), "text/csv")}, headers=user["headers"])


def _rows(preview):
    return {r["row_number"]: r for r in preview["rows"]}


@pytest.fixture
def events(client, admin, ref):
    return [_event(client, admin, ref, budget=20000, travel_budget_amount=5000), _event(client, admin, ref, budget=8000)]


def test_preview_matches_by_code_and_name_and_saves_nothing(client, admin, events):
    first, second = events
    sheet = (
        "Event,Budget,Currency,Travel budget\n"
        f"{first['event_code']},25000,usd,6000\n"   # by code, with a new travel budget
        f"{second['name']},8000,,\n"                # by name, same budget: unchanged
        "EVT-1999-9999,100,,\n"                     # no such event
        f"{second['name'].upper()},9000,,\n"        # the same event again
    )
    response = _upload(client, admin, sheet)
    assert response.status_code == 200, response.text
    rows = _rows(response.json())

    assert rows[2]["status"] == "UPDATE"
    assert rows[2]["initiative_id"] == first["id"]
    assert (rows[2]["old_budget"], rows[2]["new_budget"]) == (20000, 25000)
    assert (rows[2]["old_travel_budget"], rows[2]["new_travel_budget"]) == (5000, 6000)
    assert rows[2]["new_currency"] == "USD"
    assert rows[3]["status"] == "UNCHANGED"
    assert rows[4]["status"] == "ERROR" and "No event" in rows[4]["message"]
    assert rows[5]["status"] == "ERROR" and "already in row 3" in rows[5]["message"]
    assert response.json()["counts"] == {"UPDATE": 1, "UNCHANGED": 1, "ERROR": 2}
    assert _saved(first["id"]) == {"budget": 20000, "currency": "USD", "travel": 5000}


@pytest.mark.parametrize(
    "budget,travel,message",
    [("", "", "No budget"), ("-5", "", "negative"), ("abc", "", "isn't an amount"), ("4000", "", "travel budget has to be smaller")],
)
def test_preview_flags_values_that_cant_be_saved(client, admin, events, budget, travel, message):
    # The first event's travel budget is 5,000: a 4,000 budget would leave it too high
    sheet = f"Event code,Budget,Travel budget\n{events[0]['event_code']},{budget},{travel}\n"
    row = _rows(_upload(client, admin, sheet).json())[2]
    assert row["status"] == "ERROR"
    assert message in row["message"]


def test_preview_asks_for_the_code_when_names_repeat(client, admin, ref):
    name = f"Pytest Twin Expo {uuid.uuid4().hex[:6]}"
    _event(client, admin, ref, name=name)
    _event(client, admin, ref, name=name)
    row = _rows(_upload(client, admin, f"Event,Budget\n{name},500\n").json())[2]
    assert row["status"] == "ERROR"
    assert "Use the event code" in row["message"]


def test_preview_needs_the_event_and_budget_columns(client, admin):
    response = _upload(client, admin, "Event,Notes\nSomething,hello\nOther,x\n")
    assert response.status_code == 422
    assert "Budget" in response.json()["detail"]


def test_apply_saves_budgets_and_records_activity(client, admin, events):
    first, second = events
    changes = [
        {"initiative_id": first["id"], "budget_amount": 25000, "currency": "usd", "travel_budget_amount": 6000},
        {"initiative_id": second["id"], "budget_amount": 9500, "currency": "CAD", "travel_budget_amount": None},
    ]
    response = client.post(APPLY, json={"changes": changes}, headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 2}
    assert _saved(first["id"]) == {"budget": 25000, "currency": "USD", "travel": 6000}
    assert _saved(second["id"]) == {"budget": 9500, "currency": "CAD", "travel": None}

    activity = client.get(f"/api/initiatives/{first['id']}/activity", headers=admin["headers"]).json()
    assert any(item.get("title") == "Details updated" or "Budget" in (item.get("title") or "") for item in activity)
    budget_only = client.post(
        APPLY, json={"changes": [{"initiative_id": second["id"], "budget_amount": 9900, "currency": "CAD"}]}, headers=admin["headers"]
    )
    assert budget_only.status_code == 200
    latest = client.get(f"/api/initiatives/{second['id']}/activity", headers=admin["headers"]).json()[0]
    assert latest["title"] == "Budget updated"
    assert "9,900" in latest["detail"]


def test_apply_is_all_or_nothing(client, admin, events):
    first, second = events
    changes = [
        {"initiative_id": second["id"], "budget_amount": 12000, "currency": "USD"},
        # The first event keeps its 5,000 travel budget, which a 3,000 budget can't hold
        {"initiative_id": first["id"], "budget_amount": 3000, "currency": "USD", "travel_budget_amount": 5000},
    ]
    response = client.post(APPLY, json={"changes": changes}, headers=admin["headers"])
    assert response.status_code == 422
    assert "travel budget" in response.json()["detail"]
    assert _saved(second["id"])["budget"] == 8000


def test_apply_only_changes_events(client, admin, ref):
    other = client.post(
        "/api/initiatives",
        json={
            "name": f"Pytest Not An Event {uuid.uuid4().hex[:6]}",
            "brand_id": ref["brands"]["DESKFLEX"],
            "channel_id": ref["channels"]["SEO"],
            "sub_channel_id": ref["sub_channels"]["BLOG"],
            "start_date": date.today().isoformat(),
            "budget_amount": 100,
            "currency": "USD",
        },
        headers=admin["headers"],
    )
    assert other.status_code == 201, other.text
    response = client.post(
        APPLY, json={"changes": [{"initiative_id": other.json()["id"], "budget_amount": 5, "currency": "USD"}]}, headers=admin["headers"]
    )
    assert response.status_code == 422
    assert _saved(other.json()["id"])["budget"] == 100


def test_upload_needs_permission_to_edit_initiatives(client, viewer, events):
    assert _upload(client, viewer, f"Event,Budget\n{events[0]['event_code']},1\n").status_code == 403
    response = client.post(
        APPLY, json={"changes": [{"initiative_id": events[0]["id"], "budget_amount": 1, "currency": "USD"}]}, headers=viewer["headers"]
    )
    assert response.status_code == 403
    assert _saved(events[0]["id"])["budget"] == 20000
