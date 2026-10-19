"""The Paid Advertising page: overview figures, attention rules, campaign detail and data health.

The rules are tested directly on the calculations, then end to end through the API. API tests put their campaigns in
a made-up currency of their own, so nothing else in the database is counted with them (the page shows one currency
at a time). The campaigns are made by the temporary admin and go when it is removed.
"""
import random
import string
import uuid
from datetime import date, timedelta

import pytest

from services import ads_overview_service as s
from tests.conftest import run_sql

TODAY = date(2026, 10, 19)


# --- Calculations ---

def test_previous_range_and_allocation():
    assert s.previous_range(date(2026, 9, 20), date(2026, 10, 19)) == (date(2026, 8, 21), date(2026, 9, 19))
    campaign = {"budget_amount": 9000.0, "start_date": date(2026, 9, 1), "end_date": date(2026, 11, 29)}  # 90 days
    assert s.allocated_budget(campaign, date(2026, 9, 1), date(2026, 9, 30), None) == pytest.approx(3000)
    assert s.allocated_budget(campaign, date(2026, 12, 1), date(2026, 12, 31), 50.0) == 0
    # No budget or no end date: the budgets on its results
    assert s.allocated_budget({**campaign, "end_date": None}, date(2026, 9, 1), date(2026, 9, 30), 120.0) == 120.0
    assert s.allocated_budget({**campaign, "budget_amount": 0}, date(2026, 9, 1), date(2026, 9, 30), None) is None


def test_ratios_keep_leads_and_conversions_apart():
    figures = s.figures_of([
        {"spend": 100.0, "impressions": 10000, "clicks": 200, "conversions": 8, "verified_leads": 4, "mqls": 2, "sqls": None},
        {"spend": 50.0, "impressions": None, "clicks": 100, "conversions": None, "verified_leads": 1, "mqls": None, "sqls": None},
    ])
    assert figures == {"spend": 150.0, "impressions": 10000, "clicks": 300, "conversions": 8, "verified_leads": 5,
                       "mqls": 2, "sqls": None}
    calc = s.ratios(figures)
    assert calc["cpl"] == 30.0  # spend / verified leads, never leads + conversions
    assert calc["cost_per_conversion"] == 18.75
    assert calc["cpm"] == 15.0
    assert calc["ctr"] == 3.0
    assert calc["cpc"] == 0.5


@pytest.mark.parametrize(("objective", "code", "value"), [
    ("AWARENESS", "CPM", 4.0), ("VIDEO_VIEWS", "CPM", 4.0), ("TRAFFIC", "CPC", 2.0), ("ENGAGEMENT", "CPE", 2.0),
    ("LEADS", "CPL", 40.0), ("CONVERSIONS", "CPA", 10.0), (None, "CPL", 40.0),
])
def test_success_metric_follows_the_objective(objective, code, value):
    values = {"cpm": 4.0, "cpc": 2.0, "cpl": 40.0, "cost_per_conversion": 10.0}
    metric = s.success_metric(objective, None, None, values)
    assert (metric["code"], metric["value"], metric["target"], metric["on_target"]) == (code, value, None, None)


def test_success_metric_target_only_when_the_kpi_matches():
    values = {"cpm": 4.0, "cpc": 2.0, "cpl": 40.0, "cost_per_conversion": 10.0}
    assert s.success_metric("LEADS", "COST_PER_LEAD", 50, values)["on_target"] is True
    assert s.success_metric("LEADS", "COST_PER_LEAD", 30, values)["on_target"] is False
    # A CPC target says nothing about cost per lead
    assert s.success_metric("LEADS", "CPC", 1, values)["target"] is None
    # No objective: the primary KPI's figure
    assert s.success_metric(None, "CPC", 1.5, values) == {
        "code": "CPC", "label": "Cost per click", "value": 2.0, "target": 1.5, "on_target": False,
    }


