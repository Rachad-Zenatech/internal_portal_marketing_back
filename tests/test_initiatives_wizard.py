"""The Create Initiative wizard's fields (channel details, KPI, data source, links, evaluation), the list's new
columns and filters, the summary cards and the recent activity feed."""
import json
import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import clear_budget, run_sql


@pytest.fixture(scope="module")
def kpis():
    return {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM primary_kpis")}


@pytest.fixture(scope="module")
def categories():
    return {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM spend_categories")}


@pytest.fixture
def tag():
    return f"Pytest Wizard {uuid.uuid4().hex[:8]}"


def _payload(ref, channel="PAID_SEARCH", sub_channel="GOOGLE_ADS", **overrides) -> dict:
    payload = {
        "name": "Pytest Wizard",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"][channel],
        "sub_channel_id": ref["sub_channels"][sub_channel] if sub_channel else None,
        "budget_amount": 5000,
        "currency": "USD",
    }
    payload.update(overrides)
    return payload


def _create(client, user, payload, expect=201):
    response = client.post("/api/initiatives", json=payload, headers=user["headers"])
    assert response.status_code == expect, response.text
    return response.json()


def _patch(client, user, initiative_id, body):
    return client.patch(f"/api/initiatives/{initiative_id}", json=body, headers=user["headers"])


def _audits(initiative_id, entity_type="Initiative"):
    rows = run_sql(
        "SELECT action, old_value, new_value FROM audit_logs WHERE entity_type = $1 AND entity_id = $2 ORDER BY created_at",
        entity_type, str(initiative_id),
    )
    return [(r["action"], json.loads(r["old_value"]) if r["old_value"] else None,
             json.loads(r["new_value"]) if r["new_value"] else None) for r in rows]


# --- Channel details ---

VALID_DETAILS = [
    ("PAID_SEARCH", "GOOGLE_ADS", {"ad_account_id": "123-456-7890", "campaign_objective": "LEADS"}),
    ("PAID_SOCIAL", "LINKEDIN_ADS", {"ad_account_id": "li-77", "campaign_objective": "VIDEO_VIEWS"}),
    ("ORGANIC_SOCIAL", "LINKEDIN_ORGANIC", {"profile_account": "DeskFlex company page", "content_group": "Customer stories"}),
    ("SEO", "ORGANIC_SEARCH", {"website": "deskflex.com", "focus_area": "LINK_BUILDING"}),
    ("SEO", "BLOG", {"website": "https://deskflex.com/blog", "focus_area": "CONTENT"}),
    ("CONTENT_MARKETING", "BLOG", {"content_type": "CASE_STUDY", "distribution_channel": "Newsletter"}),
    ("EMAIL_MARKETING", "EMAIL_NEWSLETTER", {"email_platform": "HUBSPOT", "campaign_or_workflow": "October newsletter"}),
    ("MARKETING_AUTOMATION", "EMAIL_NURTURE", {"email_platform": "MAILCHIMP", "campaign_or_workflow": "Trial nurture"}),
    ("PARTNERSHIPS", "RESELLERS", {"partner_name": "Acme Resellers", "partnership_type": "RESELLER"}),
    ("AFFILIATES", "REFERRALS", {"partner_name": "Desk Deals", "partnership_type": "REFERRAL"}),
    ("PR_COMMUNICATIONS", "REVIEW_SITES", {}),
]


@pytest.mark.parametrize(("channel", "sub_channel", "details"), VALID_DETAILS)
def test_channel_details_are_saved_per_channel(client, admin, ref, channel, sub_channel, details):
    created = _create(client, admin, _payload(ref, channel, sub_channel, channel_details=details))
    assert created["channel_details"] == details

    # PATCH replaces the whole set; blanks are left out
    first_key = next(iter(details), None)
    if first_key:
        response = _patch(client, admin, created["id"], {"channel_details": {first_key: details[first_key], **dict.fromkeys(list(details)[1:], " ")}})
        assert response.status_code == 200, response.text
        assert response.json()["channel_details"] == {first_key: details[first_key]}
    assert client.get(f"/api/initiatives/{created['id']}", headers=admin["headers"]).json()["channel_details"] == (
        {first_key: details[first_key]} if first_key else {}
    )


REJECTED_DETAILS = [
    ("PAID_SEARCH", "GOOGLE_ADS", {"website": "x.com"}, "Paid Search initiatives don't have a field called website."),
    ("PAID_SOCIAL", "META_ADS", {"campaign_objective": "SALES"},
     "Campaign objective must be one of: Awareness, Traffic, Leads, Conversions, Engagement, Video views."),
    ("ORGANIC_SOCIAL", "INSTAGRAM_ORGANIC", {"ad_account_id": "1"}, "Organic Social initiatives don't have a field called ad_account_id."),
    ("SEO", "ORGANIC_SEARCH", {"focus_area": "PAID"}, "Focus area must be one of: Technical, Content, Link building, Local, Other."),
    ("SEO", "ORGANIC_SEARCH", {"website": "http://deskflex.com"}, "Website must be a domain (e.g. example.com) or a link starting with https://"),
    ("CONTENT_MARKETING", "BLOG", {"content_type": "PODCAST"},
     "Content type must be one of: Blog post, Case study, Whitepaper, eBook, Video, Webinar, Infographic, Other."),
    ("CONTENT_MARKETING", "BLOG", {"distribution_channel": "x" * 121}, "Distribution channel can be at most 120 characters."),
    ("EMAIL_MARKETING", "EMAIL_NURTURE", {"email_platform": "OUTLOOK"}, "Email platform must be one of: HubSpot, Mailchimp, Other."),
    ("MARKETING_AUTOMATION", "EMAIL_NURTURE", {"partner_name": "x"}, "Marketing Automation initiatives don't have a field called partner_name."),
    ("PARTNERSHIPS", "REFERRALS", {"partnership_type": "FRIEND"},
     "Partnership type must be one of: Referral, Reseller, Co-marketing, Sponsorship, Technology, Other."),
    ("AFFILIATES", "REFERRALS", {"partner_name": 12}, "Partner name must be text."),
    ("EVENTS", "WEBINARS", {"venue": "Hall A"}, "Events initiatives don't have a field called venue."),
    ("OTHER", "OTHER", {"anything": 1}, "Other initiatives don't have a field called anything."),
]


@pytest.mark.parametrize(("channel", "sub_channel", "details", "message"), REJECTED_DETAILS)
def test_channel_details_reject_unknown_fields_and_values(client, admin, ref, channel, sub_channel, details, message):
    created = client.post("/api/initiatives", json=_payload(ref, channel, sub_channel, channel_details=details), headers=admin["headers"])
    assert (created.status_code, created.json()["detail"]) == (422, message)

    existing = _create(client, admin, _payload(ref, channel, sub_channel))
    patched = _patch(client, admin, existing["id"], {"channel_details": details})
    assert (patched.status_code, patched.json()["detail"]) == (422, message)


def test_changing_channel_keeps_only_the_details_that_fit(client, admin, ref):
    created = _create(client, admin, _payload(ref, channel_details={"ad_account_id": "A-1", "campaign_objective": "LEADS"}))

    social = _patch(client, admin, created["id"], {"channel_id": ref["channels"]["PAID_SOCIAL"], "sub_channel_id": ref["sub_channels"]["META_ADS"]})
    assert social.json()["channel_details"] == {"ad_account_id": "A-1", "campaign_objective": "LEADS"}

    seo = _patch(client, admin, created["id"], {"channel_id": ref["channels"]["SEO"], "sub_channel_id": ref["sub_channels"]["BLOG"]})
    assert seo.json()["channel_details"] == {}
    assert _audits(created["id"])[-1][1]["channel_details"] == {"ad_account_id": "A-1", "campaign_objective": "LEADS"}


# --- Required fields ---

def test_a_draft_needs_only_name_brand_and_channel(client, admin, ref):
    draft = _create(client, admin, {"name": "Pytest Draft", "brand_id": ref["brands"]["DESKFLEX"],
                                    "channel_id": ref["channels"]["PAID_SEARCH"], "status": "DRAFT"})
    assert (draft["sub_channel_id"], draft["budget_amount"], draft["owner_user_id"]) == (None, None, str(admin["id"]))
    assert draft["attention_reasons"] == []

    # Finishing it applies the rules
    finish = _patch(client, admin, draft["id"], {"status": "PLANNED"})
    assert (finish.status_code, finish.json()["detail"]) == (422, "Choose a sub-channel for Paid Search.")
    finish = _patch(client, admin, draft["id"], {"status": "PLANNED", "sub_channel_id": ref["sub_channels"]["BING_ADS"]})
    assert (finish.status_code, finish.json()["detail"]) == (422, "Allocated budget is required. Enter 0 if there isn't one.")
    finish = _patch(client, admin, draft["id"], {"status": "PLANNED", "sub_channel_id": ref["sub_channels"]["BING_ADS"], "budget_amount": 0})
    assert finish.status_code == 200, finish.text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"budget_amount": None}, "Allocated budget is required. Enter 0 if there isn't one."),
        ({"sub_channel_id": None}, "Choose a sub-channel for Paid Search."),
        ({"channel_id": "ORGANIC_SOCIAL", "sub_channel_id": None}, "Choose a sub-channel for Organic Social."),
        ({"kpi_target": 50}, "Choose a primary KPI before setting a KPI target."),
        ({"data_source": "LINKED"}, "Add at least one campaign link, or choose another data source."),
        ({"description": "x" * 1001}, "at most 1000 characters"),
        ({"data_source": "SOMETIMES"}, "literal_error"),
        ({"evaluation": {"maturity_days": 731}}, "less than or equal to 730"),
        ({"evaluation": {"revenue_basis": "BOOKED"}}, "extra_forbidden"),
        ({"links": [{"system": "SALESFORCE", "external_id": "1"}]}, "literal_error"),
        ({"links": [{"system": "HUBSPOT", "external_id": "  "}]}, "Campaign ID is required"),
        ({"links": [{"system": "HUBSPOT", "external_id": "1"}, {"system": "HUBSPOT", "external_id": "1"}]}, "The same campaign is listed twice."),
        ({"primary_kpi_id": 999999}, "Unknown primary KPI."),
    ],
)
def test_create_rules(client, admin, ref, overrides, message):
    if overrides.get("channel_id") == "ORGANIC_SOCIAL":
        overrides = {**overrides, "channel_id": ref["channels"]["ORGANIC_SOCIAL"]}
    response = client.post("/api/initiatives", json=_payload(ref, **overrides), headers=admin["headers"])

    assert response.status_code == 422
    assert message in response.text


