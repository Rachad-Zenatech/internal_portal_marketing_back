"""Microsoft Ads (Bing Ads): connecting (mock and live mode), discovering accounts, and the campaign and weekly results
sync.

Microsoft is never called: mock mode generates everything, and the live-path tests answer the Microsoft Advertising
REST calls with an httpx.MockTransport. Each test uses its own organization, company and ad accounts (ids 5550200..),
and only syncs its own connections; everything it creates is removed afterwards, and the sync state is put back.
"""
import asyncio
import csv
import io
import json
import uuid
import zipfile
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from services import microsoft_ads_client, microsoft_ads_sync_job
from services import token_crypto_service as crypto
from services.microsoft_ads_sync_job import read_window
from services.oauth_adapters import microsoft_ads as microsoft_ads_adapter
from tests.conftest import run_sql

SEARCH = {"account_id": "555020001", "account_name": "DeskFlex Pytest Search", "account_number": "F555PY01",
          "customer_id": "555020900", "currency": "USD", "time_zone": "EasternTimeUSCanada", "is_manager": False}
AUDIENCE = {"account_id": "555020002", "account_name": "DeskFlex Pytest Audience", "account_number": "F555PY02",
            "customer_id": "555020900", "currency": "USD", "time_zone": "EasternTimeUSCanada", "is_manager": False}
LIVE_ACCOUNT = {"account_id": "555020777", "account_name": "Pytest Live Microsoft Account", "customer_id": "555020900",
                "currency": "USD", "time_zone": "EasternTimeUSCanada", "is_manager": False}
TEST_ACCOUNTS = [SEARCH["account_id"], AUDIENCE["account_id"], LIVE_ACCOUNT["account_id"]]
SYNC_NAMES = ["microsoft_ads", "microsoft_ads_mock"]


def _test_initiative_ids() -> list:
    return [r["initiative_id"] for r in run_sql(
        "SELECT initiative_id FROM initiative_links WHERE system = 'MICROSOFT_ADS' AND account_id = ANY($1::text[])",
        TEST_ACCOUNTS,
    )]


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch):
    """Mock mode with this test's accounts, whatever the local .env says."""
    monkeypatch.setenv("MICROSOFT_ADS_MODE", "mock")
    monkeypatch.setenv("MICROSOFT_ADS_CREATE_CAMPAIGNS", "true")
    monkeypatch.setenv("MICROSOFT_ADS_LOOKBACK_DAYS", "90")
    monkeypatch.setattr(microsoft_ads_adapter, "MOCK_ACCOUNTS", [SEARCH, AUDIENCE])
    monkeypatch.setattr(microsoft_ads_client, "REPORT_POLL_SECONDS", 0)


@pytest.fixture
def sync_state(client):
    """The sync state is put back afterwards, and the campaigns made for the test accounts removed."""
    run_sql("DROP TABLE IF EXISTS pytest_microsoft_ads_state")
    run_sql("CREATE TABLE pytest_microsoft_ads_state AS SELECT * FROM integration_sync_state WHERE name = ANY($1::text[])", SYNC_NAMES)
    started = run_sql("SELECT now() AS t")[0]["t"]
    yield
    ids = _test_initiative_ids()
    run_sql("DELETE FROM audit_logs WHERE entity_id = ANY($1::text[])", [str(i) for i in ids])
    # The results and cost lines the sync wrote (no actor)
    run_sql(
        "DELETE FROM audit_logs WHERE actor_user_id IS NULL AND entity_type IN ('AdResult', 'SpendLine') AND created_at >= $1 "
        "AND (new_value->>'initiative_id' = ANY($2::text[]) OR old_value->>'initiative_id' = ANY($2::text[]))",
        started, [str(i) for i in ids],
    )
    run_sql("DELETE FROM initiatives WHERE id = ANY($1::uuid[])", ids)
    run_sql("DELETE FROM integration_sync_state WHERE name = ANY($1::text[])", SYNC_NAMES)
    run_sql("INSERT INTO integration_sync_state SELECT * FROM pytest_microsoft_ads_state")
    run_sql("DROP TABLE pytest_microsoft_ads_state")