def test_primary_kpi_value_and_target():
    range_ratios = {"cpl": 80.0, "ctr": 2.5, "cpc": 1.2}
    to_date = {"spend": 1000.0, "verified_leads": 40, "customers": 4, "revenue": 3000.0}
    assert s.primary_kpi_value("COST_PER_LEAD", range_ratios, to_date) == 80.0
    assert s.primary_kpi_value("LEADS", range_ratios, to_date) == 40
    assert s.primary_kpi_value("CAC", range_ratios, to_date) == 250.0
    assert s.primary_kpi_value("ROI", range_ratios, to_date) == 200.0
    assert s.primary_kpi_value("FOLLOWERS", range_ratios, to_date) is None
    assert s.kpi_on_target("MONEY", 80, 75) is False
    assert s.kpi_on_target("MONEY", 70, 75) is True
    assert s.kpi_on_target("COUNT", 40, 30) is True
    assert s.kpi_on_target("PERCENT", 2.5, 3) is False
    assert s.kpi_on_target("COUNT", None, 3) is None


def _row(**over) -> dict:
    row = {
        "platform": "GOOGLE_ADS", "status": "ACTIVE", "start_date": TODAY - timedelta(days=30),
        "end_date": TODAY + timedelta(days=30), "budget_used_percent": 50.0, "primary_kpi": None, "kpi_target": None,
        "cpl": 60.0, "cost_per_mql": None,
        "data_health": {"status": "OK", "source": "MANUAL", "last_data_date": TODAY, "connection_status": None},
    }
    return {**row, **over}


def _kinds(row, cpl=None, lead_campaigns=3):
    reasons = s.attention_reasons(row, {"GOOGLE_ADS": cpl if cpl is not None else 60.0}, {"GOOGLE_ADS": lead_campaigns}, TODAY)
    return [r["kind"] for r in reasons]


def test_attention_rules():
    assert _kinds(_row()) == []
    # Budget nearly used up from 90%; over 100% is serious; finished campaigns are left alone
    assert _kinds(_row(budget_used_percent=89.9)) == []
    assert _kinds(_row(budget_used_percent=94.0)) == ["BUDGET"]
    assert s.attention_reasons(_row(budget_used_percent=104.0), {}, {}, TODAY)[0]["severity"] == "HIGH"
    assert _kinds(_row(budget_used_percent=120.0, status="COMPLETED")) == []
    # Cost per lead: against the KPI target when the KPI is a cost per lead...
    kpi = {"code": "COST_PER_LEAD", "name": "Cost per lead", "unit": "MONEY"}
    assert _kinds(_row(primary_kpi=kpi, kpi_target=50.0, cpl=60.0)) == ["CPL_ABOVE_TARGET"]
    assert _kinds(_row(primary_kpi=kpi, kpi_target=70.0, cpl=60.0), cpl=10.0) == []
    # ...otherwise 30% or more above the platform's cost per lead (only with other campaigns to compare with)
    assert _kinds(_row(cpl=78.0), cpl=60.0) == ["CPL_ABOVE_PLATFORM"]
    assert _kinds(_row(cpl=77.0), cpl=60.0) == []
    assert _kinds(_row(cpl=200.0), cpl=60.0, lead_campaigns=1) == []
    # Paused while its dates say it runs; not when it hasn't started or has ended
    assert _kinds(_row(status="PAUSED")) == ["UNEXPECTEDLY_PAUSED"]
    assert _kinds(_row(status="PAUSED", start_date=TODAY + timedelta(days=1))) == []
    assert _kinds(_row(status="PAUSED", end_date=TODAY - timedelta(days=1))) == []
    # Data delayed: stale, or the API connection isn't connected, or nothing at all a week after it started
    stale = {"status": "STALE", "source": "MANUAL", "last_data_date": TODAY - timedelta(days=9), "connection_status": None}
    assert _kinds(_row(data_health=stale)) == ["DATA_DELAYED"]
    assert _kinds(_row(data_health={**stale, "status": "CONNECTION"})) == ["DATA_DELAYED"]
    assert _kinds(_row(data_health={**stale, "status": "NO_DATA"})) == ["DATA_DELAYED"]
    assert _kinds(_row(data_health={**stale, "status": "NO_DATA"}, start_date=TODAY - timedelta(days=3))) == []


