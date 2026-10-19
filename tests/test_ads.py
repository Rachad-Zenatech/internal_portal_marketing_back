"""Paid ads (Google Ads and Bing Ads here; every platform in test_ads_overview.py): results, the costs and outcomes they feed, and the PPC sheet upload.

Results use periods from 2091 on, which no real data uses. Campaigns are made by the temporary admin, so they go
(with their results, costs and outcomes) when it is removed; monthly budgets made here are removed after each test.
"""
import io
import uuid
from datetime import datetime

import pytest
from openpyxl import Workbook, load_workbook

from services.ads_import_service import TEMPLATE_COLUMNS
from services.audit_service import entity_title_sql
from tests.conftest import _delete_user, _make_user, _token_for, run_sql

# Marketing's "SaaS PPC Campaigns" header exactly as in their sheet (stray spaces included)
SHEET_HEADER = [
    "Brand", "Market/Region", "Period Start", "Period End", "Channel", "Sub-Channel", "Campaign Name", "Primary KPI",
    "Budget Allocated ($)", "Actual Spend ", " Impressions", "Clicks", "CTR (%)", "CPC ($)", "Ads Conv.", "HS contact",
    "Ads Conv Rate", "CPL ($)", "MQLs", "SQLs", "Last Update", "Status", "Report Link", "Tracking Detail ",
]
DUPLICATE = "There's already a result for this campaign and period. Edit that one instead."


@pytest.fixture(autouse=True)
def _clean_budgets():
    yield
    run_sql("DELETE FROM monthly_budgets WHERE month >= DATE '2091-01-01' AND month < DATE '2093-01-01'")


