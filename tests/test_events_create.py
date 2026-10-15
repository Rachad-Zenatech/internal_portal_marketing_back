"""POST /api/initiatives with event details — codes, required fields per sub-channel, audit."""
import json
import re

import pytest

from tests.conftest import run_sql

CODE_PATTERN = re.compile(r"^EVT-\d{4}-\d{4,}$")


def _event_payload(ref, sub_channel="IN_PERSON_EVENTS", **overrides) -> dict:
    payload = {
        "name": "Pytest Event",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"][sub_channel],
    }
    payload.update(overrides)
    return payload


def _details(initiative_id: str) -> dict | None:
    rows = run_sql(
        "SELECT e.event_code, e.venue, e.city, e.country_code, t.code AS event_type, r.code AS region "
        "FROM event_details e LEFT JOIN event_types t ON t.id = e.event_type_id "
        "LEFT JOIN regions r ON r.id = e.region_id WHERE e.initiative_id = $1",
        initiative_id,
    )
    return dict(rows[0]) if rows else None


@pytest.fixture(scope="module")
def event_refs():
    """Region and event type ids by code (not part of the shared ref fixture)."""
    return {
        "regions": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM regions")},
        "event_types": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM event_types")},
    }


def test_in_person_event_stores_details_and_code(client, admin, ref, event_refs):
    event = {
        "event_type_id": event_refs["event_types"]["TRADE_SHOW"],
        "venue": "  Las Vegas Convention Center ",
        "city": "Las Vegas",
        "country_code": "us",
        "region_id": event_refs["regions"]["NORTH_AMERICA"],
    }
    response = client.post("/api/initiatives", json=_event_payload(ref, event=event), headers=admin["headers"])

    assert response.status_code == 201, response.text
    details = _details(response.json()["id"])
    assert CODE_PATTERN.match(details["event_code"])
    assert details == {
        "event_code": details["event_code"],
        "venue": "Las Vegas Convention Center",
        "city": "Las Vegas",
        "country_code": "US",
        "event_type": "TRADE_SHOW",
        "region": "NORTH_AMERICA",
    }


def test_event_without_details_still_gets_a_code(client, admin, ref):
    response = client.post("/api/initiatives", json=_event_payload(ref, sub_channel="WEBINARS"), headers=admin["headers"])

    assert response.status_code == 201, response.text
    assert CODE_PATTERN.match(_details(response.json()["id"])["event_code"])


def test_events_get_different_codes(client, admin, ref):
    codes = []
    for name in ("Pytest Event A", "Pytest Event B"):
        response = client.post(
            "/api/initiatives", json=_event_payload(ref, sub_channel="VIRTUAL_EVENTS", name=name), headers=admin["headers"]
        )
        assert response.status_code == 201, response.text
        codes.append(_details(response.json()["id"])["event_code"])

    assert codes[0] != codes[1]


@pytest.mark.parametrize(
    ("sub_channel", "event", "expected_status", "expected_text"),
    [
        ("IN_PERSON_EVENTS", None, 422, "In-person events need a city and a country."),
        ("IN_PERSON_EVENTS", {"city": "Las Vegas"}, 422, "In-person events need a city and a country."),
        ("IN_PERSON_EVENTS", {"city": "  ", "country_code": "US"}, 422, "In-person events need a city and a country."),
        ("SPONSORSHIPS", {"city": "Dubai"}, 422, "Sponsorships need a country."),
        ("SPONSORSHIPS", {"country_code": "AE"}, 201, None),
        ("WEBINARS", None, 201, None),
        ("VIRTUAL_EVENTS", {}, 201, None),
    ],
)
def test_required_event_fields_by_sub_channel(client, admin, ref, sub_channel, event, expected_status, expected_text):
    payload = _event_payload(ref, sub_channel=sub_channel)
    if event is not None:
        payload["event"] = event

    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])

    assert response.status_code == expected_status, response.text
    if expected_text:
        assert response.json()["detail"] == expected_text