def test_data_health():
    running = {"status": "ACTIVE", "start_date": TODAY - timedelta(days=30), "end_date": None}
    assert s.data_health(running, TODAY - timedelta(days=7), "SPREADSHEET", None, TODAY)["status"] == "OK"
    assert s.data_health(running, TODAY - timedelta(days=8), "SPREADSHEET", None, TODAY)["status"] == "STALE"
    assert s.data_health(running, None, None, None, TODAY)["status"] == "NO_DATA"
    assert s.data_health(running, TODAY, "MANUAL", "EXPIRED", TODAY)["status"] == "CONNECTION"
    connected = s.data_health(running, TODAY, "MANUAL", "CONNECTED", TODAY)
    assert (connected["status"], connected["source"]) == ("OK", "API")
    assert s.data_health({**running, "status": "PLANNED"}, None, None, None, TODAY)["status"] == "NOT_STARTED"
    # A finished campaign isn't late
    assert s.data_health({**running, "status": "COMPLETED"}, TODAY - timedelta(days=60), "MANUAL", None, TODAY)["status"] == "OK"
    connections = {"GOOGLE_ADS": [{"status": "EXPIRED"}, {"status": "CONNECTED"}], "MICROSOFT_ADS": [{"status": "ERROR"}]}
    assert s.connection_status("YOUTUBE_ADS", connections) == "CONNECTED"
    assert s.connection_status("BING_ADS", connections) == "ERROR"
    assert s.connection_status("TIKTOK_ADS", connections) is None


def test_buckets_and_pace():
    kind, days = s.buckets(date(2026, 10, 6), date(2026, 10, 19))
    assert (kind, len(days)) == ("day", 14)
    kind, weeks = s.buckets(date(2026, 9, 20), date(2026, 10, 19))
    assert kind == "week"
    assert weeks[0] == (date(2026, 9, 20), date(2026, 9, 20))  # cut to the range (Sep 20 is a Sunday)
    assert weeks[1] == (date(2026, 9, 21), date(2026, 9, 27))
    assert weeks[-1] == (date(2026, 10, 19), date(2026, 10, 19))
    kind, month_points = s.buckets(date(2024, 1, 15), date(2026, 1, 31))
    assert kind == "month" and month_points[0] == (date(2024, 1, 15), date(2024, 1, 31))
    assert s.months(date(2026, 9, 20), date(2026, 10, 19)) == [
        (date(2026, 9, 20), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 19)),
    ]
    start, end = TODAY - timedelta(days=49), TODAY + timedelta(days=50)  # 100 days, 50 gone by
    assert s.pace(1000, 500, start, end, TODAY)["status"] == "ON_TRACK"
    assert s.pace(1000, 700, start, end, TODAY)["status"] == "AHEAD"
    assert s.pace(1000, 300, start, end, TODAY)["status"] == "BEHIND"
    assert s.pace(1000, 0, TODAY + timedelta(days=1), end, TODAY)["status"] == "NOT_STARTED"
    assert s.pace(None, 10, start, end, TODAY)["status"] is None


def test_which_campaigns_show_for_a_range():
    rng = (date(2026, 9, 20), date(2026, 10, 19))
    c = {"status": "ACTIVE", "start_date": date(2026, 8, 1), "end_date": date(2026, 9, 1)}
    assert s.shows_in_range(c, False, *rng, TODAY, None) is False
    assert s.shows_in_range(c, True, *rng, TODAY, None) is True
    assert s.shows_in_range({**c, "end_date": date(2026, 9, 25)}, False, *rng, TODAY, None) is True
    upcoming = {"status": "PLANNED", "start_date": date(2026, 11, 1), "end_date": None}
    assert s.shows_in_range(upcoming, False, *rng, TODAY, None) is True
    assert s.shows_in_range(upcoming, False, date(2026, 8, 1), date(2026, 8, 31), TODAY, None) is False
    cancelled = {**c, "status": "CANCELLED", "end_date": None}
    assert s.shows_in_range(cancelled, False, *rng, TODAY, None) is False
    assert s.shows_in_range(cancelled, False, *rng, TODAY, "CANCELLED") is True
    assert s.matches({"brand_id": 1, "platform": "META_ADS", "objective": None}, {"brand_id": 1, "objective": None})
    assert not s.matches({"brand_id": 1, "platform": "META_ADS"}, {"platform": "GOOGLE_ADS"})


# --- Through the API ---

@pytest.fixture
def currency():
    """A made-up currency only this test uses."""
    return "Q" + "".join(random.choices(string.ascii_uppercase, k=2))