def test_budget_zero_and_defaults(client, admin, ref):
    created = _create(client, admin, _payload(ref, budget_amount=0))

    assert created["budget_amount"] == 0
    assert created["owner_user_id"] == str(admin["id"])
    assert (created["data_source"], created["channel_details"], created["links"]) == ("MANUAL", {}, [])
    assert (created["progress_percent"], created["actual_spend"]) == (None, 0)
    # The Other channel has no sub-channels to choose from but Other itself
    other = _create(client, admin, _payload(ref, "OTHER", None, budget_amount=0))
    assert other["sub_channel_id"] is None


def test_editing_doesnt_require_fields_it_doesnt_touch(client, admin, ref):
    """Older initiatives without a budget can still be renamed; clearing a required field is refused."""
    created = clear_budget(_create(client, admin, _payload(ref)))

    assert _patch(client, admin, created["id"], {"name": "Pytest Wizard renamed"}).status_code == 200
    cleared = _patch(client, admin, created["id"], {"sub_channel_id": None})
    assert (cleared.status_code, cleared.json()["detail"]) == (422, "Choose a sub-channel for Paid Search.")
    no_budget = _patch(client, admin, created["id"], {"budget_amount": None})
    assert no_budget.json()["detail"] == "Allocated budget is required. Enter 0 if there isn't one."
    no_owner = _patch(client, admin, created["id"], {"owner_user_id": None})
    assert no_owner.json()["detail"] == "Owner is required."


