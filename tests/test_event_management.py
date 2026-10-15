"""Event management: /api/events/overview, staff on events, and the travel system feed."""
import json
import uuid
from datetime import date, timedelta

import pytest

from services.service_security_policy import generate_scoped_outbound_token
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


def _travel_headers(scope_app="travel-management"):
    token = generate_scoped_outbound_token("marketing", ["events:read"], source_service=scope_app)
    return {"Authorization": f"Bearer {token}"}


# --- Staff ---


def test_add_staff_with_defaults(client, admin, event, colleague):
    response = _add(client, admin, event["id"], colleague["id"], departs_from="  Toronto (YYZ)  ")

    assert response.status_code == 201, response.text
    person = response.json()
    assert person["full_name"] == "Pytest Booth Staff"
    assert person["role"] == "BOOTH_STAFF"
    assert person["departs_from"] == "Toronto (YYZ)"
    # Arrive the day before it starts to set up, leave on the last day
    start = date.fromisoformat(event["start_date"])
    assert person["arrive_on"] == (start - timedelta(days=1)).isoformat()
    assert person["depart_on"] == event["end_date"]
    assert (person["needs_flight"], person["needs_hotel"], person["needs_ground_transport"], person["needs_stipend"]) == (True, True, True, True)
    assert person["readiness"] == "READY"
    assert person["missing"] == []

    audit = run_sql("SELECT action FROM audit_logs WHERE entity_type = 'EventStaff' AND entity_id = $1", person["id"])
    assert [a["action"] for a in audit] == ["CREATE"]


def test_missing_travel_details(client, admin, event, colleague):
    person = _add(client, admin, event["id"], colleague["id"]).json()
    # Flying, but nobody said from where
    assert person["readiness"] == "MISSING_DETAILS"
    assert person["missing"] == ["where they fly from"]

    fixed = client.patch(f"/api/event-staff/{person['id']}", json={"departs_from": "Vancouver"}, headers=admin["headers"]).json()
    assert fixed["readiness"] == "READY"


def test_local_staff_need_no_travel(client, admin, event, colleague):
    person = _add(client, admin, event["id"], colleague["id"], travel_needed=False, role="LEAD").json()
    assert person["readiness"] == "NOT_TRAVELLING"
    assert person["arrive_on"] is None
    assert person["missing"] == []


def test_same_person_twice(client, admin, event, colleague):
    _add(client, admin, event["id"], colleague["id"], departs_from="Toronto")
    response = _add(client, admin, event["id"], colleague["id"])
    assert response.status_code == 422
    assert response.json()["detail"] == "Pytest Booth Staff is already on this event."


def test_staff_rules(client, admin, ref, event, colleague):
    bad_dates = _add(client, admin, event["id"], colleague["id"], arrive_on="2026-11-05", depart_on="2026-11-01")
    assert bad_dates.status_code == 422
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
    person = _add(client, admin, event["id"], colleague["id"], departs_from="Toronto").json()

    updated = client.patch(
        f"/api/event-staff/{person['id']}",
        json={"role": "DEMO", "needs_hotel": False, "cargo_note": "2 demo drones in Pelican cases"},
        headers=admin["headers"],
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["role"], updated.json()["needs_hotel"], updated.json()["cargo_note"]) == ("DEMO", False, "2 demo drones in Pelican cases")
    audit = run_sql("SELECT new_value FROM audit_logs WHERE entity_type = 'EventStaff' AND entity_id = $1 AND action = 'UPDATE'", person["id"])
    assert set(json.loads(audit[0]["new_value"])) == {"initiative_id", "role", "needs_hotel", "cargo_note"}
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
    _add(client, admin, event["id"], colleague["id"])  # missing where they fly from
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
    assert (card["staff_count"], card["staff_ready"], card["staff_missing_details"]) == (1, 0, 1)
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


# --- Travel system ---


def test_travel_feed(client, admin, event, colleague):
    ready = _add(client, admin, event["id"], colleague["id"], departs_from="Toronto (YYZ)", cargo_note="Booth kit, 2 crates").json()

    response = client.get("/api/integrations/travel/events", headers=_travel_headers())

    assert response.status_code == 200, response.text
    feed = {e["event_code"]: e for e in response.json()}
    ours = feed[event["event_code"]]
    assert ours["city"] == "Las Vegas"
    assert ours["owner_email"]
    [person] = ours["staff"]
    assert person["assignment_id"] == ready["id"]
    assert person["email"] == colleague["email"]
    assert person["readiness"] == "READY"
    assert person["cargo_note"] == "Booth kit, 2 crates"