@pytest.fixture
def org(sync_state):
    """A temporary organization with one company (brand DeskFlex); its connections go with it."""
    org_id = run_sql(
        "INSERT INTO organizations (name, slug) VALUES ('Pytest Org', $1) RETURNING id", f"pytest-{uuid.uuid4().hex[:10]}"
    )[0]["id"]
    company_id = run_sql(
        "INSERT INTO companies (organization_id, name, code, brand_id) "
        "VALUES ($1, 'Pytest DeskFlex', 'PYTEST', (SELECT id FROM brands WHERE code = 'DESKFLEX')) RETURNING id",
        org_id,
    )[0]["id"]
    try:
        yield {"id": org_id, "company_id": company_id}
    finally:
        run_sql("DELETE FROM audit_logs WHERE entity_id IN (SELECT id::text FROM oauth_connections WHERE organization_id = $1)", org_id)
        run_sql("DELETE FROM organizations WHERE id = $1", org_id)


def _only_connections(monkeypatch, connection_ids):
    """The sync reads only these connections (other connections in the local database are left alone)."""
    original = microsoft_ads_sync_job.usable_connections

    async def only_these(conn, mode, _ids=None):
        return await original(conn, mode, [uuid.UUID(str(i)) for i in connection_ids])

    monkeypatch.setattr(microsoft_ads_sync_job, "usable_connections", only_these)