def test_kpi_and_target(client, admin, ref, kpis):
    created = _create(client, admin, _payload(ref, primary_kpi_id=kpis["COST_PER_LEAD"], kpi_target=45.5, description="  Leads from search  "))
    assert created["primary_kpi"] == {"id": kpis["COST_PER_LEAD"], "code": "COST_PER_LEAD", "name": "Cost per lead", "unit": "MONEY"}
    assert (created["kpi_target"], created["description"]) == (45.5, "Leads from search")

    # Any KPI fits any channel; clearing the KPI clears its target
    assert _patch(client, admin, created["id"], {"primary_kpi_id": kpis["FOLLOWERS"]}).json()["primary_kpi"]["code"] == "FOLLOWERS"
    cleared = _patch(client, admin, created["id"], {"primary_kpi_id": None}).json()
    assert (cleared["primary_kpi"], cleared["kpi_target"]) == (None, None)
    refused = _patch(client, admin, created["id"], {"kpi_target": 10})
    assert (refused.status_code, refused.json()["detail"]) == (422, "Choose a primary KPI before setting a KPI target.")
    assert _audits(created["id"])[-1][1] == {"primary_kpi_id": kpis["FOLLOWERS"], "kpi_target": 45.5}


def test_maturity_window_from_the_wizard(client, admin, ref):
    created = _create(client, admin, _payload(ref, evaluation={"maturity_days": 120}))
    evaluation = client.get(f"/api/initiatives/{created['id']}/evaluation", headers=admin["headers"]).json()
    assert (evaluation["maturity_days"], evaluation["revenue_basis"], evaluation["is_default"]) == (120, "CLOSED_REVENUE", False)
    # Google Ads defaults to 90 days
    assert _audits(created["id"], "Evaluation") == [("UPDATE", {"maturity_days": 90}, {"maturity_days": 120})]

    assert _patch(client, admin, created["id"], {"evaluation": {"maturity_days": 0}}).status_code == 200
    assert _patch(client, admin, created["id"], {"evaluation": {"maturity_days": 0}}).status_code == 200
    assert [a[2] for a in _audits(created["id"], "Evaluation")] == [{"maturity_days": 120}, {"maturity_days": 0}]


