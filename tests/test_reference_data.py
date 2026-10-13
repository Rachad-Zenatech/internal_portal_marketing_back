"""GET /api/reference-data — the lookup lists behind every dropdown."""
import pytest

REGIONS = ["NORTH_AMERICA", "UK_EUROPE", "AUSTRALIA_NZ", "MIDDLE_EAST", "ASIA", "OTHER"]
EVENT_TYPES = ["TRADE_SHOW", "CONFERENCE", "ROADSHOW", "CUSTOMER_EVENT", "PARTNER_EVENT", "OTHER"]


@pytest.mark.parametrize(("key", "codes"), [("regions", REGIONS), ("event_types", EVENT_TYPES)])
def test_reference_data_includes_event_lists(client, admin, key, codes):
    response = client.get("/api/reference-data", headers=admin["headers"])

    assert response.status_code == 200
    items = response.json()[key]
    assert [item["code"] for item in items] == codes  # in display order
    assert all(item["name"] and isinstance(item["id"], int) for item in items)


def test_reference_data_needs_only_sign_in(client, viewer):
    # A regular user with no Marketing permissions can still load the dropdown lists
    response = client.get("/api/reference-data", headers=viewer["headers"])

    assert response.status_code == 200
    body = response.json()
    assert [item["code"] for item in body["regions"]] == REGIONS
    assert [item["code"] for item in body["event_types"]] == EVENT_TYPES


def test_reference_data_requires_login(client):
    assert client.get("/api/reference-data").status_code == 401