@pytest.fixture(scope="module")
def markets():
    return {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM markets")}


@pytest.fixture
def tag():
    return uuid.uuid4().hex[:6]


def _campaign(client, admin, ref, markets, name, platform="GOOGLE_ADS", brand="OTHERSHIP", market="LONDON"):
    response = client.post(
        "/api/initiatives",
        json={
            "name": name,
            "brand_id": ref["brands"][brand],
            "channel_id": ref["channels"]["PAID_SEARCH"],
            "sub_channel_id": ref["sub_channels"][platform],
            "market_id": markets[market],
            "currency": "USD",
            "budget_amount": 0,
        },
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def google(client, admin, ref, markets, tag):
    return _campaign(client, admin, ref, markets, f"Pytest {tag} - Wfinder - LP1")


@pytest.fixture
def bing(client, admin, ref, markets, tag):
    return _campaign(
        client, admin, ref, markets, f"Pytest {tag} - Desk Booking - Main LP (Bing)", "BING_ADS", "DESKFLEX", "NORTH_AMERICA"
    )


@pytest.fixture
def initiative_viewer(viewer):
    """The temporary regular user, given only the "View Initiatives" permission group."""
    code = f"PYTEST_INITIATIVE_VIEWER_{uuid.uuid4().hex[:8].upper()}"
    role_id = run_sql("INSERT INTO roles (code, name, is_active) VALUES ($1, 'Pytest Initiative Viewer', true) RETURNING id", code)[0]["id"]
    run_sql(
        "INSERT INTO role_permission_groups (role_id, permission_group_id) "
        "SELECT $1, id FROM permission_groups WHERE code = 'MARKETING_VIEW_INITIATIVES'",
        role_id,
    )
    run_sql("INSERT INTO user_roles (user_id, role_id, is_active) VALUES ($1, $2, true)", viewer["id"], role_id)
    return viewer


@pytest.fixture
def other_admin(client):
    user_id = _make_user(super_admin=True)
    try:
        yield {"id": user_id, "headers": _token_for(client, user_id)}
    finally:
        _delete_user(user_id)


def _create(client, user, initiative, **fields):
    body = {"initiative_id": initiative["id"], "period_start": "2091-09-01", **fields}
    return client.post("/api/ads/results", json=body, headers=user["headers"])


def _made(client, user, initiative, **fields):
    response = _create(client, user, initiative, **fields)
    assert response.status_code == 201, response.text
    return response.json()


def _results(client, user, **params):
    response = client.get("/api/ads/results", params=params, headers=user["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def _ads_lines(client, user, initiative):
    response = client.get(f"/api/initiatives/{initiative['id']}/spend-lines", headers=user["headers"])
    assert response.status_code == 200, response.text
    return [line for line in response.json() if line["source"] in ("GOOGLE_ADS", "MICROSOFT_ADS")]


def _ads_outcomes(client, user, initiative) -> dict:
    response = client.get(f"/api/initiatives/{initiative['id']}/outcomes", headers=user["headers"])
    assert response.status_code == 200, response.text
    return {(v["period_start"], v["metric_code"]): v["value"] for v in response.json()["values"] if v["source"] == "ADS"}


def _audit_titles(entity_id):
    return [
        r["title"]
        for r in run_sql(
            f"SELECT {entity_title_sql('al')} AS title FROM audit_logs al "
            "WHERE al.entity_type = 'AdResult' AND al.entity_id = $1 ORDER BY al.created_at",
            str(entity_id),
        )
    ]


# --- Results ---

def test_create_update_delete_with_calculated_fields(client, admin, google):
    created = _made(
        client, admin, google, ad_group=" Ad Group 1a ", period_end="2091-09-21", budget_amount=1000, spend=250,
        impressions=10000, clicks=500, conversions=25, hubspot_contacts=10, mqls=5, sqls=0,
        report_url="https://datastudio.google.com/s/abc", tracking_detail="UTM set",
    )

    assert (created["campaign_name"], created["platform"], created["brand_name"], created["market_name"]) == (
        google["name"], "GOOGLE_ADS", "Othership", "London",
    )
    assert (created["ad_group"], created["currency"], created["status"], created["source"]) == ("Ad Group 1a", "USD", "ACTIVE", "MANUAL")
    assert created["updated_by_name"] == "Pytest User"
    assert {k: created[k] for k in ("ctr", "cpc", "conversion_rate", "cpl", "cost_per_mql", "cost_per_sql", "budget_used_percent")} == {
        "ctr": 5.0, "cpc": 0.5, "conversion_rate": 5.0, "cpl": 25.0, "cost_per_mql": 50.0,
        "cost_per_sql": None,  # zero SQLs
        "budget_used_percent": 25.0,
    }

    # Nothing but the period: every figure, and so every calculation, is empty
    bare = _made(client, admin, google, period_start="2091-10-01")
    assert all(bare[k] is None for k in ("spend", "impressions", "ctr", "cpc", "conversion_rate", "cpl", "budget_used_percent"))

    changed = client.patch(
        f"/api/ads/results/{created['id']}", json={"spend": 300, "impressions": None, "status": "PAUSED"}, headers=admin["headers"]
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert (body["spend"], body["cpl"], body["ctr"], body["cpc"], body["status"]) == (300, 30.0, None, 0.6, "PAUSED")

    backwards = client.patch(f"/api/ads/results/{created['id']}", json={"period_end": "2091-08-01"}, headers=admin["headers"])
    assert (backwards.status_code, backwards.json()["detail"]) == (422, "The period can't end before it starts.")
    cleared = client.patch(f"/api/ads/results/{created['id']}", json={"period_start": None}, headers=admin["headers"])
    assert cleared.status_code == 422

    assert client.delete(f"/api/ads/results/{created['id']}", headers=admin["headers"]).status_code == 204
    assert client.delete(f"/api/ads/results/{created['id']}", headers=admin["headers"]).status_code == 404
    assert [r["id"] for r in _results(client, admin, initiative_id=google["id"])["results"]] == [bare["id"]]
    assert _audit_titles(created["id"]) == [f"{google['name']} · Ad Group 1a · Sep 2091"] * 3


def test_duplicate_campaign_and_period(client, admin, google):
    _made(client, admin, google)
    again = _create(client, admin, google, spend=10)
    assert (again.status_code, again.json()["detail"]) == (422, DUPLICATE)

    # Another ad group (or the same name in another month) is fine
    group = _made(client, admin, google, ad_group="Ad Group 1a")
    assert _create(client, admin, google, ad_group="Ad Group 1a").json()["detail"] == DUPLICATE
    moved = client.patch(f"/api/ads/results/{group['id']}", json={"ad_group": None}, headers=admin["headers"])
    assert (moved.status_code, moved.json()["detail"]) == (422, DUPLICATE)


def test_only_ad_campaigns(client, admin, ref, google):
    seo = client.post(
        "/api/initiatives",
        json={"name": "Pytest SEO", "brand_id": ref["brands"]["OTHERSHIP"], "channel_id": ref["channels"]["SEO"], "currency": "USD",
              "sub_channel_id": ref["sub_channels"]["ORGANIC_SEARCH"], "budget_amount": 0},
        headers=admin["headers"],
    ).json()
    refused = _create(client, admin, seo)
    assert (refused.status_code, refused.json()["detail"]) == (
        422, "Ad results are for Paid Search and Paid Social campaigns with an ad platform."
    )
    assert _create(client, admin, {"id": str(uuid.uuid4())}).status_code == 404
    assert _create(client, admin, google, report_url="http://example.com").status_code == 422
    assert _create(client, admin, google, spend=-1).status_code == 422
    assert _create(client, admin, google, period_end="2091-08-01").status_code == 422
    picker = client.get("/api/ads/campaigns", headers=admin["headers"]).json()
    assert seo["id"] not in {c["initiative_id"] for c in picker}


def test_permissions(client, admin, viewer, initiative_viewer, google):
    result = _made(client, admin, google, spend=10)
    headers = initiative_viewer["headers"]

    assert client.get("/api/ads/results", headers=headers).status_code == 200
    assert client.get("/api/ads/campaigns", params={"platform": "GOOGLE_ADS"}, headers=headers).status_code == 200
    assert client.get("/api/ads/template", headers=headers).status_code == 200
    assert client.post("/api/ads/results", json={"initiative_id": google["id"], "period_start": "2091-11-01"}, headers=headers).status_code == 403
    assert client.patch(f"/api/ads/results/{result['id']}", json={"spend": 1}, headers=headers).status_code == 403
    assert client.delete(f"/api/ads/results/{result['id']}", headers=headers).status_code == 403
    upload = client.post("/api/ads/imports", files={"file": ("ppc.xlsx", _xlsx([SHEET_HEADER]), "application/octet-stream")}, headers=headers)
    assert upload.status_code == 403


def test_campaign_picker(client, admin, google, bing):
    _made(client, admin, google, period_start="2091-09-01")
    _made(client, admin, google, period_start="2091-10-01")

    picked = {c["initiative_id"]: c for c in client.get("/api/ads/campaigns", headers=admin["headers"]).json()}
    assert (picked[google["id"]]["result_count"], picked[google["id"]]["last_period_start"]) == (2, "2091-10-01")
    assert (picked[bing["id"]]["platform"], picked[bing["id"]]["result_count"], picked[bing["id"]]["market_name"]) == ("BING_ADS", 0, "North America")
    only_bing = client.get("/api/ads/campaigns", params={"platform": "BING_ADS"}, headers=admin["headers"]).json()
    assert google["id"] not in {c["initiative_id"] for c in only_bing} and bing["id"] in {c["initiative_id"] for c in only_bing}


# --- What results feed ---

def test_spend_lines_follow_results(client, admin, google, bing):
    google_result = _made(client, admin, google, ad_group="Ad Group 1a", period_end="2091-09-21", spend=250)
    bing_result = _made(client, admin, bing, period_start="2091-10-02", spend=99.5)
    _made(client, admin, google, period_start="2091-12-01")  # no spend, no cost

    [line] = _ads_lines(client, admin, google)
    assert (line["source"], line["category_code"], line["amount"], line["status"], line["currency"]) == (
        "GOOGLE_ADS", "MEDIA_ADS", 250, "INVOICED", "USD",
    )
    assert (line["vendor"], line["incurred_on"], line["lock_reason"]) == ("Google Ads", "2091-09-01", "ADS_IMPORT")
    assert line["description"] == f"{google['name']} · Ad Group 1a · 1 Sep 2091 to 21 Sep 2091"
    [bing_line] = _ads_lines(client, admin, bing)
    assert (bing_line["source"], bing_line["vendor"], bing_line["amount"]) == ("MICROSOFT_ADS", "Microsoft Advertising", 99.5)
    assert bing_line["description"] == f"{bing['name']} · from 2 Oct 2091"
    # People can't edit them: they follow the results
    edit = client.patch(f"/api/spend-lines/{line['id']}", json={"amount": 1}, headers=admin["headers"])
    assert edit.status_code == 422

    client.patch(f"/api/ads/results/{google_result['id']}", json={"spend": 300, "period_start": "2091-09-02"}, headers=admin["headers"])
    [line] = _ads_lines(client, admin, google)
    assert (line["amount"], line["incurred_on"]) == (300, "2091-09-02")

    client.patch(f"/api/ads/results/{google_result['id']}", json={"spend": 0}, headers=admin["headers"])
    assert _ads_lines(client, admin, google) == []
    client.patch(f"/api/ads/results/{google_result['id']}", json={"spend": 40}, headers=admin["headers"])
    assert len(_ads_lines(client, admin, google)) == 1
    client.delete(f"/api/ads/results/{google_result['id']}", headers=admin["headers"])
    assert _ads_lines(client, admin, google) == []
    client.delete(f"/api/ads/results/{bing_result['id']}", headers=admin["headers"])
    assert _ads_lines(client, admin, bing) == []


def test_monthly_budgets_count_ads_spend(client, admin, ref, markets, google):
    budget = client.post(
        "/api/budgets",
        json={"month": "2091-09-01", "brand_id": ref["brands"]["OTHERSHIP"], "market_id": markets["LONDON"],
              "channel_id": ref["channels"]["PAID_SEARCH"], "amount": 1000, "currency": "USD"},
        headers=admin["headers"],
    )
    assert budget.status_code == 201, budget.text
    _made(client, admin, google, period_start="2091-09-05", spend=400)
    _made(client, admin, google, period_start="2091-09-05", ad_group="Ad Group 1a", spend=100)

    month = client.get("/api/budgets", params={"month": "2091-09-01"}, headers=admin["headers"]).json()
    [row] = [b for b in month["budgets"] if b["id"] == budget.json()["id"]]
    assert (row["spent"], row["expected"], row["remaining"]) == (500, 500, 500)


def test_outcomes_sum_per_month_and_win_over_typed_in(client, admin, google):
    _made(client, admin, google, period_start="2091-09-01", impressions=1000, clicks=50, conversions=2.5, hubspot_contacts=4, mqls=2)
    _made(client, admin, google, period_start="2091-09-15", ad_group="Ad Group 1a", impressions=500, clicks=25, hubspot_contacts=1)
    october = _made(client, admin, google, period_start="2091-10-01", clicks=7)

    assert _ads_outcomes(client, admin, google) == {
        ("2091-09-01", "IMPRESSIONS"): 1500, ("2091-09-01", "CLICKS"): 75, ("2091-09-01", "CONVERSIONS"): 2.5,
        ("2091-09-01", "LEADS"): 5, ("2091-09-01", "MQLS"): 2, ("2091-10-01", "CLICKS"): 7,
    }

    typed = client.put(
        f"/api/initiatives/{google['id']}/outcomes/2091-10-01",
        json={"values": [{"metric_code": "CLICKS", "value": 999}, {"metric_code": "SQLS", "value": 3}]},
        headers=admin["headers"],
    )
    assert typed.status_code == 200, typed.text
    totals = client.get(f"/api/initiatives/{google['id']}/performance", headers=admin["headers"]).json()["totals"]
    assert (totals["CLICKS"], totals["IMPRESSIONS"], totals["SQLS"]) == (82, 1500, 3)  # synced clicks win in October

    client.patch(f"/api/ads/results/{october['id']}", json={"clicks": 8}, headers=admin["headers"])
    outcomes = _ads_outcomes(client, admin, google)
    assert outcomes[("2091-10-01", "CLICKS")] == 8
    # Typed-in values are kept
    manual = run_sql(
        "SELECT count(*) AS n FROM outcomes WHERE initiative_id = $1 AND source = 'MANUAL'", uuid.UUID(google["id"])
    )[0]["n"]
    assert manual == 2

    client.delete(f"/api/ads/results/{october['id']}", headers=admin["headers"])
    assert ("2091-10-01", "CLICKS") not in _ads_outcomes(client, admin, google)


def test_reconciled_lines_are_left_alone(client, admin, google):
    result = _made(client, admin, google, spend=100)
    [line] = _ads_lines(client, admin, google)
    run_sql("UPDATE spend_lines SET reconciled_month = DATE '2091-09-01', reconciled_at = now() WHERE id = $1", uuid.UUID(line["id"]))

    changed = client.patch(f"/api/ads/results/{result['id']}", json={"spend": 150}, headers=admin["headers"])
    assert changed.status_code == 200, changed.text
    assert (changed.json()["spend"], changed.json()["spend_conflicts"]) == (150, 1)
    [line] = _ads_lines(client, admin, google)
    assert line["amount"] == 100

    # Removing the result leaves the reconciled cost too
    assert client.delete(f"/api/ads/results/{result['id']}", headers=admin["headers"]).status_code == 204
    assert [l["amount"] for l in _ads_lines(client, admin, google)] == [100]  # noqa: E741


# --- Listing ---

def test_filters_and_totals(client, admin, ref, markets, tag, google, bing):
    other = _campaign(client, admin, ref, markets, f"Pytest {tag} - AU - Main LP", brand="WORKAWARE", market="AUSTRALIA")
    sept = _made(client, admin, google, period_start="1991-09-01", period_end="1991-09-21", budget_amount=1000, spend=400,
                 impressions=10000, clicks=200, hubspot_contacts=4)
    ongoing = _made(client, admin, bing, period_start="1991-10-01", budget_amount=500, spend=100, impressions=2000, clicks=100,
                    status="ACTIVE")
    august = _made(client, admin, other, period_start="1991-08-01", period_end="1991-08-31", spend=50, status="ENDED")
    # Years before any real data, so open-ended results in the local database never overlap these windows
    window = {"from": "1991-01-01", "to": "1991-12-31"}

    everything = _results(client, admin, **window)
    assert [r["id"] for r in everything["results"]] == [ongoing["id"], sept["id"], august["id"]]  # newest period first
    totals = {t["platform"]: t for t in everything["totals"]}
    assert set(totals) == {"GOOGLE_ADS", "BING_ADS", "ALL"}
    assert (totals["GOOGLE_ADS"]["spend"], totals["GOOGLE_ADS"]["campaign_count"]) == (450, 2)
    assert (totals["GOOGLE_ADS"]["budget_used_percent"], totals["GOOGLE_ADS"]["cpl"]) == (45.0, 112.5)
    all_ = totals["ALL"]
    assert (all_["spend"], all_["impressions"], all_["clicks"], all_["campaign_count"], all_["currency"]) == (550, 12000, 300, 3, "USD")
    assert (all_["ctr"], all_["cpc"], all_["conversions"], all_["mqls"]) == (2.5, 1.83, None, None)

    ids = lambda **p: [r["id"] for r in _results(client, admin, **window, **p)["results"]]  # noqa: E731
    assert ids(platform="BING_ADS") == [ongoing["id"]]
    assert ids(brand_id=ref["brands"]["WORKAWARE"]) == [august["id"]]
    assert ids(market_id=markets["LONDON"]) == [sept["id"]]
    assert ids(initiative_id=google["id"]) == [sept["id"]]
    assert ids(status="ENDED") == [august["id"]]
    # Overlap: September 21 is in Sept's period; the ongoing Bing period counts for any later date
    assert set(_ids_between(client, admin, "1991-09-21", "1991-09-30")) == {sept["id"]}
    assert set(_ids_between(client, admin, "1992-06-01", "1992-06-30")) == {ongoing["id"]}
    assert set(_ids_between(client, admin, "1991-08-15", "1991-09-01")) == {sept["id"], august["id"]}
    empty = _results(client, admin, **{"from": "1991-01-01", "to": "1991-01-31"})
    assert empty == {"results": [], "totals": []}


def _ids_between(client, user, start, end):
    return [r["id"] for r in _results(client, user, **{"from": start, "to": end})["results"]]


# --- PPC sheet upload ---

def _xlsx(rows: list[list], other_sheet_first: bool = False) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    if other_sheet_first:
        # Marketing's workbook starts with a reference sheet; the campaigns are further along
        sheet.title = "Reference Lists & Marketing Rul"
        sheet.append(["Brand", "Market/Region", "Rule"])
        sheet.append(["Othership", "London", "Keep CPL under 300"])
        sheet = workbook.create_sheet("SaaS PPC Campaigns")
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _row(brand, market, start, end, platform, name, budget=None, spend=None, impressions=None, clicks=None,
         conversions=None, contacts=None, status=None, link=None, ctr=None, cpl=None, conv_rate=None, mqls=None, sqls=None):
    return [brand, market, start, end, "Paid Search", platform, name, None, budget, spend, impressions, clicks, ctr, None,
            conversions, contacts, conv_rate, cpl, mqls, sqls, datetime(2092, 9, 2), status, link, None]


def _upload(client, user, data: bytes, filename="ppc.xlsx"):
    return client.post("/api/ads/imports", files={"file": (filename, data, "application/octet-stream")}, headers=user["headers"])


@pytest.fixture
def sheet_campaigns(client, admin, ref, markets, tag, google, bing):
    """LP1 (Google) with two results already, Desk Booking on Google, and Desk Booking (Bing)."""
    desk = _campaign(client, admin, ref, markets, f"Pytest {tag} - Desk Booking - Main LP", brand="DESKFLEX", market="NORTH_AMERICA")
    august = _made(client, admin, google, period_start="2092-08-03", period_end="2092-08-31", budget_amount=4850, spend=4311,
                   impressions=21432, clicks=1086, conversions=39, hubspot_contacts=15, status="PAUSED")
    july = _made(client, admin, google, period_start="2092-07-01", period_end="2092-07-31", spend=1000, clicks=100,
                 status="PAUSED")
    return {"lp1": google, "desk": desk, "bing": bing, "august": august, "july": july}


def _sheet(tag) -> bytes:
    lp1, desk, lp9 = f"Pytest {tag} - Wfinder - LP1", f"Pytest {tag} - Desk Booking - Main LP", f"Pytest {tag} - Wfinder - LP9"
    rows = [
        SHEET_HEADER,
        # 2: an existing result with a new spend figure (calculated columns are ignored)
        _row("Othership", "London", datetime(2092, 8, 3), datetime(2092, 8, 31), "Google Ads", lp1, 4850, 4500, 21432, 1086,
             39, 15, "Paused", "https://datastudio.google.com/s/rIadGwNNhJ4", ctr=0.0506, cpl=287.4, conv_rate=0.0359),
        # 3: the same as the portal has
        _row("Othership", "London", "2092-07-01", "2092-07-31", "Google Ads", lp1, None, 1000, None, 100, status="paused"),
        # 4: an ad group of LP1, with "." and spreadsheet errors in the figure columns
        _row("Othership", "London", datetime(2092, 9, 1), datetime(2092, 9, 21), "Google Ads", f"{lp1} - Ad Group 1a", None,
             3379.04, 15260, 776, ".", ".", "Paused", "https://datastudio.google.com/s/x", conv_rate="#REF!", cpl="#VALUE!"),
        # 5: an ongoing Google row with no end date
        _row("DeskFlex", "North America", datetime(2092, 9, 1), None, "Google Ads", desk, 9000, 9000, 19200, 698, None, 22,
             "ongoing", "https://datastudio.google.com/s/jzam30vttkI", conv_rate=0),
        # 6: the same campaign on Bing, matched to "... (Bing)"; its link isn't https
        _row("DeskFlex", "North America", datetime(2092, 9, 21), None, "Bing Ads", desk, 3000, 120, 900, 30,
             link="http://datastudio.google.com/s/old"),
        # 7 and 8: a campaign the portal doesn't have yet, and one of its ad groups
        _row("WorkAware", "Australia", datetime(2092, 9, 23), None, "Google Ads", lp9, 7800, 500),
        _row("WorkAware", "Australia", datetime(2092, 9, 19), None, "Google Ads", f"{lp9} - Ad Group 2b", None, 80),
        # 9: not Google or Bing
        _row("Othership", "London", datetime(2092, 9, 1), None, "Facebook Ads", lp1, 100),
        # 10: unknown brand and no start
        _row("Acme", "London", None, None, "Google Ads", lp1, 100),
        # 11: row 5 again
        _row("DeskFlex", "North America", datetime(2092, 9, 1), None, "Google Ads", desk, 1, 1),
        # 12: unreadable figures and status
        _row("Othership", "London", datetime(2092, 10, 1), None, "Google Ads", lp1, None, -5, 10.5, "abc", status="sleeping"),
    ]
    return _xlsx(rows, other_sheet_first=True)


def test_import_preview(client, admin, tag, sheet_campaigns):
    response = _upload(client, admin, _sheet(tag))

    assert response.status_code == 201, response.text
    preview = response.json()
    rows = {r["row_number"]: r for r in preview["rows"]}
    assert preview["counts"] == {"new": 3, "update": 1, "unchanged": 1, "invalid": 4, "new_campaign": 2}
    lp1, desk, bing = sheet_campaigns["lp1"], sheet_campaigns["desk"], sheet_campaigns["bing"]

    assert (rows[2]["status"], rows[2]["initiative_id"], rows[2]["spend"], rows[2]["messages"]) == ("UPDATE", lp1["id"], 4500, ["Updates spend and report link."])
    assert (rows[2]["result_status"], rows[2]["platform"], rows[2]["brand"]) == ("PAUSED", "GOOGLE_ADS", "Othership")
    assert (rows[3]["status"], rows[3]["messages"]) == ("UNCHANGED", [])

    assert (rows[4]["status"], rows[4]["initiative_id"], rows[4]["campaign_name"], rows[4]["ad_group"]) == ("NEW", lp1["id"], lp1["name"], "Ad Group 1a")
    assert (rows[4]["spend"], rows[4]["conversions"], rows[4]["hubspot_contacts"], rows[4]["messages"]) == (3379.04, None, None, [])

    assert (rows[5]["status"], rows[5]["initiative_id"], rows[5]["period_end"], rows[5]["result_status"]) == ("NEW", desk["id"], None, "ACTIVE")
    assert (rows[6]["status"], rows[6]["initiative_id"], rows[6]["platform"], rows[6]["report_url"]) == ("NEW", bing["id"], "BING_ADS", None)
    assert rows[6]["messages"] == ["The report link isn't an https link, so it was left out."]

    assert (rows[7]["status"], rows[7]["initiative_id"], rows[7]["ad_group"], rows[7]["currency"]) == ("NEW_CAMPAIGN", None, None, "USD")
    assert rows[2]["currency"] == "USD"
    assert rows[7]["messages"] == [f"A new Google Ads campaign called 'Pytest {tag} - Wfinder - LP9' will be added."]
    assert (rows[8]["status"], rows[8]["campaign_name"], rows[8]["ad_group"]) == ("NEW_CAMPAIGN", f"Pytest {tag} - Wfinder - LP9", "Ad Group 2b")

    assert (rows[9]["status"], rows[9]["messages"]) == ("INVALID", ["Only Google Ads and Bing Ads rows can be imported here."])
    assert rows[10]["messages"] == ["Unknown brand 'Acme'.", "Add the period start."]
    assert (rows[11]["status"], rows[11]["messages"]) == ("INVALID", ["Appears twice in this file (row 5)."])
    assert rows[12]["messages"] == [
        "Actual Spend can't be negative.", "Impressions must be a whole number.", "Clicks: 'abc' isn't an amount",
        "Unknown status 'sleeping'. Use ongoing, paused or ended.",
    ]
    # Nothing is saved yet
    assert len(_results(client, admin, initiative_id=lp1["id"])["results"]) == 2


def test_import_confirm_and_undo(client, admin, tag, sheet_campaigns):
    lp1, desk, bing = sheet_campaigns["lp1"], sheet_campaigns["desk"], sheet_campaigns["bing"]
    batch_id = _upload(client, admin, _sheet(tag)).json()["batch_id"]

    confirmed = client.post(f"/api/ads/imports/{batch_id}/confirm", headers=admin["headers"])

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {
        "batch_id": batch_id, "status": "CONFIRMED", "created": 5, "updated": 1, "unchanged": 1,
        "campaigns_created": 1, "skipped": 0, "spend_conflicts": 0,
    }
    lp9 = [c for c in client.get("/api/ads/campaigns", params={"platform": "GOOGLE_ADS"}, headers=admin["headers"]).json()
           if c["name"] == f"Pytest {tag} - Wfinder - LP9"]
    assert [(c["brand_name"], c["market_name"], c["currency"], c["status"], c["result_count"]) for c in lp9] == [
        ("WorkAware", "Australia", "USD", "ACTIVE", 2),
    ]
    [created] = run_sql(
        "SELECT i.start_date, i.owner_user_id, c.code AS channel FROM initiatives i JOIN channels c ON c.id = i.channel_id WHERE i.id = $1",
        uuid.UUID(lp9[0]["initiative_id"]),
    )
    assert (created["start_date"].isoformat(), created["owner_user_id"], created["channel"]) == ("2092-09-19", admin["id"], "PAID_SEARCH")

    lp1_results = {(r["ad_group"], r["period_start"]): r for r in _results(client, admin, initiative_id=lp1["id"])["results"]}
    assert (lp1_results[(None, "2092-08-03")]["spend"], lp1_results[(None, "2092-08-03")]["source"]) == (4500, "SPREADSHEET")
    assert lp1_results[("Ad Group 1a", "2092-09-01")]["spend"] == 3379.04
    assert lp1_results[(None, "2092-07-01")]["source"] == "MANUAL"  # unchanged rows aren't touched
    [desk_result] = _results(client, admin, initiative_id=desk["id"])["results"]
    assert (desk_result["status"], desk_result["period_end"], desk_result["hubspot_contacts"]) == ("ACTIVE", None, 22)
    assert [l["amount"] for l in _ads_lines(client, admin, bing)] == [120]  # noqa: E741
    assert _ads_outcomes(client, admin, bing) == {("2092-09-01", "IMPRESSIONS"): 900, ("2092-09-01", "CLICKS"): 30}
    assert sorted(l["amount"] for l in _ads_lines(client, admin, lp1)) == [1000, 3379.04, 4500]  # noqa: E741

    # Confirming twice is refused
    assert client.post(f"/api/ads/imports/{batch_id}/confirm", headers=admin["headers"]).status_code == 409

    undone = client.post(f"/api/ads/imports/{batch_id}/undo", headers=admin["headers"])

    assert undone.status_code == 200, undone.text
    assert undone.json() == {"batch_id": batch_id, "status": "UNDONE", "removed": 5, "restored": 1, "skipped": 0, "campaigns_removed": 1}
    lp1_results = {(r["ad_group"], r["period_start"]): r for r in _results(client, admin, initiative_id=lp1["id"])["results"]}
    assert set(lp1_results) == {(None, "2092-08-03"), (None, "2092-07-01")}
    august = lp1_results[(None, "2092-08-03")]
    assert (august["spend"], august["source"], august["status"], august["budget_amount"]) == (4311, "MANUAL", "PAUSED", 4850)
    assert sorted(l["amount"] for l in _ads_lines(client, admin, lp1)) == [1000, 4311]  # noqa: E741
    assert _results(client, admin, initiative_id=desk["id"])["results"] == []
    assert _ads_outcomes(client, admin, bing) == {}
    assert run_sql("SELECT count(*) AS n FROM initiatives WHERE id = $1", uuid.UUID(lp9[0]["initiative_id"]))[0]["n"] == 0
    assert client.post(f"/api/ads/imports/{batch_id}/undo", headers=admin["headers"]).status_code == 409


def test_undo_keeps_reconciled_rows(client, admin, tag, sheet_campaigns):
    batch_id = _upload(client, admin, _sheet(tag)).json()["batch_id"]
    client.post(f"/api/ads/imports/{batch_id}/confirm", headers=admin["headers"])
    [line] = _ads_lines(client, admin, sheet_campaigns["bing"])
    run_sql("UPDATE spend_lines SET reconciled_month = DATE '2092-09-01' WHERE id = $1", uuid.UUID(line["id"]))

    undone = client.post(f"/api/ads/imports/{batch_id}/undo", headers=admin["headers"]).json()

    assert (undone["removed"], undone["restored"], undone["skipped"]) == (4, 1, 1)
    assert len(_results(client, admin, initiative_id=sheet_campaigns["bing"]["id"])["results"]) == 1


def test_only_the_uploader_can_confirm_and_previews_expire(client, admin, other_admin, tag, sheet_campaigns):
    batch_id = _upload(client, admin, _sheet(tag)).json()["batch_id"]

    theirs = client.post(f"/api/ads/imports/{batch_id}/confirm", headers=other_admin["headers"])
    assert (theirs.status_code, theirs.json()["detail"]) == (403, "Only the person who uploaded this file can confirm it.")

    run_sql("UPDATE import_batches SET created_at = now() - interval '25 hours' WHERE id = $1", uuid.UUID(batch_id))
    late = client.post(f"/api/ads/imports/{batch_id}/confirm", headers=admin["headers"])
    assert (late.status_code, late.json()["detail"]) == (410, "This preview has expired. Upload the file again.")
    assert run_sql("SELECT status FROM import_batches WHERE id = $1", uuid.UUID(batch_id))[0]["status"] == "EXPIRED"
    assert client.post(f"/api/ads/imports/{uuid.uuid4()}/confirm", headers=admin["headers"]).status_code == 404


def test_upload_needs_the_key_columns(client, admin):
    response = _upload(client, admin, _xlsx([["Brand", "Market/Region", "Clicks"], ["Othership", "London", 5]]))
    assert (response.status_code, response.json()["detail"]) == (
        422, "The sheet needs Sub-Channel, Campaign Name and Period Start columns. Start from the template.",
    )
    assert _upload(client, admin, b"not a sheet", "ppc.pdf").status_code == 422


def test_template(client, admin, tag, sheet_campaigns):
    response = client.get("/api/ads/template", headers=admin["headers"])

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="saas-ppc-campaigns-template.xlsx"'
    workbook = load_workbook(io.BytesIO(response.content))
    sheet = workbook["SaaS PPC Campaigns"]
    assert [c.value for c in sheet[1]] == TEMPLATE_COLUMNS
    assert "Othership" in [c.value for c in workbook["Lists"]["A"]]

    # A filled-in template reads back
    sheet.append(_row("Othership", "London", datetime(2092, 11, 1), None, "Google Ads", sheet_campaigns["lp1"]["name"], 10, 5))
    buffer = io.BytesIO()
    workbook.save(buffer)
    preview = _upload(client, admin, buffer.getvalue()).json()
    assert [(r["status"], r["initiative_id"]) for r in preview["rows"]] == [("NEW", sheet_campaigns["lp1"]["id"])]