# --- Links ---

def test_links_create_replace_and_audit(client, admin, ref):
    external = uuid.uuid4().hex[:12]
    created = _create(client, admin, _payload(ref, data_source="LINKED", links=[
        {"system": "GOOGLE_ADS", "external_id": f" {external} ", "account_id": "123-456"},
        {"system": "HUBSPOT", "external_id": f"hs-{external}"},
    ]))
    assert created["data_source"] == "LINKED"
    assert [(link["system"], link["external_id"], link["account_id"]) for link in created["links"]] == [
        ("GOOGLE_ADS", external, "123-456"), ("HUBSPOT", f"hs-{external}", None),
    ]
    assert _audits(created["id"])[0][2]["links"][0] == {"system": "GOOGLE_ADS", "external_id": external, "account_id": "123-456"}

    replaced = _patch(client, admin, created["id"], {"links": [
        {"system": "GOOGLE_ADS", "external_id": external, "account_id": "999"},
        {"system": "META_ADS", "external_id": f"m-{external}"},
    ]})
    assert replaced.status_code == 200, replaced.text
    assert sorted((link["system"], link["account_id"]) for link in replaced.json()["links"]) == [("GOOGLE_ADS", "999"), ("META_ADS", None)]
    _, old, new = _audits(created["id"])[-1]
    assert [link["system"] for link in old["links"]] == ["GOOGLE_ADS", "HUBSPOT"]
    assert [link["system"] for link in new["links"]] == ["GOOGLE_ADS", "META_ADS"]

    # The same set again changes nothing; links can't all go while the data source is Linked
    assert _patch(client, admin, created["id"], {"links": new["links"]}).status_code == 200
    assert len(_audits(created["id"])) == 2
    emptied = _patch(client, admin, created["id"], {"links": []})
    assert (emptied.status_code, emptied.json()["detail"]) == (422, "Add at least one campaign link, or choose another data source.")
    manual = _patch(client, admin, created["id"], {"links": [], "data_source": "MANUAL"})
    assert (manual.status_code, manual.json()["links"]) == (200, [])
    assert run_sql("SELECT count(*) AS n FROM initiative_links WHERE initiative_id = $1", uuid.UUID(created["id"]))[0]["n"] == 0