def _kpi_id(code: str) -> int:
    return run_sql("SELECT id FROM primary_kpis WHERE code = $1", code)[0]["id"]


def _campaign(client, admin, ref, currency, name, *, channel="PAID_SEARCH", platform="GOOGLE_ADS", brand="OTHERSHIP",
              status="ACTIVE", start=-60, end=29, budget=9000, objective=None, kpi=None, target=None, links=None):
    today = date.today()
    body = {
        "name": f"Pytest overview {uuid.uuid4().hex[:6]} {name}",
        "brand_id": ref["brands"][brand], "channel_id": ref["channels"][channel],
        "sub_channel_id": ref["sub_channels"][platform], "currency": currency, "budget_amount": budget, "status": status,
        "start_date": str(today + timedelta(days=start)), "end_date": str(today + timedelta(days=end)),
    }
    if objective:
        body["channel_details"] = {"campaign_objective": objective}
    if kpi:
        body.update(primary_kpi_id=_kpi_id(kpi), kpi_target=target)
    if links:
        body.update(links=links, data_source="LINKED")
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def _result(client, admin, campaign, start_days_ago, length=7, **figures):
    today = date.today()
    start = today - timedelta(days=start_days_ago)
    body = {"initiative_id": campaign["id"], "period_start": str(start), "period_end": str(start + timedelta(days=length - 1)),
            **figures}
    response = client.post("/api/ads/results", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def scenario(client, admin, ref, currency):
    """Four campaigns in the test currency, one for each attention rule. Range: the last 30 days."""
    google_id = f"pytest-{uuid.uuid4().hex[:10]}"
    a = _campaign(client, admin, ref, currency, "A leads", objective="LEADS", kpi="COST_PER_LEAD", target=50,
                  links=[{"system": "GOOGLE_ADS", "external_id": google_id}])
    _result(client, admin, a, 20, length=15, spend=1000, impressions=10000, clicks=500, conversions=30, hubspot_contacts=10, mqls=4, sqls=2)
    _result(client, admin, a, 40, spend=500, impressions=5000, clicks=250, conversions=10, hubspot_contacts=10)
    b = _campaign(client, admin, ref, currency, "B awareness", channel="PAID_SOCIAL", platform="LINKEDIN_ADS",
                  brand="DESKFLEX", budget=3000, objective="AWARENESS")
    _result(client, admin, b, 50, spend=2000, impressions=100000, clicks=300)
    _result(client, admin, b, 3, length=3, spend=820, impressions=50000, clicks=100, hubspot_contacts=0)
    c = _campaign(client, admin, ref, currency, "C paused", channel="PAID_SOCIAL", platform="META_ADS",
                  status="PAUSED", start=-10, end=20, budget=1000)
    d = _campaign(client, admin, ref, currency, "D stale", start=-30, end=30, budget=6000, objective="TRAFFIC")
    _result(client, admin, d, 20, spend=300, impressions=4000, clicks=150, hubspot_contacts=6)
    return {"a": a, "b": b, "c": c, "d": d, "google_id": google_id}


def _overview(client, user, **params):
    response = client.get("/api/ads/overview", params=params, headers=user["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def test_overview_figures_previous_period_and_attention(client, admin, currency, scenario):
    data = _overview(client, admin, currency=currency)
    today = date.today()
    assert (data["date_from"], data["date_to"]) == (str(today - timedelta(days=29)), str(today))
    assert (data["previous_from"], data["previous_to"]) == (str(today - timedelta(days=59)), str(today - timedelta(days=30)))
    assert data["currency"] == currency and data["bucket"] == "week"
    k = data["kpis"]
    # Budgets spread over the campaigns' dates: 9000 x 30/90, 3000 x 30/90, 1000 x 11/31, 6000 x 30/61
    allocated = 3000 + 1000 + 1000 * 11 / 31 + 6000 * 30 / 61
    assert k["allocated_budget"] == pytest.approx(allocated, abs=0.01)
    assert k["actual_spend"] == 2120
    assert k["budget_remaining"] == pytest.approx(allocated - 2120, abs=0.01)
    assert k["remaining_percent"] == pytest.approx((allocated - 2120) / allocated * 100, abs=0.01)
    assert (k["verified_leads"], k["cost_per_lead"], k["conversions"]) == (16, 132.5, 30)
    assert (k["campaign_count"], k["active_campaign_count"]) == (4, 3)
    p = data["previous"]
    assert (p["actual_spend"], p["verified_leads"], p["cost_per_lead"]) == (2500, 10, 250)
    # Over time: the points add up to the period's figures
    assert sum(point["spend"] for point in data["timeseries"]) == 2120
    assert sum(point["verified_leads"] for point in data["timeseries"]) == 16

    platforms = {p["platform"]: p for p in data["by_platform"]}
    assert set(platforms) == {"GOOGLE_ADS", "LINKEDIN_ADS", "META_ADS"}
    assert (platforms["GOOGLE_ADS"]["spend"], platforms["GOOGLE_ADS"]["verified_leads"]) == (1300, 16)
    assert (platforms["GOOGLE_ADS"]["previous_spend"], platforms["LINKEDIN_ADS"]["previous_spend"]) == (500, 2000)
    brands = {b["brand_name"]: b for b in data["by_brand"]}
    assert brands["DeskFlex"]["spend"] == 820 and brands["Othership"]["spend"] == 1300
    assert sum(m["spend"] or 0 for m in data["by_month"]) == 2120

    names = {scenario[key]["id"]: key for key in "abcd"}
    kinds = {(names[a["initiative_id"]], a["kind"]) for a in data["attention"]}
    assert kinds == {("a", "CPL_ABOVE_TARGET"), ("b", "BUDGET"), ("c", "UNEXPECTEDLY_PAUSED"), ("d", "DATA_DELAYED")}
    budget = next(a for a in data["attention"] if a["kind"] == "BUDGET")
    assert (budget["value"], budget["severity"]) == (94.0, "MEDIUM")

    rows = {names[r["initiative_id"]]: r for r in data["campaigns"]}
    a = rows["a"]
    assert (a["spend"], a["verified_leads"], a["cpl"], a["ctr"], a["cpc"], a["conversions"]) == (1000, 10, 100, 5, 2, 30)
    assert (a["budget_amount"], a["spend_to_date"], a["allocated_budget"]) == (9000, 1500, 3000)
    assert a["success_metric"] == {"code": "CPL", "label": "Cost per lead", "value": 100.0, "target": 50.0, "on_target": False}
    assert (a["primary_kpi"]["code"], a["primary_kpi_value"], a["primary_kpi_on_target"]) == ("COST_PER_LEAD", 100.0, False)
    assert rows["b"]["success_metric"]["code"] == "CPM" and rows["b"]["success_metric"]["value"] == 16.4
    assert (rows["b"]["platform"], rows["b"]["channel"]) == ("LINKEDIN_ADS", "PAID_SOCIAL")
    assert rows["d"]["success_metric"]["code"] == "CPC"
    assert rows["d"]["data_health"]["status"] == "STALE"
    assert rows["c"]["data_health"]["status"] == "NO_DATA" and rows["c"]["spend"] is None


def test_overview_filters(client, admin, ref, currency, scenario):
    def ids(**params):
        return {r["initiative_id"] for r in _overview(client, admin, currency=currency, **params)["campaigns"]}

    assert ids(platform="LINKEDIN_ADS") == {scenario["b"]["id"]}
    assert ids(brand_id=ref["brands"]["DESKFLEX"]) == {scenario["b"]["id"]}
    assert ids(objective="TRAFFIC") == {scenario["d"]["id"]}
    assert ids(status="PAUSED") == {scenario["c"]["id"]}
    # A range before any of them ran
    old = _overview(client, admin, currency=currency, **{"from": "2020-01-01", "to": "2020-01-31"})
    assert old["campaigns"] == [] and old["kpis"]["actual_spend"] is None
    # The platform filter doesn't change what a campaign is compared with
    filtered = _overview(client, admin, currency=currency, brand_id=ref["brands"]["OTHERSHIP"])
    assert {a["kind"] for a in filtered["attention"]} == {"CPL_ABOVE_TARGET", "UNEXPECTEDLY_PAUSED", "DATA_DELAYED"}
    # Short ranges are by day
    assert _overview(client, admin, currency=currency, **{"from": str(date.today() - timedelta(days=6))})["bucket"] == "day"
    refused = client.get("/api/ads/overview", params={"from": "2026-10-10", "to": "2026-10-01"}, headers=admin["headers"])
    assert refused.status_code == 422


def test_money_never_mixes_currencies(client, admin, ref, currency, scenario):
    other = "Q" + "".join(random.choices(string.ascii_uppercase, k=2))
    while other == currency:
        other = "Q" + "".join(random.choices(string.ascii_uppercase, k=2))
    e = _campaign(client, admin, ref, other, "E other currency", budget=1000)
    _result(client, admin, e, 5, spend=999, hubspot_contacts=3)
    assert _overview(client, admin, currency=currency)["kpis"]["actual_spend"] == 2120
    data = _overview(client, admin, currency=other)
    assert (data["kpis"]["actual_spend"], data["currency"]) == (999, other)
    assert [r["initiative_id"] for r in data["campaigns"]] == [e["id"]]
    assert {currency, other} <= set(data["currencies"])


def test_campaign_detail(client, admin, scenario):
    a = scenario["a"]
    response = client.get(f"/api/ads/campaigns/{a['id']}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["campaign"]["initiative_id"] == a["id"] and detail["currency"] == a["currency"]
    assert sum(p["spend"] for p in detail["trend"]) == 1000
    assert detail["funnel"] == {"impressions": 10000, "clicks": 500, "conversions": 30, "verified_leads": 10, "mqls": 4, "sqls": 2}
    budget = detail["budget"]
    assert (budget["budget_amount"], budget["spent"], budget["remaining"]) == (9000, 1500, 7500)
    # 61 of 90 days gone, 16.67% spent: well behind
    assert budget["time_elapsed_percent"] == pytest.approx(61 / 90 * 100, abs=0.01)
    assert budget["status"] == "BEHIND"
    crm = detail["crm"]
    assert (crm["verified_leads"], crm["mqls"], crm["sqls"], crm["cost_per_lead"]) == (20, 4, 2, 75)
    assert detail["links"] == [{"system": "GOOGLE_ADS", "external_id": scenario["google_id"], "account_id": None}]
    assert detail["platform_cpl"] == 81.25  # Google Ads in the range: 1,300 / 16
    # A finished campaign still opens, with its figures for any range
    old = client.get(f"/api/ads/campaigns/{a['id']}", params={"from": "2020-01-01", "to": "2020-01-31"}, headers=admin["headers"])
    assert old.status_code == 200 and old.json()["campaign"]["spend"] is None
    assert client.get(f"/api/ads/campaigns/{uuid.uuid4()}", headers=admin["headers"]).status_code == 404


def test_data_health_endpoint(client, admin, ref, scenario):
    response = client.get("/api/ads/data-health", params={"brand_id": ref["brands"]["OTHERSHIP"]}, headers=admin["headers"])
    assert response.status_code == 200, response.text
    platforms = {p["platform"]: p for p in response.json()["platforms"]}
    google = platforms["GOOGLE_ADS"]
    problems = {p["initiative_id"]: p for p in google["problems"]}
    assert problems[scenario["d"]["id"]]["health"] == "STALE"
    assert scenario["a"]["id"] not in problems
    assert google["source"] in ("MANUAL", "SPREADSHEET", "DATA_STUDIO", "API")
    # DeskFlex's campaign is filtered out
    assert scenario["b"]["id"] not in {x["initiative_id"] for p in platforms.values() for x in p["problems"]}
    assert all(p["campaign_count"] >= p["active_campaign_count"] for p in platforms.values())


def test_social_spend_lines_and_campaign_picker(client, admin, scenario):
    lines = client.get(f"/api/initiatives/{scenario['b']['id']}/spend-lines", headers=admin["headers"]).json()
    assert sorted((line["source"], line["amount"]) for line in lines) == [("LINKEDIN_ADS", 820), ("LINKEDIN_ADS", 2000)]
    picker = {c["initiative_id"]: c for c in client.get("/api/ads/campaigns", headers=admin["headers"]).json()}
    assert picker[scenario["c"]["id"]]["platform"] == "META_ADS"


def test_permissions(client, admin, viewer, scenario):
    for path in ("/api/ads/overview", f"/api/ads/campaigns/{scenario['a']['id']}", "/api/ads/data-health"):
        assert client.get(path, headers=viewer["headers"]).status_code == 403
        assert client.get(path).status_code == 401
