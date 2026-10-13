"""POST /api/initiatives — validation, permissions and audit logging."""
import pytest

from tests.conftest import run_sql


def _payload(ref, **overrides) -> dict:
    payload = {
        "name": "Pytest Expo",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "funnel_stage_id": ref["funnel_stages"]["CONVERSION"],
        "goal_id": ref["goals"]["DEMOS"],
        "primary_kpi_id": ref["primary_kpis"]["CP_SQL"],
        "start_date": "2026-12-01",
        "end_date": "2026-12-03",
        "budget_amount": 12500,
        "currency": "usd",
    }
    payload.update(overrides)
    return payload


def test_create_initiative(client, admin, ref):
    response = client.post("/api/initiatives", json=_payload(ref, name="  Pytest Expo  "), headers=admin["headers"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Pytest Expo"
    assert body["channel_name"] == "Events"
    assert body["sub_channel_name"] == "In-Person Events"
    assert body["goal_name"] == "Demos"
    assert body["currency"] == "USD"
    assert body["status"] == "PLANNED"
    # Owner defaults to the person creating it
    assert body["owner_user_id"] == str(admin["id"])

    audit = run_sql(
        "SELECT action, actor_user_id FROM audit_logs WHERE entity_type = 'Initiative' AND entity_id = $1",
        body["id"],
    )
    assert [(a["action"], a["actor_user_id"]) for a in audit] == [("CREATE", admin["id"])]


def test_create_initiative_minimal_fields(client, admin, ref):
    payload = {"name": "Pytest SEO", "brand_id": ref["brands"]["TBD_ALL"], "channel_id": ref["channels"]["SEO"]}

    response = client.post("/api/initiatives", json=payload, headers=admin["headers"])

    assert response.status_code == 201, response.text
    assert response.json()["sub_channel_id"] is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"sub_channel_id": "GOOGLE_ADS"}, "doesn't belong to the chosen channel"),
        ({"funnel_stage_id": "AWARENESS"}, "isn't allowed for the chosen funnel stage"),
        ({"funnel_stage_id": None}, "Choose a funnel stage before choosing a goal"),
        ({"brand_id": 999999}, "Unknown brand"),
        ({"start_date": "2026-12-05", "end_date": "2026-12-01"}, "End date can't be before the start date"),
        ({"budget_amount": -1}, "greater than or equal to 0"),
        ({"name": "   "}, "Name is required"),
        ({"currency": "DOLLARS"}, "string_pattern_mismatch"),
        ({"status": "DONE"}, "literal_error"),
    ],
)
def test_create_initiative_rejects_invalid_input(client, admin, ref, overrides, expected):
    # Codes in overrides are resolved to ids for the matching reference table
    lookup = {"sub_channel_id": "sub_channels", "funnel_stage_id": "funnel_stages"}
    resolved = {
        key: ref[lookup[key]][value] if key in lookup and isinstance(value, str) else value
        for key, value in overrides.items()
    }

    response = client.post("/api/initiatives", json=_payload(ref, **resolved), headers=admin["headers"])

    assert response.status_code == 422
    assert expected in response.text


def test_create_initiative_requires_manage_permission(client, viewer, ref):
    response = client.post("/api/initiatives", json=_payload(ref), headers=viewer["headers"])

    assert response.status_code == 403
    assert run_sql("SELECT 1 FROM initiatives WHERE created_by = $1", viewer["id"]) == []


def test_create_initiative_requires_login(client, ref):
    response = client.post("/api/initiatives", json=_payload(ref))

    assert response.status_code == 401


def test_owner_options(client, admin, viewer):
    response = client.get("/api/initiatives/owner-options", params={"search": "pytest"}, headers=admin["headers"])

    assert response.status_code == 200
    ids = {option["id"] for option in response.json()}
    assert {str(admin["id"]), str(viewer["id"])} <= ids


def test_owner_options_requires_manage_permission(client, viewer):
    response = client.get("/api/initiatives/owner-options", headers=viewer["headers"])

    assert response.status_code == 403