def test_a_campaign_belongs_to_one_initiative(client, admin, ref):
    external = uuid.uuid4().hex[:12]
    first = _create(client, admin, _payload(ref, name="Pytest Wizard First", links=[{"system": "LINKEDIN_ADS", "external_id": external}]))
    second = _create(client, admin, _payload(ref, name="Pytest Wizard Second"))

    taken = client.post("/api/initiatives", json=_payload(ref, links=[{"system": "LINKEDIN_ADS", "external_id": external}]), headers=admin["headers"])
    assert (taken.status_code, taken.json()["detail"]) == (422, "That campaign is already linked to Pytest Wizard First.")
    patched = _patch(client, admin, second["id"], {"links": [{"system": "LINKEDIN_ADS", "external_id": external}]})
    assert (patched.status_code, patched.json()["detail"]) == (422, "That campaign is already linked to Pytest Wizard First.")
    # Another system can use the same campaign id
    assert _patch(client, admin, second["id"], {"links": [{"system": "META_ADS", "external_id": external}]}).status_code == 200

    # Deleting an initiative frees its campaigns
    assert client.delete(f"/api/initiatives/{first['id']}", headers=admin["headers"]).status_code == 204
    assert _patch(client, admin, second["id"], {"links": [{"system": "LINKEDIN_ADS", "external_id": external}]}).status_code == 200


# --- List additions ---

