"""GET and PATCH /api/initiatives/{id} — detail, edits, status rules, permissions and audit logging."""
import json
import uuid

import pytest

from tests.conftest import run_sql


@pytest.fixture
def initiative(client, admin, ref):
    """An Events initiative created by the temporary admin (removed with the admin)."""
    payload = {
        "name": "Pytest Edit Expo",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "start_date": "2026-12-01",
        "end_date": "2026-12-03",
        "budget_amount": 10000,
        "event": {"city": "Las Vegas", "country_code": "US"},
    }
    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def test_get_initiative(client, admin, initiative):
    response = client.get(f"/api/initiatives/{initiative['id']}", headers=admin["headers"])

    assert response.status_code == 200
    assert response.json()["name"] == "Pytest Edit Expo"


def test_get_missing_initiative(client, admin):
    response = client.get(f"/api/initiatives/{uuid.uuid4()}", headers=admin["headers"])

    assert response.status_code == 404


def test_update_changes_only_sent_fields_and_audits(client, admin, initiative):
    response = client.patch(
        f"/api/initiatives/{initiative['id']}",
        json={"name": "Pytest Edit Expo (renamed)", "budget_amount": 12500, "end_date": "2026-12-03"},
        headers=admin["headers"],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Pytest Edit Expo (renamed)"
    assert body["budget_amount"] == 12500
    assert body["sub_channel_name"] == "In-Person Events"  # untouched

    audit = run_sql(
        "SELECT old_value, new_value FROM audit_logs WHERE entity_type = 'Initiative' AND entity_id = $1 AND action = 'UPDATE'",
        initiative["id"],
    )
    assert len(audit) == 1
    # end_date was sent unchanged, so only the real changes are recorded
    assert json.loads(audit[0]["old_value"]) == {"name": "Pytest Edit Expo", "budget_amount": 10000.0}
    assert json.loads(audit[0]["new_value"]) == {"name": "Pytest Edit Expo (renamed)", "budget_amount": 12500.0}


def test_update_with_no_changes_is_not_audited(client, admin, initiative):
    response = client.patch(f"/api/initiatives/{initiative['id']}", json={"name": "Pytest Edit Expo"}, headers=admin["headers"])

    assert response.status_code == 200
    assert run_sql(
        "SELECT 1 FROM audit_logs WHERE entity_id = $1 AND action = 'UPDATE'", initiative["id"]
    ) == []


def test_update_clears_optional_field(client, admin, initiative):
    response = client.patch(f"/api/initiatives/{initiative['id']}", json={"end_date": None}, headers=admin["headers"])

    assert response.status_code == 200
    assert response.json()["end_date"] is None


@pytest.mark.parametrize(
    ("steps", "allowed"),
    [
        (["ACTIVE", "PAUSED", "ACTIVE", "COMPLETED"], True),
        (["CANCELLED", "PLANNED"], True),
        (["COMPLETED"], False),  # Planned can't jump straight to Completed
        (["ACTIVE", "PLANNED"], False),
    ],
)
def test_status_transitions(client, admin, initiative, steps, allowed):
    responses = [
        client.patch(f"/api/initiatives/{initiative['id']}", json={"status": step}, headers=admin["headers"])
        for step in steps
    ]

    if allowed:
        assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    else:
        assert responses[-1].status_code == 422
        assert "can't go from" in responses[-1].text


def test_update_rejects_sub_channel_from_another_channel(client, admin, ref):
    # Not an event: events can't leave the Events channel at all (see test_events_update.py)
    created = client.post(
        "/api/initiatives",
        json={
            "name": "Pytest Search",
            "brand_id": ref["brands"]["DESKFLEX"],
            "channel_id": ref["channels"]["PAID_SEARCH"],
            "sub_channel_id": ref["sub_channels"]["GOOGLE_ADS"],
            "budget_amount": 0,
        },
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text

    response = client.patch(
        f"/api/initiatives/{created.json()['id']}",
        json={"channel_id": ref["channels"]["PAID_SOCIAL"]},  # keeps the Google Ads sub-channel
        headers=admin["headers"],
    )

    assert response.status_code == 422
    assert "doesn't belong to the chosen channel" in response.text


@pytest.mark.parametrize("payload", [{"name": None}, {"name": "  "}, {"brand_id": None}, {"status": None}])
def test_update_rejects_clearing_required_fields(client, admin, initiative, payload):
    response = client.patch(f"/api/initiatives/{initiative['id']}", json=payload, headers=admin["headers"])

    assert response.status_code == 422


def test_update_requires_update_permission(client, viewer, initiative):
    response = client.patch(f"/api/initiatives/{initiative['id']}", json={"name": "Hijacked"}, headers=viewer["headers"])

    assert response.status_code == 403
    assert run_sql("SELECT name FROM initiatives WHERE id = $1", initiative["id"])[0]["name"] == "Pytest Edit Expo"


def test_get_requires_view_permission(client, viewer, initiative):
    response = client.get(f"/api/initiatives/{initiative['id']}", headers=viewer["headers"])

    assert response.status_code == 403


def test_update_missing_initiative(client, admin):
    response = client.patch(f"/api/initiatives/{uuid.uuid4()}", json={"name": "Nope"}, headers=admin["headers"])

    assert response.status_code == 404