def _connect_mock(client, admin, org) -> dict:
    response = client.post(
        "/api/oauth/connect-instant/MICROSOFT_ADS", headers=admin["headers"],
        json={"organization_id": str(org["id"]), "company_id": str(org["company_id"])},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sync(client, admin) -> dict:
    response = client.post("/api/integrations/microsoft-ads/sync", headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def _results(initiative_ids) -> list:
    return [dict(r) for r in run_sql(
        "SELECT id, initiative_id, ad_group, period_start, period_end, spend, impressions, clicks, conversions, "
        "hubspot_contacts, mqls, sqls, status, source FROM ad_results WHERE initiative_id = ANY($1::uuid[]) "
        "ORDER BY initiative_id, period_start",
        initiative_ids,
    )]


# --- Rules ---

def test_mode_defaults_and_connection_mode(monkeypatch):
    monkeypatch.delenv("MICROSOFT_ADS_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    assert microsoft_ads_client.microsoft_ads_mode() == "mock"
    monkeypatch.setenv("APP_ENV", "production")
    assert microsoft_ads_client.microsoft_ads_mode() == "live"
    monkeypatch.setenv("MICROSOFT_ADS_MODE", "mock")
    assert microsoft_ads_client.microsoft_ads_mode() == "mock"
    # A connection made before the mode was recorded: mock when its token is a fake one
    assert microsoft_ads_client.connection_mode({}, crypto.encrypt_token("EwBoA_msft_ads_123")) == "mock"
    assert microsoft_ads_client.connection_mode({}, crypto.encrypt_token("EwAoA8l6BAAU-real")) == "live"
    assert microsoft_ads_client.connection_mode({"mode": "live"}, crypto.encrypt_token("EwBoA_msft_ads_123")) == "live"


def test_campaign_parsing_maps_microsoft_statuses_and_types():
    parsed = microsoft_ads_client.parse_campaign({
        "Id": 412, "Name": " Brand ", "Status": "BudgetPaused", "CampaignType": "Audience", "DailyBudget": 25.5,
        "StartDate": {"Day": 5, "Month": 1, "Year": 2026}, "EndDate": None,
    })
    assert parsed == {
        "campaign_id": "412", "name": "Brand", "status": "PAUSED", "channel_type": "AUDIENCE",
        "start_date": date(2026, 1, 5), "end_date": None, "daily_budget": Decimal("25.50"), "total_budget": None,
        "weeks": {},
    }
    assert microsoft_ads_client.parse_campaign({"Id": 1, "Status": "Deleted"})["status"] == "REMOVED"
    assert microsoft_ads_client.parse_campaign({"Id": 1, "Status": "Active"})["status"] == "ENABLED"


def test_mock_data_is_stable_with_lower_volumes_than_google():
    from services.google_ads_mock_data import mock_account_campaigns as google_campaigns
    window = read_window(date(2026, 10, 20))
    first = microsoft_ads_sync_job.mock_account_campaigns(SEARCH, *window)
    assert first == microsoft_ads_sync_job.mock_account_campaigns(SEARCH, *window)
    assert 4 <= len(first) <= 7 and first[0]["name"] == "DeskFlex - Brand Terms - Search"
    assert all(c["name"].startswith("DeskFlex - ") for c in first)
    google = google_campaigns({"account_id": "412-880-9921", "account_name": "DeskFlex"}, *window)
    spend = sum(w["spend"] for c in first for w in c["weeks"].values())
    assert 0 < spend < sum(w["spend"] for c in google for w in c["weeks"].values())


# --- Mock mode end to end ---

def test_mock_connect_then_sync_creates_campaigns_results_and_costs(client, admin, org, monkeypatch):
    connected = _connect_mock(client, admin, org)
    connection = connected["connection"]
    assert connection["connection_metadata"]["mode"] == "mock"
    assert [a["account_id"] for a in connected["discovered_accounts"]] == [SEARCH["account_id"], AUDIENCE["account_id"]]
    _only_connections(monkeypatch, [connection["id"]])

    summary = _sync(client, admin)
    assert summary["mode"] == "mock"
    assert [r["account_id"] for r in summary["account_runs"]] == [SEARCH["account_id"], AUDIENCE["account_id"]]
    window = read_window(date.today())
    expected = [c for a in (SEARCH, AUDIENCE) for c in microsoft_ads_sync_job.mock_account_campaigns(a, *window)]
    assert summary["campaigns_created"] == len(expected)
    weeks = sum(len(c["weeks"]) for c in expected)
    assert summary["results_created"] == weeks > 0

    campaigns = {r["external_id"]: dict(r) for r in run_sql(
        """
        SELECT l.external_id, l.account_id, i.id, i.name, i.status, i.currency, i.data_source, i.channel_details,
               i.description, b.code AS brand, ch.code AS channel, sc.code AS sub_channel, i.created_by
        FROM initiative_links l JOIN initiatives i ON i.id = l.initiative_id
        JOIN brands b ON b.id = i.brand_id JOIN channels ch ON ch.id = i.channel_id
        JOIN sub_channels sc ON sc.id = i.sub_channel_id
        WHERE l.system = 'MICROSOFT_ADS' AND l.account_id = ANY($1::text[])
        """,
        TEST_ACCOUNTS,
    )}
    assert set(campaigns) == {c["campaign_id"] for c in expected}
    for c in expected:
        row = campaigns[c["campaign_id"]]
        assert row["name"] == c["name"] and row["brand"] == "DESKFLEX" and row["created_by"] is None
        assert (row["channel"], row["sub_channel"], row["data_source"], row["currency"]) == ("PAID_SEARCH", "BING_ADS", "LINKED", "USD")
        assert row["description"] == f"Added by the Microsoft Ads sync from account {row['account_id']} (mock data)."
        details = row["channel_details"] if isinstance(row["channel_details"], dict) else json.loads(row["channel_details"])
        assert details["campaign_objective"] == ("AWARENESS" if c["channel_type"] == "AUDIENCE" else "LEADS")
        assert row["status"] == microsoft_ads_sync_job.shared.initiative_status(c["status"], c["start_date"], c["end_date"], date.today())

    ids = [r["id"] for r in campaigns.values()]
    results = _results(ids)
    assert len(results) == weeks
    assert all(r["source"] == "MICROSOFT_ADS_API" and r["ad_group"] is None for r in results)
    assert all(r["period_start"].weekday() == 0 and r["period_end"] == r["period_start"] + timedelta(days=6) for r in results)
    # Spend mirrored into one Microsoft Advertising cost line per result with spend
    lines = run_sql("SELECT external_ref, amount, source FROM spend_lines WHERE initiative_id = ANY($1::uuid[])", ids)
    assert len(lines) == sum(1 for r in results if r["spend"] and r["spend"] > 0)
    assert {line["source"] for line in lines} == {"MICROSOFT_ADS"}

    # Status and data health
    status = client.get("/api/integrations/microsoft-ads/sync-status", headers=admin["headers"]).json()
    assert status["mode"] == "mock" and status["configured"] is True and status["last_success_at"]
    assert status["last_run_summary"]["campaigns_created"] == len(expected)
    health = client.get("/api/ads/data-health", headers=admin["headers"]).json()
    bing = next(p for p in health["platforms"] if p["platform"] == "BING_ADS")
    assert bing["connection_status"] == "CONNECTED" and bing["source"] == "API"
    assert bing["last_sync_at"] and bing["sync_mode"] == "mock"


def test_rerun_updates_in_place_and_keeps_hubspot_figures(client, admin, org, monkeypatch):
    connection = _connect_mock(client, admin, org)["connection"]
    _only_connections(monkeypatch, [connection["id"]])
    first = _sync(client, admin)
    ids = _test_initiative_ids()
    before = _results(ids)
    lines_before = run_sql("SELECT count(*) AS n FROM spend_lines WHERE initiative_id = ANY($1::uuid[])", ids)[0]["n"]

    hubspot, edited = before[0], before[1]
    run_sql("UPDATE ad_results SET hubspot_contacts = 7, mqls = 3, sqls = 1 WHERE id = $1", hubspot["id"])
    run_sql("UPDATE ad_results SET clicks = clicks + 1000 WHERE id = $1", edited["id"])
    # A week Microsoft no longer reports, inside the window
    monday = date.today() - timedelta(days=date.today().weekday())
    stale = run_sql(
        "INSERT INTO ad_results (initiative_id, period_start, period_end, spend, source) "
        "VALUES ($1, $2, $3, 11, 'MICROSOFT_ADS_API') ON CONFLICT DO NOTHING RETURNING id",
        ids[0], monday + timedelta(days=7), monday + timedelta(days=13),
    )

    second = _sync(client, admin)
    assert second["campaigns_created"] == 0 and second["campaigns_unchanged"] == first["campaigns_created"]
    assert second["results_created"] == 0 and second["results_updated"] == 1
    assert second["results_removed"] == len(stale) == 1
    assert len(_test_initiative_ids()) == len(ids)

    after = {r["id"]: r for r in _results(ids)}
    assert (after[hubspot["id"]]["hubspot_contacts"], after[hubspot["id"]]["mqls"], after[hubspot["id"]]["sqls"]) == (7, 3, 1)
    assert after[edited["id"]]["clicks"] == edited["clicks"]
    assert len(after) == len(before)
    lines_after = run_sql("SELECT count(*) AS n FROM spend_lines WHERE initiative_id = ANY($1::uuid[])", ids)[0]["n"]
    assert lines_after == lines_before

    # A third run changes nothing
    third = _sync(client, admin)
    assert (third["campaigns_created"], third["campaigns_updated"], third["results_created"], third["results_updated"],
            third["results_removed"]) == (0, 0, 0, 0, 0)


def test_rerun_follows_microsoft_status_and_keeps_a_given_name(client, admin, org, monkeypatch):
    connection = _connect_mock(client, admin, org)["connection"]
    _only_connections(monkeypatch, [connection["id"]])
    _sync(client, admin)
    ids = _test_initiative_ids()
    sync_made, named = ids[0], ids[1]
    run_sql("UPDATE initiatives SET status = 'DRAFT', name = 'Renamed by sync test' WHERE id = $1", sync_made)
    run_sql("UPDATE initiatives SET name = 'Our own name', created_by = $2 WHERE id = $1", named, admin["id"])

    summary = _sync(client, admin)
    assert summary["campaigns_updated"] == 1
    rows = {r["id"]: r for r in run_sql("SELECT id, name, status FROM initiatives WHERE id = ANY($1::uuid[])", [sync_made, named])}
    assert rows[sync_made]["status"] != "DRAFT" and rows[sync_made]["name"] != "Renamed by sync test"
    assert rows[named]["name"] == "Our own name"
    run_sql("UPDATE initiatives SET created_by = NULL WHERE id = $1", named)  # so the fixture removes it


# --- Live mode ---

def _no_credentials(monkeypatch):
    for name in ("MICROSOFT_ADS_CLIENT_ID", "MICROSOFT_ADS_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    # The portal's sign-in app is never used for Microsoft Ads
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "portal-sso-app")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "portal-sso-secret")


def test_live_mode_without_credentials_refuses_to_connect(client, admin, org, monkeypatch):
    monkeypatch.setenv("MICROSOFT_ADS_MODE", "live")
    _no_credentials(monkeypatch)
    response = client.post(
        "/api/oauth/connect-instant/MICROSOFT_ADS", headers=admin["headers"], json={"organization_id": str(org["id"])}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Microsoft Ads isn't set up: add MICROSOFT_ADS_CLIENT_ID and MICROSOFT_ADS_CLIENT_SECRET"
    response = client.get(f"/api/oauth/authorize/MICROSOFT_ADS?organization_id={org['id']}", headers=admin["headers"])
    assert response.status_code == 400 and "isn't set up" in response.json()["detail"]
    assert run_sql("SELECT count(*) AS n FROM oauth_connections WHERE organization_id = $1", org["id"])[0]["n"] == 0
    status = client.get("/api/integrations/microsoft-ads/sync-status", headers=admin["headers"]).json()
    assert status["mode"] == "live" and status["configured"] is False


def test_live_mode_refuses_test_connections(client, admin, org, monkeypatch):
    monkeypatch.setenv("MICROSOFT_ADS_MODE", "live")
    monkeypatch.setenv("MICROSOFT_ADS_CLIENT_ID", "ads-app")
    monkeypatch.setenv("MICROSOFT_ADS_CLIENT_SECRET", "ads-secret")
    response = client.post(
        "/api/oauth/connect-instant/MICROSOFT_ADS", headers=admin["headers"], json={"organization_id": str(org["id"])}
    )
    assert response.status_code == 400 and "live mode" in response.json()["detail"]
    response = client.get(f"/api/oauth/authorize/MICROSOFT_ADS?organization_id={org['id']}", headers=admin["headers"])
    url = httpx.URL(response.json()["authorization_url"])
    assert url.host == "login.microsoftonline.com" and url.params["client_id"] == "ads-app"
    assert url.params["scope"] == "https://ads.microsoft.com/msads.manage offline_access"


def test_mock_sign_in_returns_to_the_app(client, admin, org):
    response = client.get(
        f"/api/oauth/authorize/MICROSOFT_ADS?organization_id={org['id']}&redirect_uri=http://localhost:5180/oauth/callback",
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorization_url"].startswith("http://localhost:5180/oauth/callback?code=mock_microsoft_ads_")
    code = httpx.URL(body["authorization_url"]).params["code"]
    response = client.post("/api/oauth/callback", headers=admin["headers"], json={
        "provider": "MICROSOFT_ADS", "code": code, "state": body["state"],
        "redirect_uri": "http://localhost:5180/oauth/callback",
    })
    assert response.status_code == 200, response.text
    connected = response.json()
    assert connected["connection"]["connection_metadata"]["mode"] == "mock"
    assert [a["account_id"] for a in connected["discovered_accounts"]] == [SEARCH["account_id"], AUDIENCE["account_id"]]


def _store_live_connection(org) -> uuid.UUID:
    meta = {"mode": "live", "discovered_accounts": [LIVE_ACCOUNT]}
    return run_sql(
        """
        INSERT INTO oauth_connections (organization_id, provider, provider_account_id, account_name,
                                       access_token_encrypted, expires_at, status, connection_metadata)
        VALUES ($1, 'MICROSOFT_ADS', $2, $3, $4, now() + interval '1 hour', 'CONNECTED', CAST($5 AS jsonb))
        RETURNING id
        """,
        org["id"], LIVE_ACCOUNT["account_id"], LIVE_ACCOUNT["account_name"], crypto.encrypt_token("live-token"),
        json.dumps(meta),
    )[0]["id"]


def _transport(handler):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory


def _zipped_csv(rows: list[dict]) -> bytes:
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=["TimePeriod", "CampaignId", "Spend", "Impressions", "Clicks", "Conversions"])
    writer.writeheader()
    writer.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("report.csv", "﻿" + text.getvalue())
    return out.getvalue()


def _campaign(cid, name, status, start, campaign_type="Search", daily=50.0):
    return {"Id": cid, "Name": name, "Status": status, "CampaignType": campaign_type, "DailyBudget": daily,
            "BudgetType": "DailyBudgetStandard", "StartDate": start, "EndDate": None}


def test_live_sync_maps_api_rows_and_ignores_mock_connections(client, admin, org, monkeypatch):
    mock_connection = _connect_mock(client, admin, org)["connection"]
    live_id = _store_live_connection(org)
    monkeypatch.setenv("MICROSOFT_ADS_MODE", "live")
    monkeypatch.setenv("MICROSOFT_ADS_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_ADS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("MICROSOFT_ADS_DEVELOPER_TOKEN", "dev-token")
    _only_connections(monkeypatch, [mock_connection["id"], live_id])

    week = read_window(date.today())[0] + timedelta(days=7)
    future = date.today() + timedelta(days=30)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.host == "reports.example.test":
            return httpx.Response(200, content=_zipped_csv([
                {"TimePeriod": week.isoformat(), "CampaignId": "70000001", "Spend": "123.45", "Impressions": "1000",
                 "Clicks": "50", "Conversions": "2.5"},
                {"TimePeriod": week.strftime("%m/%d/%Y"), "CampaignId": "70000002", "Spend": "10", "Impressions": "300",
                 "Clicks": "4", "Conversions": "0"},
                {"TimePeriod": week.isoformat(), "CampaignId": "99999999", "Spend": "5", "Impressions": "1",
                 "Clicks": "0", "Conversions": "0"},
            ]))
        assert request.headers["authorization"] == "Bearer live-token"
        assert request.headers["developertoken"] == "dev-token"
        assert request.headers["customeraccountid"] == LIVE_ACCOUNT["account_id"]
        assert request.headers["customerid"] == LIVE_ACCOUNT["customer_id"]
        body = json.loads(request.content)
        if request.url.path.endswith("/Campaigns/QueryByAccountId"):
            assert body["AccountId"] == LIVE_ACCOUNT["account_id"] and "Audience" in body["CampaignType"]
            return httpx.Response(200, json={"Campaigns": [
                _campaign(70000001, "Pytest Live - Brand - Search", "Active", "2026-01-05"),
                _campaign(70000002, "Pytest Live - Old - Audience", "Deleted", {"Day": 5, "Month": 1, "Year": 2026}, "Audience"),
                _campaign(70000003, "Pytest Live - Removed Unused", "Deleted", "2026-01-05"),
                _campaign(70000004, "Pytest Live - Launch - Search", "Active", future.isoformat()),
                _campaign(70000005, "Pytest Live - Paused - Search", "BudgetAndManualPaused", "2026-01-05"),
            ]})
        if request.url.path.endswith("/GenerateReport/Submit"):
            report = body["ReportRequest"]
            assert report["Type"] == "CampaignPerformanceReportRequest" and report["Aggregation"] == "WeeklyStartingMonday"
            assert report["Scope"]["AccountIds"] == [LIVE_ACCOUNT["account_id"]]
            return httpx.Response(200, json={"ReportRequestId": "report-1"})
        if request.url.path.endswith("/GenerateReport/Poll"):
            # Pending once, then ready
            done = sum(1 for p in calls if p.endswith("/GenerateReport/Poll")) > 1
            return httpx.Response(200, json={"ReportRequestStatus": {
                "Status": "Success" if done else "Pending",
                "ReportDownloadUrl": "https://reports.example.test/report.zip" if done else None}})
        raise AssertionError(f"Unexpected call {request.url}")

    monkeypatch.setattr(microsoft_ads_client, "_http_client", _transport(handler))
    summary = _sync(client, admin)
    assert summary["mode"] == "live"
    # Only the live connection's account was read; the mock one is ignored in live mode
    assert [r["account_id"] for r in summary["account_runs"]] == [LIVE_ACCOUNT["account_id"]]
    assert summary["campaigns_created"] == 4 and summary["campaigns_skipped"] == 1

    rows = {r["external_id"]: r for r in run_sql(
        "SELECT l.external_id, i.id, i.status, i.start_date, i.end_date, i.budget_amount, i.channel_details, i.description "
        "FROM initiative_links l JOIN initiatives i ON i.id = l.initiative_id WHERE l.system = 'MICROSOFT_ADS' AND l.account_id = $1",
        LIVE_ACCOUNT["account_id"],
    )}
    assert {k: v["status"] for k, v in rows.items()} == {
        "70000001": "ACTIVE", "70000002": "CANCELLED", "70000004": "PLANNED", "70000005": "PAUSED",
    }
    brand_row = rows["70000001"]
    assert brand_row["start_date"] == date(2026, 1, 5) and brand_row["end_date"] is None
    # The budget so far: 50 a day from the start date to today
    assert brand_row["budget_amount"] == Decimal("50.00") * ((date.today() - date(2026, 1, 5)).days + 1)
    assert brand_row["description"] == f"Added by the Microsoft Ads sync from account {LIVE_ACCOUNT['account_id']}."
    details = rows["70000002"]["channel_details"]
    details = details if isinstance(details, dict) else json.loads(details)
    assert details["campaign_objective"] == "AWARENESS"
    results = _results([brand_row["id"], rows["70000002"]["id"]])
    brand = next(r for r in results if r["initiative_id"] == brand_row["id"])
    assert (brand["period_start"], brand["spend"], brand["impressions"], brand["clicks"], brand["conversions"]) == (
        week, Decimal("123.45"), 1000, 50, Decimal("2.50"))
    assert brand["status"] == "ACTIVE" and brand["source"] == "MICROSOFT_ADS_API"
    assert next(r for r in results if r["initiative_id"] == rows["70000002"]["id"])["status"] == "ENDED"


def test_live_discover_accounts_reads_the_user_and_their_accounts(monkeypatch):
    monkeypatch.setenv("MICROSOFT_ADS_DEVELOPER_TOKEN", "dev-token")
    monkeypatch.delenv("MICROSOFT_ADS_CUSTOMER_ID", raising=False)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path))
        if request.url.path.endswith("/User/Query"):
            return httpx.Response(200, json={"User": {"Id": 9001, "UserName": "pytest"}, "CustomerRoles": []})
        body = json.loads(request.content)
        assert body["Predicates"] == [{"Field": "UserId", "Operator": "Equals", "Value": "9001"}]
        return httpx.Response(200, json={"Accounts": [
            {"Id": 111, "Name": "Direct Account", "Number": "F111", "ParentCustomerId": 222, "CurrencyCode": "CAD",
             "TimeZone": "EasternTimeUSCanada", "AccountLifeCycleStatus": "Active"},
            {"Id": 333, "Name": "Closed Account", "Number": "F333", "ParentCustomerId": 222, "CurrencyCode": "USD",
             "AccountLifeCycleStatus": "Inactive"},
        ]})

    monkeypatch.setattr(microsoft_ads_client, "_http_client", _transport(handler))
    accounts = asyncio.run(microsoft_ads_client.discover_accounts("token"))
    assert accounts[0] == {"account_id": "111", "account_name": "Direct Account", "account_number": "F111",
                           "customer_id": "222", "currency": "CAD", "time_zone": "EasternTimeUSCanada", "is_manager": False}
    assert accounts[1]["error"] == "The account is inactive."
    assert seen == [
        ("clientcenter.api.bingads.microsoft.com", "/CustomerManagement/v13/User/Query"),
        ("clientcenter.api.bingads.microsoft.com", "/CustomerManagement/v13/Accounts/Search"),
    ]


def test_live_api_errors_are_plain_and_stored(client, admin, org, monkeypatch):
    live_id = _store_live_connection(org)
    monkeypatch.setenv("MICROSOFT_ADS_MODE", "live")
    monkeypatch.setenv("MICROSOFT_ADS_CLIENT_ID", "client-id")
    monkeypatch.setenv("MICROSOFT_ADS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("MICROSOFT_ADS_DEVELOPER_TOKEN", "dev-token")
    _only_connections(monkeypatch, [live_id])
    monkeypatch.setattr(microsoft_ads_client, "_http_client", _transport(
        lambda request: httpx.Response(403, json={"OperationErrors": [{"Code": 106, "Message": "The user is not authorized"}]})
    ))
    response = client.post("/api/integrations/microsoft-ads/sync", headers=admin["headers"])
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Couldn't read Pytest Live Microsoft Account: Microsoft Ads refused access: The user is not authorized.")
    status = client.get("/api/integrations/microsoft-ads/sync-status", headers=admin["headers"]).json()
    assert status["last_error"] == response.json()["detail"]

    # Without a developer token nothing is called, and the reason is plain
    monkeypatch.setenv("MICROSOFT_ADS_DEVELOPER_TOKEN", "")
    response = client.post("/api/integrations/microsoft-ads/sync", headers=admin["headers"])
    assert response.status_code == 503 and response.json()["detail"].endswith(microsoft_ads_client.NO_DEVELOPER_TOKEN_MESSAGE)


# --- Permissions ---

def test_sync_needs_permission(client, viewer):
    assert client.post("/api/integrations/microsoft-ads/sync", headers=viewer["headers"]).status_code == 403
    assert client.get("/api/integrations/microsoft-ads/sync-status", headers=viewer["headers"]).status_code == 403
    assert client.post("/api/integrations/microsoft-ads/sync").status_code == 401