def _list(client, user, **params):
    response = client.get("/api/initiatives", params=params, headers=user["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def _cost(client, user, initiative_id, category_id, amount, status="INVOICED", currency=None):
    body = {"category_id": category_id, "amount": amount, "incurred_on": "2026-01-15", "status": status}
    if currency:
        body["currency"] = currency
    response = client.post(f"/api/initiatives/{initiative_id}/spend-lines", json=body, headers=user["headers"])
    assert response.status_code == 201, response.text


def test_actual_spend_progress_and_attention_reasons(client, admin, ref, categories, tag):
    today = date.today()
    software = categories["SOFTWARE"]

    over = _create(client, admin, _payload(ref, name=f"{tag} over", budget_amount=1000, start_date=str(today), status="ACTIVE"))
    _cost(client, admin, over["id"], software, 900)
    _cost(client, admin, over["id"], software, 300, status="COMMITTED")  # not actual yet
    _cost(client, admin, over["id"], software, 50, currency="EUR")  # another currency isn't counted
    no_budget = clear_budget(_create(client, admin, _payload(ref, name=f"{tag} no budget", start_date=str(today))))
    untracked = _create(client, admin, _payload(ref, name=f"{tag} untracked", status="ACTIVE", start_date=str(today - timedelta(days=20))))
    linked = _create(client, admin, _payload(ref, name=f"{tag} linked", status="ACTIVE", start_date=str(today - timedelta(days=20)),
                                             links=[{"system": "GOOGLE_ADS", "external_id": uuid.uuid4().hex}]))
    ended = _create(client, admin, _payload(ref, name=f"{tag} ended", start_date=str(today - timedelta(days=40)), end_date=str(today - timedelta(days=1))))
    undated = _create(client, admin, _payload(ref, name=f"{tag} undated"))
    fine = _create(client, admin, _payload(ref, name=f"{tag} fine", start_date=str(today)))
    draft = _create(client, admin, {"name": f"{tag} draft", "brand_id": ref["brands"]["DESKFLEX"], "channel_id": ref["channels"]["SEO"], "status": "DRAFT"})

    items = {i["id"]: i for i in _list(client, admin, search=tag)["items"]}
    assert (items[over["id"]]["actual_spend"], items[over["id"]]["progress_percent"]) == (900, 90)
    _cost(client, admin, over["id"], software, 200)
    items = {i["id"]: i for i in _list(client, admin, search=tag)["items"]}
    assert (items[over["id"]]["actual_spend"], items[over["id"]]["progress_percent"]) == (1100, 110)

    expected = {
        over["id"]: ["OVER_BUDGET"],
        no_budget["id"]: ["NO_BUDGET"],
        untracked["id"]: ["NO_TRACKING"],
        linked["id"]: [],
        ended["id"]: ["NO_RESULTS"],
        undated["id"]: ["NO_DATES"],
        fine["id"]: [],
        draft["id"]: [],
    }
    assert {i: items[i]["attention_reasons"] for i in expected} == expected
    assert all(items[i]["needs_attention"] == bool(reasons) for i, reasons in expected.items())
    assert items[no_budget["id"]]["progress_percent"] is None

    flagged = _list(client, admin, search=tag, needs_attention="true")
    assert {i["id"] for i in flagged["items"]} == {i for i, reasons in expected.items() if reasons}
    assert flagged["total"] == 5

    # Completed with no results counts as ended; recording results clears it
    completed = _create(client, admin, _payload(ref, name=f"{tag} completed", status="ACTIVE", start_date=str(today)))
    assert _patch(client, admin, completed["id"], {"status": "COMPLETED"}).json()["attention_reasons"] == ["NO_RESULTS"]
    saved = client.put(f"/api/initiatives/{completed['id']}/outcomes/{today.replace(day=1)}",
                       json={"values": [{"metric_code": "LEADS", "value": 3}]}, headers=admin["headers"])
    assert saved.status_code == 200, saved.text
    assert client.get(f"/api/initiatives/{completed['id']}", headers=admin["headers"]).json()["attention_reasons"] == []


def test_owner_and_date_filters(client, admin, viewer, ref, tag):
    mine = _create(client, admin, _payload(ref, name=f"{tag} mine", start_date="2004-03-01", end_date="2004-03-31"))
    theirs = _create(client, admin, _payload(ref, name=f"{tag} theirs", owner_user_id=str(viewer["id"]), start_date="2004-05-01"))
    _create(client, admin, _payload(ref, name=f"{tag} undated"))

    def ids(**params):
        return {i["id"] for i in _list(client, admin, search=tag, **params)["items"]}

    assert ids(owner_user_id=str(viewer["id"])) == {theirs["id"]}
    assert ids(**{"from": "2004-03-15", "to": "2004-03-20"}) == {mine["id"]}
    assert ids(**{"from": "2004-04-01", "to": "2004-04-30"}) == set()
    # No end date: still running after its start
    assert ids(**{"from": "2005-01-01", "to": "2005-01-31"}) == {theirs["id"]}
    assert ids(**{"from": "2004-03-31"}) == {mine["id"], theirs["id"]}
    assert ids(to="2004-02-28") == set()
    # Brand and owner names are searched too
    assert theirs["id"] in {i["id"] for i in _list(client, admin, search="Pytest User", owner_user_id=str(viewer["id"]))["items"]}
    bad = client.get("/api/initiatives", params={"from": "2004-03-02", "to": "2004-03-01"}, headers=admin["headers"])
    assert bad.status_code == 422


# --- Summary ---

def test_summary_counts_budget_per_currency_and_previous_period(client, admin, ref, tag):
    _create(client, admin, _payload(ref, name=f"{tag} a", status="ACTIVE", budget_amount=1000, start_date="2003-06-01", end_date="2003-06-10"))
    _create(client, admin, _payload(ref, name=f"{tag} b", budget_amount=250.5, start_date="2003-06-15", end_date="2003-06-20"))
    _create(client, admin, _payload(ref, name=f"{tag} c", budget_amount=400, currency="EUR", start_date="2003-06-20", end_date="2003-06-30"))
    _create(client, admin, _payload(ref, name=f"{tag} earlier", status="ACTIVE", budget_amount=70, start_date="2003-05-05", end_date="2003-05-06"))
    clear_budget(_create(client, admin, _payload(ref, name=f"{tag} earlier no budget", start_date="2003-05-20", end_date="2003-05-21")))

    response = client.get("/api/initiatives/summary", params={"search": tag, "from": "2003-06-01", "to": "2003-06-30"}, headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    # Every one has ended without results, so each needs attention
    assert (body["total"], body["active"], body["needs_attention"]) == (3, 1, 3)
    assert body["allocated_budget"] == [{"currency": "USD", "amount": 1250.5}, {"currency": "EUR", "amount": 400}]
    # The 30 days before June 1-30: May 2-31
    assert body["previous"] == {"total": 2, "active": 1, "needs_attention": 2, "allocated_budget": [{"currency": "USD", "amount": 70}]}

    # Without a period there's nothing to compare with; needs_attention isn't a filter here
    whole = client.get("/api/initiatives/summary", params={"search": tag, "needs_attention": "false"}, headers=admin["headers"]).json()
    assert (whole["total"], whole["previous"]) == (5, None)
    assert client.get("/api/initiatives/summary", params={"search": tag, "status": "ACTIVE"}, headers=admin["headers"]).json()["total"] == 2


def test_summary_and_activity_need_view_permission(client, admin, viewer, ref):
    created = _create(client, admin, _payload(ref))
    assert client.get("/api/initiatives/summary", headers=viewer["headers"]).status_code == 403
    assert client.get(f"/api/initiatives/{created['id']}/activity", headers=viewer["headers"]).status_code == 403


# --- Activity ---

def test_activity_in_plain_english(client, admin, ref, categories, request):
    created = _create(client, admin, _payload(ref, status="PLANNED"))
    # Entries without a person aren't removed with the test user
    request.addfinalizer(lambda: run_sql("DELETE FROM audit_logs WHERE entity_id = $1 AND actor_user_id IS NULL", created["id"]))
    run_sql("UPDATE audit_logs SET created_at = created_at - interval '1 minute' WHERE entity_id = $1", created["id"])
    _cost(client, admin, created["id"], categories["MEDIA_ADS"], 1250, status="COMMITTED")
    line_id = client.get(f"/api/initiatives/{created['id']}/spend-lines", headers=admin["headers"]).json()
    line_id = (line_id["items"] if isinstance(line_id, dict) else line_id)[0]["id"]
    assert client.patch(f"/api/spend-lines/{line_id}", json={"amount": 1500}, headers=admin["headers"]).status_code == 200
    client.put(f"/api/initiatives/{created['id']}/outcomes/2026-01-01",
               json={"values": [{"metric_code": "LEADS", "value": 200}]}, headers=admin["headers"])
    _patch(client, admin, created["id"], {"links": [{"system": "GOOGLE_ADS", "external_id": uuid.uuid4().hex[:10]}]})
    _patch(client, admin, created["id"], {"evaluation": {"maturity_days": 60}})
    _patch(client, admin, created["id"], {"status": "ACTIVE", "budget_amount": 6000})
    # A change made by the Navan sync has no person
    run_sql(
        "INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, new_value, created_at) "
        "VALUES (NULL, 'UPDATE', 'Initiative', $1, CAST($2 AS jsonb), now() + interval '1 second')",
        created["id"], json.dumps({"event": {"navan_estimate_amount": 900}, "source": "NAVAN"}),
    )

    response = client.get(f"/api/initiatives/{created['id']}/activity", params={"limit": 50}, headers=admin["headers"])
    assert response.status_code == 200, response.text
    items = response.json()
    assert [i["title"] for i in items] == [
        "Navan estimate updated",
        "Status changed to Active",
        "Evaluation settings updated",
        "Campaign links updated",
        "Results updated",
        "Expense updated",
        "Expense added",
        "Initiative created",
    ]
    assert [i["at"] for i in items] == sorted((i["at"] for i in items), reverse=True)
    assert items[0]["actor_name"] == "Navan"
    assert items[0]["detail"] == "Navan estimate set to 900.00 USD"
    assert all(i["actor_name"] == "Pytest User" for i in items[1:])
    assert items[1]["detail"] == "Also changed: budget"
    assert items[2]["detail"] == "Maturity window set to 60 days"
    assert items[3]["detail"].startswith("Linked Google Ads campaign ")
    assert items[4]["detail"] == "200 leads recorded for Jan 2026"
    assert "cost changed from 1,250.00 USD to 1,500.00 USD" in items[5]["detail"]
    assert "cost added: 1,250.00 USD" in items[6]["detail"]

    limited = client.get(f"/api/initiatives/{created['id']}/activity", params={"limit": 2}, headers=admin["headers"]).json()
    assert [i["title"] for i in limited] == ["Navan estimate updated", "Status changed to Active"]
    assert client.get(f"/api/initiatives/{created['id']}/activity", params={"limit": 51}, headers=admin["headers"]).status_code == 422
    assert client.get(f"/api/initiatives/{uuid.uuid4()}/activity", headers=admin["headers"]).status_code == 404
