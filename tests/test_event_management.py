"""Event management: /api/events/overview and staff on events."""
import json
import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import run_sql


@pytest.fixture
def colleague():
    """A second person to put on events; removed afterwards."""
    email = f"pytest.staff.{uuid.uuid4().hex[:8]}@zenatech.com"
    user_id = run_sql("INSERT INTO users (email, full_name, is_active) VALUES ($1, 'Pytest Booth Staff', true) RETURNING id", email)[0]["id"]
    yield {"id": user_id, "email": email}
    run_sql("DELETE FROM users WHERE id = $1", user_id)


@pytest.fixture
def initiative_viewer(viewer):
    """The temporary regular user, given only the "View Initiatives" permission group."""
    code = f"PYTEST_INITIATIVE_VIEWER_{uuid.uuid4().hex[:8].upper()}"
    role_id = run_sql("INSERT INTO roles (code, name, is_active) VALUES ($1, 'Pytest Initiative Viewer', true) RETURNING id", code)[0]["id"]
    run_sql(
        "INSERT INTO role_permission_groups (role_id, permission_group_id) "
        "SELECT $1, id FROM permission_groups WHERE code = 'MARKETING_VIEW_INITIATIVES'",
        role_id,
    )
    run_sql("INSERT INTO user_roles (user_id, role_id, is_active) VALUES ($1, $2, true)", viewer["id"], role_id)
    return viewer


def _event(client, admin, ref, start: date, days: int = 3, **overrides):
    body = {
        "name": f"Pytest Managed Expo {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=days - 1)).isoformat(),
        "budget_amount": 20000,
        "currency": "USD",
        "event": {"city": "Las Vegas", "country_code": "US", "venue": "Convention Center"},
        **overrides,
    }
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def event(client, admin, ref):
    return _event(client, admin, ref, date.today() + timedelta(days=30))


def _add(client, user, initiative_id, person_id, **fields):
    return client.post(f"/api/initiatives/{initiative_id}/staff", json={"user_id": str(person_id), **fields}, headers=user["headers"])


# --- Staff ---


def test_add_staff_with_defaults(client, admin, event, colleague):
    response = _add(client, admin, event["id"], colleague["id"], notes="  Brings the demo kit  ")

    assert response.status_code == 201, response.text
    person = response.json()
    assert person["full_name"] == "Pytest Booth Staff"
    assert person["role"] == "BOOTH_STAFF"
    assert person["notes"] == "Brings the demo kit"
    # Travel details are no longer part of the team (travel is booked in Navan)
    assert set(person) == {"id", "initiative_id", "user_id", "full_name", "email", "role", "notes", "updated_at"}

    audit = run_sql("SELECT action FROM audit_logs WHERE entity_type = 'EventStaff' AND entity_id = $1", person["id"])
    assert [a["action"] for a in audit] == ["CREATE"]


def test_same_person_twice(client, admin, event, colleague):
    _add(client, admin, event["id"], colleague["id"])
    response = _add(client, admin, event["id"], colleague["id"])
    assert response.status_code == 422
    assert response.json()["detail"] == "Pytest Booth Staff is already on this event."


def test_staff_rules(client, admin, ref, event, colleague):
    assert _add(client, admin, event["id"], colleague["id"], role="PILOT").status_code == 422  # unknown role
    assert _add(client, admin, event["id"], uuid.uuid4()).status_code == 422  # unknown person
    ads = client.post(
        "/api/initiatives",
        json={"name": "Pytest Ads", "brand_id": ref["brands"]["DESKFLEX"], "channel_id": ref["channels"]["PAID_SEARCH"]},
        headers=admin["headers"],
    ).json()
    not_event = _add(client, admin, ads["id"], colleague["id"])
    assert not_event.status_code == 422
    assert not_event.json()["detail"] == "Staff can only be assigned to events."


def test_update_and_remove_staff(client, admin, event, colleague):
    person = _add(client, admin, event["id"], colleague["id"]).json()

    updated = client.patch(
        f"/api/event-staff/{person['id']}",
        json={"role": "DEMO", "notes": "2 demo drones in Pelican cases"},
        headers=admin["headers"],
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["role"], updated.json()["notes"]) == ("DEMO", "2 demo drones in Pelican cases")
    audit = run_sql("SELECT new_value FROM audit_logs WHERE entity_type = 'EventStaff' AND entity_id = $1 AND action = 'UPDATE'", person["id"])
    assert set(json.loads(audit[0]["new_value"])) == {"initiative_id", "role", "notes"}
    assert client.patch(f"/api/event-staff/{person['id']}", json={"role": None}, headers=admin["headers"]).status_code == 422

    assert client.delete(f"/api/event-staff/{person['id']}", headers=admin["headers"]).status_code == 204
    assert client.get(f"/api/initiatives/{event['id']}/staff", headers=admin["headers"]).json() == []
    assert client.delete(f"/api/event-staff/{person['id']}", headers=admin["headers"]).status_code == 404


def test_viewer_can_read_but_not_change(client, admin, event, colleague, initiative_viewer):
    person = _add(client, admin, event["id"], colleague["id"]).json()
    assert client.get(f"/api/initiatives/{event['id']}/staff", headers=initiative_viewer["headers"]).status_code == 200
    assert client.get("/api/events/overview", headers=initiative_viewer["headers"]).status_code == 200
    assert _add(client, initiative_viewer, event["id"], colleague["id"]).status_code == 403
    assert client.patch(f"/api/event-staff/{person['id']}", json={"notes": "x"}, headers=initiative_viewer["headers"]).status_code == 403
    assert client.delete(f"/api/event-staff/{person['id']}", headers=initiative_viewer["headers"]).status_code == 403


# --- Event cards ---


def test_overview_shows_what_needs_doing(client, admin, ref, colleague, event):
    _add(client, admin, event["id"], colleague["id"])
    client.post(
        f"/api/initiatives/{event['id']}/spend-lines",
        json={"category_id": run_sql("SELECT id FROM spend_categories WHERE code = 'BOOTH_SPACE'")[0]["id"], "amount": 8000, "incurred_on": "2026-06-01"},
        headers=admin["headers"],
    )

    overview = client.get("/api/events/overview", params={"search": event["event_code"]}, headers=admin["headers"]).json()

    assert len(overview) == 1
    card = overview[0]
    assert card["timing"] == "UPCOMING"
    assert card["days_away"] == 30
    assert card["staff_count"] == 1
    assert not {"staff_ready", "staff_missing_details", "staff_booked"} & set(card)
    assert (card["expected_cost"], card["cost_lines"]) == (8000, 1)
    assert (card["demos_given"], card["has_results"]) == (0, False)
    assert card["city"] == "Las Vegas"


def test_overview_order_and_timing_filter(client, admin, ref):
    tag = uuid.uuid4().hex[:6]
    today = date.today()
    now = _event(client, admin, ref, today - timedelta(days=1), name=f"Pytest Now {tag}")
    soon = _event(client, admin, ref, today + timedelta(days=5), name=f"Pytest Soon {tag}")
    later = _event(client, admin, ref, today + timedelta(days=50), name=f"Pytest Later {tag}")
    past = _event(client, admin, ref, today - timedelta(days=40), name=f"Pytest Past {tag}")

    names = [e["name"] for e in client.get("/api/events/overview", params={"search": tag}, headers=admin["headers"]).json()]
    assert names == [now["name"], soon["name"], later["name"], past["name"]]

    upcoming = client.get("/api/events/overview", params={"search": tag, "timing": "UPCOMING"}, headers=admin["headers"]).json()
    assert [e["name"] for e in upcoming] == [soon["name"], later["name"]]
