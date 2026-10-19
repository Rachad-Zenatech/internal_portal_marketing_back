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
    assert len(categories) == 36  # the cost classification's 35, plus Demo equipment (demo log)
    assert {"expense_type_id", "applies_to", "manual_entry_allowed", "default_cost_type", "help_text"} <= categories[0].keys()


def test_every_category_has_an_expense_type(cost_lists):
    expense_types, categories = cost_lists
    type_ids = {t["id"] for t in expense_types}

    assert all(c["expense_type_id"] in type_ids for c in categories)
    # and every expense type is used by at least one category
    assert type_ids == {c["expense_type_id"] for c in categories}


def test_travel_categories_can_be_entered_by_hand(cost_lists):
    expense_types, categories = cost_lists
    travel_id = next(t["id"] for t in expense_types if t["code"] == "TRAVEL_ACCOMMODATION")
    travel = [c for c in categories if c["expense_type_id"] == travel_id]

    assert [c["code"] for c in travel] == ["FLIGHTS", "HOTELS", "GROUND_TRANSPORT", "PER_DIEMS_MEALS", "OTHER_TRAVEL"]
    assert all(c["help_text"] == "Navan adds flights, hotels and cars booked there. Enter anything else here." for c in travel)
    # every category can be entered by hand
    assert all(c["manual_entry_allowed"] for c in categories)


def test_category_defaults(cost_lists):
    _, categories = cost_lists
    by_code = {c["code"]: c for c in categories}

    assert by_code["BOOTH_BUILD"]["default_cost_type"] == "CAPEX"
    assert by_code["BOOTH_SPACE"]["default_cost_type"] == "OPEX"
    assert by_code["MEDIA_ADS"]["applies_to"] == "ADS"
    assert by_code["STAFF_TIME"]["applies_to"] == "ALL"
    assert by_code["FREIGHT"]["applies_to"] == "EVENTS"


CONTRACT_KPIS = {
    "COST_PER_LEAD": "MONEY", "COST_PER_MQL": "MONEY", "CAC": "MONEY", "ROI": "PERCENT", "LEADS": "COUNT",
    "MQLS": "COUNT", "SQLS": "COUNT", "DEMOS": "COUNT", "CUSTOMERS": "COUNT", "CONVERSIONS": "COUNT",
    "CONVERSION_RATE": "PERCENT", "CTR": "PERCENT", "CPC": "MONEY", "IMPRESSIONS": "COUNT", "REACH": "COUNT",
    "ENGAGEMENT_RATE": "PERCENT", "WEBSITE_TRAFFIC": "COUNT", "ORGANIC_TRAFFIC": "COUNT", "PAGE_VIEWS": "COUNT",
    "OPPORTUNITIES": "COUNT", "EMAIL_OPEN_RATE": "PERCENT", "EMAIL_CLICK_RATE": "PERCENT", "FOLLOWERS": "COUNT",
}


def test_primary_kpis_have_units(client, viewer):
    kpis = client.get("/api/reference-data", headers=viewer["headers"]).json()["primary_kpis"]

    assert {k["code"]: k["unit"] for k in kpis} == CONTRACT_KPIS
    assert [k["sort_order"] for k in kpis] == sorted(k["sort_order"] for k in kpis)
    assert all(k["name"] and isinstance(k["id"], int) for k in kpis)


def test_choices_for_the_wizard(client, viewer):
    choices = client.get("/api/reference-data", headers=viewer["headers"]).json()["choices"]

    codes = {field: [c["code"] for c in options] for field, options in choices.items()}
    assert codes["campaign_objective"] == ["AWARENESS", "TRAFFIC", "LEADS", "CONVERSIONS", "ENGAGEMENT", "VIDEO_VIEWS"]
    assert codes["focus_area"] == ["TECHNICAL", "CONTENT", "LINK_BUILDING", "LOCAL", "OTHER"]
    assert codes["content_type"] == ["BLOG_POST", "CASE_STUDY", "WHITEPAPER", "EBOOK", "VIDEO", "WEBINAR", "INFOGRAPHIC", "OTHER"]
    assert codes["email_platform"] == ["HUBSPOT", "MAILCHIMP", "OTHER"]
    assert codes["partnership_type"] == ["REFERRAL", "RESELLER", "CO_MARKETING", "SPONSORSHIP", "TECHNOLOGY", "OTHER"]
    assert codes["link_system"] == ["HUBSPOT", "GOOGLE_ADS", "MICROSOFT_ADS", "LINKEDIN_ADS", "META_ADS"]
    assert codes["data_source"] == ["LINKED", "MANUAL", "LATER"]
    assert choices["revenue_basis"] == [{"code": "CLOSED_REVENUE", "name": "Closed revenue"}]
    assert all(c["name"] for options in choices.values() for c in options)


def test_organic_social_channel(client, viewer):
    body = client.get("/api/reference-data", headers=viewer["headers"]).json()
    channels = body["channels"]
    subs = {s["id"]: s for s in body["sub_channels"]}

    codes = [c["code"] for c in channels]
    assert codes.index("ORGANIC_SOCIAL") == codes.index("PAID_SOCIAL") + 1
    organic = next(c for c in channels if c["code"] == "ORGANIC_SOCIAL")
    assert organic["name"] == "Organic Social"
    assert [subs[i]["name"] for i in organic["sub_channel_ids"]] == [
        "LinkedIn", "Instagram", "YouTube", "Facebook", "X (Twitter)", "TikTok", "Other",
    ]
    assert organic["sub_channel_required"] is True
    assert next(c for c in channels if c["code"] == "OTHER")["sub_channel_required"] is False

    # Each platform pre-fills engagement with a 90-day window; the shared Other has no default
    defaults = {d["sub_channel_id"]: d for d in body["evaluation_defaults"]}
    stage = next(s["id"] for s in body["funnel_stages"] if s["code"] == "AWARENESS")
    goal = next(g["id"] for g in body["goals"] if g["code"] == "ENGAGEMENT")
    platforms = [i for i in organic["sub_channel_ids"] if subs[i]["code"] != "OTHER"]
    assert all((defaults[i]["funnel_stage_id"], defaults[i]["goal_id"], defaults[i]["maturity_days"]) == (stage, goal, 90) for i in platforms)
    assert "ORGANIC_SOCIAL" in body["result_metrics_by_channel"]
