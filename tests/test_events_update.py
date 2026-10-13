"""Event details on GET, list search and PATCH — partial edits, audit, sub-channel rules, channel moves."""
import json
import re

import pytest

from tests.conftest import run_sql

CODE_PATTERN = re.compile(r"^EVT-\d{4}-\d{4,}$")


@pytest.fixture(scope="module")
def event_refs():
    return {
        "regions": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM regions")},
        "event_types": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM event_types")},
    }


def _create(client, admin, ref, *, channel="EVENTS", sub_channel="WEBINARS", event=None, name="Pytest Event Edit"):
    payload = {
        "name": name,
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"][channel],
        "sub_channel_id": ref["sub_channels"][sub_channel],
    }
    if event is not None:
        payload["event"] = event
    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def _patch(client, user, initiative_id, payload):
    return client.patch(f"/api/initiatives/{initiative_id}", json=payload, headers=user["headers"])


def _update_audits(initiative_id):
    return run_sql(
        "SELECT old_value, new_value FROM audit_logs WHERE entity_type = 'Initiative' AND entity_id = $1 AND action = 'UPDATE'",
        initiative_id,
    )


def test_get_returns_event_fields(client, admin, ref, event_refs):
    created = _create(
        client, admin, ref, sub_channel="IN_PERSON_EVENTS",
        event={"event_type_id": event_refs["event_types"]["TRADE_SHOW"], "venue": "LVCC", "city": "Las Vegas",
               "country_code": "US", "region_id": event_refs["regions"]["NORTH_AMERICA"]},
    )

    body = client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).json()

    assert CODE_PATTERN.match(body["event_code"])
    assert created["event_code"] == body["event_code"]  # the create response includes it too
    assert (body["event_type_name"], body["venue"], body["city"], body["country_code"], body["region_name"]) == (
        "Trade show", "LVCC", "Las Vegas", "US", "North America"
    )


def test_non_event_initiatives_have_null_event_fields(client, admin, ref):
    created = _create(client, admin, ref, channel="PAID_SEARCH", sub_channel="GOOGLE_ADS", name="Pytest Search")

    body = client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).json()

    assert all(body[f] is None for f in ("event_code", "event_type_id", "venue", "city", "country_code", "region_id"))


def test_list_search_finds_event_by_code(client, admin, ref):
    created = _create(client, admin, ref)

    response = client.get("/api/initiatives", params={"search": created["event_code"]}, headers=admin["headers"])

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [created["id"]]
    assert response.json()["total"] == 1


def test_partial_event_update_changes_and_audits_only_sent_fields(client, admin, ref):
    created = _create(client, admin, ref, sub_channel="SPONSORSHIPS", event={"city": "Dubai", "country_code": "AE"})

    response = _patch(client, admin, created["id"], {"name": "Pytest Sponsorship", "event": {"city": "Abu Dhabi"}})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["name"], body["city"], body["country_code"]) == ("Pytest Sponsorship", "Abu Dhabi", "AE")
    assert body["event_code"] == created["event_code"]

    audits = _update_audits(created["id"])
    assert len(audits) == 1  # one audit row per request
    assert json.loads(audits[0]["old_value"]) == {"name": "Pytest Event Edit", "event": {"city": "Dubai"}}
    assert json.loads(audits[0]["new_value"]) == {
        "name": "Pytest Sponsorship",
        "event": {"city": "Abu Dhabi", "event_code": created["event_code"]},
    }


def test_unchanged_event_fields_are_not_audited(client, admin, ref):
    created = _create(client, admin, ref, sub_channel="SPONSORSHIPS", event={"country_code": "AE"})

    response = _patch(client, admin, created["id"], {"event": {"country_code": "ae"}})

    assert response.status_code == 200
    assert _update_audits(created["id"]) == []


def test_event_code_cannot_be_edited(client, admin, ref):
    created = _create(client, admin, ref)

    response = _patch(client, admin, created["id"], {"event": {"event_code": "EVT-2026-9999"}})

    assert response.status_code == 422
    assert client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).json()["event_code"] == created["event_code"]


def test_required_fields_apply_after_sub_channel_change(client, admin, ref):
    created = _create(client, admin, ref, sub_channel="WEBINARS")
    in_person = ref["sub_channels"]["IN_PERSON_EVENTS"]

    rejected = _patch(client, admin, created["id"], {"sub_channel_id": in_person})
    accepted = _patch(
        client, admin, created["id"],
        {"sub_channel_id": in_person, "event": {"city": "Toronto", "country_code": "CA"}},
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "In-person events need a city and a country."
    assert accepted.status_code == 200, accepted.text
    assert (accepted.json()["sub_channel_name"], accepted.json()["city"]) == ("In-Person Events", "Toronto")


def test_clearing_a_required_field_is_rejected(client, admin, ref):
    created = _create(client, admin, ref, sub_channel="IN_PERSON_EVENTS", event={"city": "Paris", "country_code": "FR"})

    response = _patch(client, admin, created["id"], {"event": {"city": None}})

    assert response.status_code == 422
    assert response.json()["detail"] == "In-person events need a city and a country."


def test_moving_out_of_events_is_blocked(client, admin, ref):
    created = _create(client, admin, ref)

    response = _patch(
        client, admin, created["id"],
        {"channel_id": ref["channels"]["PAID_SEARCH"], "sub_channel_id": ref["sub_channels"]["GOOGLE_ADS"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Remove this initiative's event details before moving it to another channel."
    after = client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).json()
    assert (after["channel_name"], after["event_code"]) == ("Events", created["event_code"])


def test_moving_into_events_creates_a_code(client, admin, ref):
    created = _create(client, admin, ref, channel="PAID_SEARCH", sub_channel="GOOGLE_ADS", name="Pytest Search")

    response = _patch(
        client, admin, created["id"],
        {"channel_id": ref["channels"]["EVENTS"], "sub_channel_id": ref["sub_channels"]["VIRTUAL_EVENTS"]},
    )

    assert response.status_code == 200, response.text
    assert CODE_PATTERN.match(response.json()["event_code"])
    audit = json.loads(_update_audits(created["id"])[0]["new_value"])
    assert audit["event"]["event_code"] == response.json()["event_code"]


def test_event_details_rejected_on_non_event_update(client, admin, ref):
    created = _create(client, admin, ref, channel="PAID_SEARCH", sub_channel="GOOGLE_ADS", name="Pytest Search")

    response = _patch(client, admin, created["id"], {"event": {"city": "Las Vegas"}})

    assert response.status_code == 422
    assert response.json()["detail"] == "Event details can only be added to initiatives in the Events channel."


def test_viewer_cannot_patch_event_details(client, admin, viewer, ref):
    created = _create(client, admin, ref, sub_channel="SPONSORSHIPS", event={"city": "Dubai", "country_code": "AE"})

    response = _patch(client, viewer, created["id"], {"event": {"city": "Hijacked"}})

    assert response.status_code == 403
    assert client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).json()["city"] == "Dubai"