def test_event_details_rejected_outside_events_channel(client, admin, ref):
    payload = {
        "name": "Pytest Search",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["PAID_SEARCH"],
        "sub_channel_id": ref["sub_channels"]["GOOGLE_ADS"],
        "event": {"city": "Las Vegas"},
    }

    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])

    assert response.status_code == 422
    assert response.json()["detail"] == "Event details can only be added to initiatives in the Events channel."
    assert run_sql("SELECT 1 FROM initiatives WHERE created_by = $1", admin["id"]) == []


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"region_id": 999999}, "Unknown region."),
        ({"event_type_id": 999999}, "Unknown event type."),
        ({"country_code": "USA"}, "Country must be a two-letter code"),
        ({"country_code": "1A"}, "Country must be a two-letter code"),
    ],
)
def test_invalid_event_references_are_rejected(client, admin, ref, event, expected):
    payload = _event_payload(ref, sub_channel="VIRTUAL_EVENTS", event=event)

    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])

    assert response.status_code == 422
    assert expected in response.text
    # Nothing half-saved: the initiative was rolled back with its details
    assert run_sql("SELECT 1 FROM initiatives WHERE created_by = $1", admin["id"]) == []


def test_audit_entry_includes_event_code(client, admin, ref):
    response = client.post(
        "/api/initiatives",
        json=_event_payload(ref, event={"city": "Las Vegas", "country_code": "US"}),
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    initiative_id = response.json()["id"]

    audit = run_sql(
        "SELECT new_value FROM audit_logs WHERE entity_type = 'Initiative' AND entity_id = $1 AND action = 'CREATE'",
        initiative_id,
    )
    logged_event = json.loads(audit[0]["new_value"])["event"]
    assert logged_event["event_code"] == _details(initiative_id)["event_code"]
    assert logged_event["city"] == "Las Vegas"


def test_webinar_stores_platform_and_link(client, admin, ref):
    event = {"platform": " Zoom ", "event_url": "https://zoom.us/j/123456"}

    response = client.post("/api/initiatives", json=_event_payload(ref, sub_channel="WEBINARS", event=event), headers=admin["headers"])

    assert response.status_code == 201, response.text
    assert (response.json()["platform"], response.json()["event_url"]) == ("Zoom", "https://zoom.us/j/123456")


@pytest.mark.parametrize("url", ["zoom.us/j/123", "ftp://example.com/event", "javascript:alert(1)"])
def test_event_link_must_be_http(client, admin, ref, url):
    payload = _event_payload(ref, sub_channel="VIRTUAL_EVENTS", event={"event_url": url})

    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])

    assert response.status_code == 422
    assert "Event link must start with http:// or https://" in response.text


def test_blank_event_link_is_saved_as_empty(client, admin, ref):
    payload = _event_payload(ref, sub_channel="WEBINARS", event={"platform": "", "event_url": "   "})

    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])

    assert response.status_code == 201, response.text
    assert (response.json()["platform"], response.json()["event_url"]) == (None, None)


@pytest.mark.parametrize(
    ("sub_channel", "event"),
    [
        ("IN_PERSON_EVENTS", {"city": "Las Vegas", "country_code": "US", "platform": "Zoom"}),
        ("SPONSORSHIPS", {"country_code": "AE", "event_url": "https://example.com/live"}),
    ],
)
def test_platform_and_link_rejected_for_in_person_events(client, admin, ref, sub_channel, event):
    response = client.post("/api/initiatives", json=_event_payload(ref, sub_channel=sub_channel, event=event), headers=admin["headers"])

    assert response.status_code == 422
    assert response.json()["detail"] == "Platform and event link are for virtual events."
    assert run_sql("SELECT 1 FROM initiatives WHERE created_by = $1", admin["id"]) == []