def test_travel_feed_leaves_out_drafts(client, admin, ref):
    """Nobody books travel for an event that's still being entered; it appears once it's finished."""
    draft = _event(client, admin, ref, date.today() + timedelta(days=30), status="DRAFT")

    codes = {e["event_code"] for e in client.get("/api/integrations/travel/events", headers=_travel_headers()).json()}
    assert draft["event_code"] not in codes

    client.patch(f"/api/initiatives/{draft['id']}", json={"status": "PLANNED"}, headers=admin["headers"])
    codes = {e["event_code"] for e in client.get("/api/integrations/travel/events", headers=_travel_headers()).json()}
    assert draft["event_code"] in codes


def test_travel_feed_leaves_out_people_who_dont_travel(client, admin, event, colleague):
    _add(client, admin, event["id"], colleague["id"], travel_needed=False)
    feed = {e["event_code"]: e for e in client.get("/api/integrations/travel/events", headers=_travel_headers()).json()}
    assert feed[event["event_code"]]["staff"] == []


def test_travel_feed_updated_since(client, admin, event):
    future = "2099-01-01T00:00:00Z"
    feed = client.get("/api/integrations/travel/events", params={"updated_since": future}, headers=_travel_headers()).json()
    assert event["event_code"] not in {e["event_code"] for e in feed}


def test_travel_feed_is_for_the_travel_system_only(client, viewer):
    assert client.get("/api/integrations/travel/events").status_code == 401
    # A regular person's sign-in isn't a travel system token (super admins keep root access, as everywhere)
    assert client.get("/api/integrations/travel/events", headers=viewer["headers"]).status_code == 403
    # Another trusted application without the scope
    assert client.get("/api/integrations/travel/events", headers=_travel_headers("finance")).status_code == 403


def test_travel_system_reports_booking_status(client, admin, event, colleague):
    person = _add(client, admin, event["id"], colleague["id"], departs_from="Toronto").json()
    token = generate_scoped_outbound_token("marketing", ["events:travel_status_write"], source_service="travel-management")

    response = client.post(
        "/api/integrations/travel/staff-status",
        json={"assignment_id": person["id"], "travel_status": "BOOKED"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204, response.text
    [staff] = client.get(f"/api/initiatives/{event['id']}/staff", headers=admin["headers"]).json()
    assert staff["readiness"] == "BOOKED"
    assert staff["travel_status_at"] is not None
    missing = client.post(
        "/api/integrations/travel/staff-status",
        json={"assignment_id": str(uuid.uuid4()), "travel_status": "BOOKED"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert missing.status_code == 404


def test_changing_travel_after_booking_clears_the_status(client, admin, event, colleague):
    person = _add(client, admin, event["id"], colleague["id"], departs_from="Toronto").json()
    run_sql("UPDATE event_staff SET travel_status = 'BOOKED', travel_status_at = now() WHERE id = $1", person["id"])

    # A note isn't something the travel system books from: still booked
    kept = client.patch(f"/api/event-staff/{person['id']}", json={"notes": "Bring badges"}, headers=admin["headers"]).json()
    assert kept["readiness"] == "BOOKED"

    moved = client.patch(f"/api/event-staff/{person['id']}", json={"departs_from": "Montreal"}, headers=admin["headers"]).json()
    assert moved["readiness"] == "READY"
    assert moved["travel_status_at"] is None


def test_travel_feed_returns_an_event_after_someone_is_removed(client, admin, event, colleague):
    person = _add(client, admin, event["id"], colleague["id"], departs_from="Toronto").json()
    # A naive time is taken as UTC (no 500)
    since = run_sql("SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') AS t")[0]["t"]

    assert client.delete(f"/api/event-staff/{person['id']}", headers=admin["headers"]).status_code == 204

    response = client.get("/api/integrations/travel/events", params={"updated_since": since}, headers=_travel_headers())
    assert response.status_code == 200, response.text
    feed = {e["event_code"]: e for e in response.json()}
    assert feed[event["event_code"]]["staff"] == []
