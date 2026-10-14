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


EXPENSE_TYPES = [
    "SPACE_SPONSORSHIP", "BOOTH_EXHIBIT", "SHOW_SERVICES", "SHIPPING_LOGISTICS", "MARKETING_PROMOTION",
    "REGISTRATION_HOSPITALITY", "TRAVEL_ACCOMMODATION", "PEOPLE_OTHER", "MEDIA",
]


@pytest.fixture
def cost_lists(client, admin):
    response = client.get("/api/reference-data", headers=admin["headers"])
    assert response.status_code == 200
    body = response.json()
    return body["expense_types"], body["spend_categories"]


def test_reference_data_includes_cost_lists(cost_lists):
    expense_types, categories = cost_lists

    assert [t["code"] for t in expense_types] == EXPENSE_TYPES  # in display order
    assert {t["code"]: t["source"] for t in expense_types}["TRAVEL_ACCOMMODATION"] == "TRAVEL_SYSTEM"
    assert len(categories) == 35
    assert {"expense_type_id", "applies_to", "manual_entry_allowed", "default_cost_type", "help_text"} <= categories[0].keys()


def test_every_category_has_an_expense_type(cost_lists):
    expense_types, categories = cost_lists
    type_ids = {t["id"] for t in expense_types}

    assert all(c["expense_type_id"] in type_ids for c in categories)
    # and every expense type is used by at least one category
    assert type_ids == {c["expense_type_id"] for c in categories}


def test_travel_categories_cannot_be_entered_by_hand(cost_lists):
    expense_types, categories = cost_lists
    travel_id = next(t["id"] for t in expense_types if t["code"] == "TRAVEL_ACCOMMODATION")
    travel = [c for c in categories if c["expense_type_id"] == travel_id]

    assert [c["code"] for c in travel] == ["FLIGHTS", "HOTELS", "GROUND_TRANSPORT", "PER_DIEMS_MEALS", "OTHER_TRAVEL"]
    assert all(c["manual_entry_allowed"] is False for c in travel)
    assert all(c["help_text"] == "Comes from the travel management system" for c in travel)
    # every other category can be entered by hand
    assert all(c["manual_entry_allowed"] for c in categories if c["expense_type_id"] != travel_id)


def test_category_defaults(cost_lists):
    _, categories = cost_lists
    by_code = {c["code"]: c for c in categories}

    assert by_code["BOOTH_BUILD"]["default_cost_type"] == "CAPEX"
    assert by_code["BOOTH_SPACE"]["default_cost_type"] == "OPEX"
    assert by_code["MEDIA_ADS"]["applies_to"] == "ADS"
    assert by_code["STAFF_TIME"]["applies_to"] == "ALL"
    assert by_code["FREIGHT"]["applies_to"] == "EVENTS"