def test_virtual_event_may_have_a_location(client, admin, ref):
    event = {"platform": "Teams", "city": "Toronto", "country_code": "CA"}

    response = client.post("/api/initiatives", json=_event_payload(ref, sub_channel="WEBINARS", event=event), headers=admin["headers"])

    assert response.status_code == 201, response.text
    assert response.json()["city"] == "Toronto"


def test_database_rejects_non_http_link(client, admin, ref):
    created = client.post("/api/initiatives", json=_event_payload(ref, sub_channel="WEBINARS"), headers=admin["headers"])
    assert created.status_code == 201, created.text

    # The API validates first; the database rule is the backstop for anything that skips it
    with pytest.raises(Exception, match="event_details_event_url_check"):
        run_sql("UPDATE event_details SET event_url = 'zoom.us/j/1' WHERE initiative_id = $1", created.json()["id"])


def test_in_person_event_stores_booth_size(client, admin, ref):
    event = {"city": "Las Vegas", "country_code": "US", "booth_size_sqm": 18.58}

    response = client.post("/api/initiatives", json=_event_payload(ref, event=event), headers=admin["headers"])

    assert response.status_code == 201, response.text
    assert response.json()["booth_size_sqm"] == 18.58


@pytest.mark.parametrize("sub_channel", ["WEBINARS", "VIRTUAL_EVENTS"])
def test_booth_size_rejected_for_virtual_events(client, admin, ref, sub_channel):
    payload = _event_payload(ref, sub_channel=sub_channel, event={"booth_size_sqm": 18.6})

    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])

    assert response.status_code == 422
    assert response.json()["detail"] == "Booth size is for in-person events."


@pytest.mark.parametrize("size", [0, -5])
def test_booth_size_must_be_positive(client, admin, ref, size):
    payload = _event_payload(ref, event={"city": "Las Vegas", "country_code": "US", "booth_size_sqm": size})

    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])

    assert response.status_code == 422


# --- Drafts ---


def test_draft_event_can_skip_required_fields(client, admin, ref):
    """Saved part-way: an in-person event with no city or country yet still gets its details row and code."""
    response = client.post("/api/initiatives", json=_event_payload(ref, status="DRAFT"), headers=admin["headers"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "DRAFT"
    assert CODE_PATTERN.match(body["event_code"])


def test_finishing_a_draft_applies_the_event_rules(client, admin, ref):
    draft = client.post("/api/initiatives", json=_event_payload(ref, status="DRAFT"), headers=admin["headers"]).json()
    url = f"/api/initiatives/{draft['id']}"

    # Still a draft: other edits are fine without a city
    assert client.patch(url, json={"name": "Pytest Event (draft)"}, headers=admin["headers"]).status_code == 200

    missing = client.patch(url, json={"status": "PLANNED"}, headers=admin["headers"])
    assert missing.status_code == 422
    assert missing.json()["detail"] == "In-person events need a city and a country."

    finished = client.patch(
        url, json={"status": "PLANNED", "event": {"city": "Las Vegas", "country_code": "US"}}, headers=admin["headers"]
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "PLANNED"


def test_nothing_goes_back_to_draft(client, admin, ref):
    created = client.post(
        "/api/initiatives", json=_event_payload(ref, event={"city": "Las Vegas", "country_code": "US"}), headers=admin["headers"]
    ).json()

    response = client.patch(f"/api/initiatives/{created['id']}", json={"status": "DRAFT"}, headers=admin["headers"])

    assert response.status_code == 422
    assert response.json()["detail"] == "An initiative can't go from Planned to Draft."


def test_drafts_can_be_listed(client, admin, ref):
    created = client.post("/api/initiatives", json=_event_payload(ref, status="DRAFT"), headers=admin["headers"]).json()

    listed = client.get("/api/initiatives", params={"status": "DRAFT", "limit": 200}, headers=admin["headers"]).json()

    assert created["id"] in {item["id"] for item in listed["items"]}
    assert {item["status"] for item in listed["items"]} == {"DRAFT"}
