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
