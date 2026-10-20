"""Google Ads: connecting (mock and live mode), discovering accounts, and the campaign and weekly results sync.

Google is never called: mock mode generates everything, and the live-path tests answer the Google Ads REST calls
with an httpx.MockTransport. Each test uses its own organization, company and ad accounts (ids 555-010-...), and only
syncs its own connections; everything it creates is removed afterwards, and the sync state is put back.
"""
import asyncio
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from services import google_ads_client, google_ads_sync_job
from services import token_crypto_service as crypto
from services.google_ads_sync_job import campaign_budget, initiative_status, objective_for, read_window, result_status
from services.oauth_adapters import google_ads as google_ads_adapter
from services.oauth_adapters.microsoft_ads import MicrosoftAdsOAuthProvider
from tests.conftest import run_sql

MANAGER = {"account_id": "555-010-0900", "account_name": "Pytest Manager (MCC)", "currency": "USD",
           "time_zone": "America/New_York", "is_manager": True}
CLIENT = {"account_id": "555-010-0003", "account_name": "DeskFlex Pytest Search Ads", "currency": "USD",
          "time_zone": "America/Chicago", "is_manager": False}
LIVE_ACCOUNT = {"account_id": "555-010-0777", "account_name": "Pytest Live Account", "currency": "USD",
                "time_zone": "America/Chicago", "is_manager": False}
TEST_ACCOUNTS = [MANAGER["account_id"], CLIENT["account_id"], LIVE_ACCOUNT["account_id"]]


def _test_initiative_ids() -> list:
    return [r["initiative_id"] for r in run_sql(
        "SELECT initiative_id FROM initiative_links WHERE system = 'GOOGLE_ADS' AND account_id = ANY($1::text[])",
        TEST_ACCOUNTS,
    )]


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch):
    """Mock mode with this test's accounts, whatever the local .env says."""
    monkeypatch.setenv("GOOGLE_ADS_MODE", "mock")
    monkeypatch.setenv("GOOGLE_ADS_CREATE_CAMPAIGNS", "true")
    monkeypatch.setenv("GOOGLE_ADS_LOOKBACK_DAYS", "90")
    monkeypatch.setattr(google_ads_adapter, "MOCK_ACCOUNTS", [MANAGER, CLIENT])


@pytest.fixture
def sync_state(client):
    """The sync state is put back afterwards, and the campaigns made for the test accounts removed."""
    run_sql("DROP TABLE IF EXISTS pytest_google_ads_state")
    run_sql("CREATE TABLE pytest_google_ads_state AS SELECT * FROM integration_sync_state WHERE name IN ('google_ads', 'google_ads_mock')")
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
    run_sql("DELETE FROM integration_sync_state WHERE name IN ('google_ads', 'google_ads_mock')")
    run_sql("INSERT INTO integration_sync_state SELECT * FROM pytest_google_ads_state")
    run_sql("DROP TABLE pytest_google_ads_state")


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
    original = google_ads_sync_job.usable_connections

    async def only_these(conn, mode, _ids=None):
        return await original(conn, mode, [uuid.UUID(str(i)) for i in connection_ids])

    monkeypatch.setattr(google_ads_sync_job, "usable_connections", only_these)


def _connect_mock(client, admin, org) -> dict:
    response = client.post(
        "/api/oauth/connect-instant/GOOGLE_ADS", headers=admin["headers"],
        json={"organization_id": str(org["id"]), "company_id": str(org["company_id"])},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sync(client, admin) -> dict:
    response = client.post("/api/integrations/google-ads/sync", headers=admin["headers"])
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

def test_status_mapping():
    today = date(2026, 10, 20)
    assert initiative_status("ENABLED", date(2026, 1, 1), None, today) == "ACTIVE"
    assert initiative_status("ENABLED", date(2026, 11, 1), None, today) == "PLANNED"
    assert initiative_status("ENABLED", date(2026, 1, 1), date(2026, 3, 1), today) == "COMPLETED"
    assert initiative_status("PAUSED", date(2026, 1, 1), None, today) == "PAUSED"
    assert initiative_status("REMOVED", date(2026, 1, 1), None, today) == "CANCELLED"
    assert [result_status(s) for s in ("ACTIVE", "PLANNED", "PAUSED", "COMPLETED", "CANCELLED")] == [
        "ACTIVE", "ACTIVE", "PAUSED", "ENDED", "ENDED"]
    assert objective_for("SEARCH") == "LEADS" and objective_for("VIDEO") == "AWARENESS"


def test_campaign_budget_is_the_budget_so_far_without_an_end_date():
    today = date(2026, 10, 20)
    daily = Decimal("100.00")
    base = {"daily_budget": daily, "total_budget": None, "start_date": date(2026, 10, 1), "weeks": {}}
    # A total budget wins; with an end date it's the whole run
    assert campaign_budget({**base, "total_budget": Decimal("5000"), "status": "ENABLED", "end_date": None}, today) == Decimal("5000")
    assert campaign_budget({**base, "status": "ENABLED", "end_date": date(2026, 10, 10)}, today) == Decimal("1000.00")
    # Running without an end date: up to today (20 days)
    assert campaign_budget({**base, "status": "ENABLED", "end_date": None}, today) == Decimal("2000.00")
    # Not started yet: nothing so far
    assert campaign_budget({**base, "status": "ENABLED", "end_date": None, "start_date": date(2026, 11, 1)}, today) == Decimal("0.00")
    # Paused: up to the end of its last week with spend (Oct 5 to 11), not today
    weeks = {date(2026, 9, 28): {"spend": Decimal("80")}, date(2026, 10, 5): {"spend": Decimal("50")}, date(2026, 10, 12): {"spend": 0}}
    assert campaign_budget({**base, "status": "PAUSED", "end_date": None, "weeks": weeks}, today) == Decimal("1100.00")
    # Paused with no spend in the window: unknown (the portal keeps the budget it has)
    assert campaign_budget({**base, "status": "PAUSED", "end_date": None}, today) is None
    assert campaign_budget({**base, "daily_budget": None, "status": "ENABLED", "end_date": None}, today) is None


def test_read_window_starts_on_a_monday(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_LOOKBACK_DAYS", "90")
    start, end = read_window(date(2026, 10, 20))
    assert start.weekday() == 0 and end == date(2026, 10, 20)
    assert date(2026, 10, 20) - start >= timedelta(days=89)


def test_mode_defaults(monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    assert google_ads_client.google_ads_mode() == "mock"
    monkeypatch.setenv("APP_ENV", "production")
    assert google_ads_client.google_ads_mode() == "live"
    monkeypatch.setenv("GOOGLE_ADS_MODE", "mock")
    assert google_ads_client.google_ads_mode() == "mock"


def test_microsoft_ads_never_uses_the_sign_in_app(monkeypatch):
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "portal-sso-app")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "portal-sso-secret")
    monkeypatch.delenv("MICROSOFT_ADS_CLIENT_ID", raising=False)
    monkeypatch.delenv("MICROSOFT_ADS_CLIENT_SECRET", raising=False)
    adapter = MicrosoftAdsOAuthProvider()
    assert adapter.client_id == "" and adapter.client_secret == "" and not adapter.is_configured
    monkeypatch.setenv("MICROSOFT_ADS_CLIENT_ID", "ads-app")
    monkeypatch.setenv("MICROSOFT_ADS_CLIENT_SECRET", "ads-secret")
    assert MicrosoftAdsOAuthProvider().client_id == "ads-app"


# --- Mock mode end to end ---

def test_mock_connect_then_sync_creates_campaigns_results_and_costs(client, admin, org, monkeypatch):
    connected = _connect_mock(client, admin, org)
    connection = connected["connection"]
    assert connection["connection_metadata"]["mode"] == "mock"
    assert [a["account_id"] for a in connected["discovered_accounts"]] == [MANAGER["account_id"], CLIENT["account_id"]]
    _only_connections(monkeypatch, [connection["id"]])

    summary = _sync(client, admin)
    assert summary["mode"] == "mock"
    # The manager account is never synced
    assert [r["account_id"] for r in summary["account_runs"]] == [CLIENT["account_id"]]
    expected = google_ads_sync_job.mock_account_campaigns(CLIENT, *read_window(date.today()))
    assert 5 <= len(expected) <= 8
    assert summary["campaigns_created"] == len(expected)
    weeks = sum(len(c["weeks"]) for c in expected)
    assert summary["results_created"] == weeks > 0

    campaigns = {r["external_id"]: dict(r) for r in run_sql(
        """
        SELECT l.external_id, l.account_id, i.id, i.name, i.status, i.currency, i.data_source, i.channel_details,
               b.code AS brand, ch.code AS channel, sc.code AS sub_channel, i.created_by
        FROM initiative_links l JOIN initiatives i ON i.id = l.initiative_id
        JOIN brands b ON b.id = i.brand_id JOIN channels ch ON ch.id = i.channel_id
        JOIN sub_channels sc ON sc.id = i.sub_channel_id
        WHERE l.system = 'GOOGLE_ADS' AND l.account_id = $1
        """,
        CLIENT["account_id"],
    )}
    assert set(campaigns) == {c["campaign_id"] for c in expected}
    for c in expected:
        row = campaigns[c["campaign_id"]]
        assert row["name"] == c["name"] and row["brand"] == "DESKFLEX" and row["created_by"] is None
        assert (row["channel"], row["sub_channel"], row["data_source"], row["currency"]) == ("PAID_SEARCH", "GOOGLE_ADS", "LINKED", "USD")
        details = row["channel_details"] if isinstance(row["channel_details"], dict) else json.loads(row["channel_details"])
        assert details["ad_account_id"] == CLIENT["account_id"]
        assert row["status"] == initiative_status(c["status"], c["start_date"], c["end_date"], date.today())
    # This account has paused campaigns and one that has ended
    assert {"PAUSED", "COMPLETED", "ACTIVE"} <= {r["status"] for r in campaigns.values()}

    ids = [r["id"] for r in campaigns.values()]
    results = _results(ids)
    assert len(results) == weeks
    assert all(r["source"] == "GOOGLE_ADS_API" and r["ad_group"] is None for r in results)
    assert all(r["period_start"].weekday() == 0 and r["period_end"] == r["period_start"] + timedelta(days=6) for r in results)
    # Spend mirrored into one Google Ads cost line per result with spend
    lines = run_sql(
        "SELECT external_ref, amount, source FROM spend_lines WHERE initiative_id = ANY($1::uuid[])", ids
    )
    assert len(lines) == sum(1 for r in results if r["spend"] and r["spend"] > 0)
    assert {line["source"] for line in lines} == {"GOOGLE_ADS"}
    by_ref = {line["external_ref"]: line["amount"] for line in lines}
    assert all(by_ref[f"ad_result:{r['id']}"] == r["spend"] for r in results if r["spend"])

    # Status and data health
    status = client.get("/api/integrations/google-ads/sync-status", headers=admin["headers"]).json()
    assert status["mode"] == "mock" and status["configured"] is True and status["last_success_at"]
    assert status["last_run_summary"]["campaigns_created"] == len(expected)
    health = client.get("/api/ads/data-health", headers=admin["headers"]).json()
    google = next(p for p in health["platforms"] if p["platform"] == "GOOGLE_ADS")
    assert google["connection_status"] == "CONNECTED" and google["source"] == "API"
    assert google["last_sync_at"] and google["sync_mode"] == "mock"


def test_rerun_updates_in_place_and_keeps_hubspot_figures(client, admin, org, monkeypatch):
    connection = _connect_mock(client, admin, org)["connection"]
    _only_connections(monkeypatch, [connection["id"]])
    first = _sync(client, admin)
    ids = _test_initiative_ids()
    before = _results(ids)
    lines_before = run_sql("SELECT count(*) AS n FROM spend_lines WHERE initiative_id = ANY($1::uuid[])", ids)[0]["n"]

    # Leads from HubSpot on one week, and a figure changed by hand on another
    hubspot, edited = before[0], before[1]
    run_sql("UPDATE ad_results SET hubspot_contacts = 7, mqls = 3, sqls = 1 WHERE id = $1", hubspot["id"])
    run_sql("UPDATE ad_results SET clicks = clicks + 1000 WHERE id = $1", edited["id"])
    # A week Google no longer reports, inside the window, and an older one outside it
    window_start = read_window(date.today())[0]
    gone = run_sql(
        "INSERT INTO ad_results (initiative_id, period_start, period_end, spend, source) "
        "VALUES ($1, $2, $3, 5, 'GOOGLE_ADS_API') RETURNING id",
        ids[0], window_start - timedelta(days=7 * 30), window_start - timedelta(days=7 * 30) + timedelta(days=6),
    )[0]["id"]
    run_sql(  # a typed-in week Google doesn't report stays too
        "INSERT INTO ad_results (initiative_id, ad_group, period_start, period_end, spend, source) "
        "VALUES ($1, 'Typed in', $2, $3, 9, 'MANUAL')",
        ids[0], window_start, window_start + timedelta(days=6),
    )
    stale = run_sql(
        "INSERT INTO ad_results (initiative_id, period_start, period_end, spend, source) "
        "VALUES ($1, $2, $3, 11, 'GOOGLE_ADS_API') "
        "ON CONFLICT DO NOTHING RETURNING id",
        ids[0], date.today() + timedelta(days=7 - date.today().weekday()), date.today() + timedelta(days=13 - date.today().weekday()),
    )

    second = _sync(client, admin)
    assert second["campaigns_created"] == 0
    assert second["campaigns_unchanged"] == first["campaigns_created"]
    assert second["results_created"] == 0
    assert second["results_updated"] == 1  # the week changed by hand goes back to Google's figures
    assert second["results_removed"] == len(stale)
    assert len(_test_initiative_ids()) == len(ids)

    after = {r["id"]: r for r in _results(ids)}
    assert (after[hubspot["id"]]["hubspot_contacts"], after[hubspot["id"]]["mqls"], after[hubspot["id"]]["sqls"]) == (7, 3, 1)
    assert after[hubspot["id"]]["spend"] == hubspot["spend"]
    assert after[edited["id"]]["clicks"] == edited["clicks"]
    assert gone in after  # outside the window: kept
    assert any(r["ad_group"] == "Typed in" for r in after.values())
    assert len([r for r in after.values() if r["source"] == "GOOGLE_ADS_API" and r["id"] != gone]) == len(before)
    lines_after = run_sql("SELECT count(*) AS n FROM spend_lines WHERE initiative_id = ANY($1::uuid[])", ids)[0]["n"]
    # The typed-in and older weeks have spend lines of their own now; nothing else was added
    assert lines_after == lines_before + 2


def test_rerun_follows_google_status_and_keeps_a_given_name(client, admin, org, monkeypatch):
    connection = _connect_mock(client, admin, org)["connection"]
    _only_connections(monkeypatch, [connection["id"]])
    _sync(client, admin)
    ids = _test_initiative_ids()
    sync_made, named = ids[0], ids[1]
    run_sql("UPDATE initiatives SET status = 'DRAFT', name = 'Renamed by sync test' WHERE id = $1", sync_made)
    # Someone's own campaign linked to a Google campaign keeps its name
    run_sql("UPDATE initiatives SET name = 'Our own name', created_by = $2 WHERE id = $1", named, admin["id"])

    summary = _sync(client, admin)
    assert summary["campaigns_updated"] == 1
    rows = {r["id"]: r for r in run_sql("SELECT id, name, status FROM initiatives WHERE id = ANY($1::uuid[])", [sync_made, named])}
    assert rows[sync_made]["status"] != "DRAFT" and rows[sync_made]["name"] != "Renamed by sync test"
    assert rows[named]["name"] == "Our own name"
    run_sql("UPDATE initiatives SET created_by = NULL WHERE id = $1", named)  # so the fixture removes it


# --- Live mode ---

def _no_credentials(monkeypatch):
    for name in ("GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)


def test_live_mode_without_credentials_refuses_to_connect(client, admin, org, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_MODE", "live")
    _no_credentials(monkeypatch)
    response = client.post(
        "/api/oauth/connect-instant/GOOGLE_ADS", headers=admin["headers"], json={"organization_id": str(org["id"])}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Google Ads isn't set up: add GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET"
    response = client.get(
        f"/api/oauth/authorize/GOOGLE_ADS?organization_id={org['id']}", headers=admin["headers"]
    )
    assert response.status_code == 400 and "isn't set up" in response.json()["detail"]
    assert run_sql("SELECT count(*) AS n FROM oauth_connections WHERE organization_id = $1", org["id"])[0]["n"] == 0
    status = client.get("/api/integrations/google-ads/sync-status", headers=admin["headers"]).json()
    assert status["mode"] == "live" and status["configured"] is False


def test_mock_sign_in_returns_to_the_app(client, admin, org):
    response = client.get(
        f"/api/oauth/authorize/GOOGLE_ADS?organization_id={org['id']}&redirect_uri=http://localhost:5180/oauth/callback",
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorization_url"].startswith("http://localhost:5180/oauth/callback?code=mock_google_ads_")
    code = httpx.URL(body["authorization_url"]).params["code"]
    response = client.post("/api/oauth/callback", headers=admin["headers"], json={
        "provider": "GOOGLE_ADS", "code": code, "state": body["state"],
        "redirect_uri": "http://localhost:5180/oauth/callback",
    })
    assert response.status_code == 200, response.text
    assert response.json()["connection"]["connection_metadata"]["mode"] == "mock"


def _store_live_connection(org) -> uuid.UUID:
    meta = {"mode": "live", "discovered_accounts": [LIVE_ACCOUNT]}
    return run_sql(
        """
        INSERT INTO oauth_connections (organization_id, provider, provider_account_id, account_name,
                                       access_token_encrypted, expires_at, status, connection_metadata)
        VALUES ($1, 'GOOGLE_ADS', $2, $3, $4, now() + interval '1 hour', 'CONNECTED', CAST($5 AS jsonb))
        RETURNING id
        """,
        org["id"], LIVE_ACCOUNT["account_id"], LIVE_ACCOUNT["account_name"], crypto.encrypt_token("live-token"),
        json.dumps(meta),
    )[0]["id"]


def _google_ads_transport(handler):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory


def _campaign(cid, name, status, start, end="2037-12-30 23:59:59", channel="SEARCH", daily_micros=50_000_000):
    return {"campaign": {"resourceName": f"customers/5550100777/campaigns/{cid}", "id": cid, "name": name,
                         "status": status, "advertisingChannelType": channel, "startDateTime": f"{start} 00:00:00",
                         "endDateTime": end},
            "campaignBudget": {"amountMicros": str(daily_micros), "period": "DAILY"}}


def test_live_sync_maps_gaql_rows_and_ignores_mock_connections(client, admin, org, monkeypatch):
    mock_connection = _connect_mock(client, admin, org)["connection"]
    live_id = _store_live_connection(org)
    monkeypatch.setenv("GOOGLE_ADS_MODE", "live")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev-token")
    _only_connections(monkeypatch, [mock_connection["id"], live_id])

    week = read_window(date.today())[0] + timedelta(days=7)
    future = (date.today() + timedelta(days=30)).isoformat()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer live-token"
        assert request.headers["developer-token"] == "dev-token"
        assert request.url.path == f"/{google_ads_client.API_VERSION}/customers/5550100777/googleAds:searchStream"
        query = json.loads(request.content)["query"]
        if "segments.week" in query:
            return httpx.Response(200, json=[{"results": [
                {"campaign": {"id": "90000000001"}, "segments": {"week": week.isoformat()},
                 "metrics": {"costMicros": "123450000", "impressions": "1000", "clicks": "50", "conversions": 2.5}},
                {"campaign": {"id": "90000000002"}, "segments": {"week": week.isoformat()},
                 "metrics": {"costMicros": "10000000", "impressions": "300", "clicks": "4", "conversions": 0}},
            ]}])
        return httpx.Response(200, json=[{"results": [
            _campaign("90000000001", "Pytest Live - Brand - Search", "ENABLED", "2026-01-05"),
            _campaign("90000000002", "Pytest Live - Old - Display", "REMOVED", "2026-01-05", channel="DISPLAY"),
            _campaign("90000000003", "Pytest Live - Removed Unused", "REMOVED", "2026-01-05"),
            _campaign("90000000004", "Pytest Live - Launch - Search", "ENABLED", future),
            _campaign("90000000005", "Pytest Live - Paused - Search", "PAUSED", "2026-01-05"),
        ]}])

    monkeypatch.setattr(google_ads_client, "_http_client", _google_ads_transport(handler))
    summary = _sync(client, admin)
    assert summary["mode"] == "live"
    # Only the live connection's account was read; the mock one is ignored in live mode
    assert [r["account_id"] for r in summary["account_runs"]] == [LIVE_ACCOUNT["account_id"]]
    assert len(calls) == 2
    # The removed campaign with nothing in the window isn't added
    assert summary["campaigns_created"] == 4 and summary["campaigns_skipped"] == 1

    rows = {r["external_id"]: r for r in run_sql(
        "SELECT l.external_id, i.id, i.status, i.start_date, i.end_date, i.channel_details FROM initiative_links l "
        "JOIN initiatives i ON i.id = l.initiative_id WHERE l.system = 'GOOGLE_ADS' AND l.account_id = $1",
        LIVE_ACCOUNT["account_id"],
    )}
    assert {k: v["status"] for k, v in rows.items()} == {
        "90000000001": "ACTIVE", "90000000002": "CANCELLED", "90000000004": "PLANNED", "90000000005": "PAUSED",
    }
    assert rows["90000000001"]["start_date"] == date(2026, 1, 5) and rows["90000000001"]["end_date"] is None
    details = rows["90000000002"]["channel_details"]
    details = details if isinstance(details, dict) else json.loads(details)
    assert details["campaign_objective"] == "AWARENESS"
    results = _results([rows["90000000001"]["id"], rows["90000000002"]["id"]])
    brand = next(r for r in results if r["initiative_id"] == rows["90000000001"]["id"])
    assert (brand["period_start"], brand["spend"], brand["impressions"], brand["clicks"], brand["conversions"]) == (
        week, Decimal("123.45"), 1000, 50, Decimal("2.50"))
    assert brand["status"] == "ACTIVE"
    assert next(r for r in results if r["initiative_id"] == rows["90000000002"]["id"])["status"] == "ENDED"


def test_live_discover_accounts_parses_customers_and_manager_clients(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
    monkeypatch.delenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", raising=False)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("login-customer-id"), "developer-token" in request.headers))
        if request.url.path.endswith("customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1112223333", "customers/4445556666"]})
        query = json.loads(request.content)["query"]
        if "customer_client" in query:
            return httpx.Response(200, json=[{"results": [
                {"customerClient": {"id": "7778889999", "descriptiveName": "Child Account", "currencyCode": "CAD",
                                    "timeZone": "America/Toronto", "manager": False, "status": "ENABLED"}},
                {"customerClient": {"id": "4445556666", "descriptiveName": "Already listed", "manager": False}},
            ]}])
        if "/customers/1112223333/" in request.url.path:
            return httpx.Response(200, json=[{"results": [{"customer": {
                "id": "1112223333", "descriptiveName": "Group MCC", "currencyCode": "USD",
                "timeZone": "America/New_York", "manager": True}}]}])
        return httpx.Response(200, json=[{"results": [{"customer": {
            "id": "4445556666", "descriptiveName": "Direct Account", "currencyCode": "USD",
            "timeZone": "America/Chicago", "manager": False}}]}])

    monkeypatch.setattr(google_ads_client, "_http_client", _google_ads_transport(handler))
    accounts = asyncio.run(google_ads_client.discover_accounts("token"))
    assert accounts == [
        {"account_id": "111-222-3333", "account_name": "Group MCC", "currency": "USD", "time_zone": "America/New_York", "is_manager": True},
        {"account_id": "444-555-6666", "account_name": "Direct Account", "currency": "USD", "time_zone": "America/Chicago", "is_manager": False},
        {"account_id": "777-888-9999", "account_name": "Child Account", "currency": "CAD", "time_zone": "America/Toronto",
         "is_manager": False, "login_customer_id": "111-222-3333"},
    ]
    # The manager's clients are read through the manager; no developer token is sent when none is set
    assert ("/" + google_ads_client.API_VERSION + "/customers/1112223333/googleAds:searchStream", "1112223333", False) in seen
    assert not any(dev for _, _, dev in seen)


def test_live_api_errors_are_plain_and_stored(client, admin, org, monkeypatch):
    live_id = _store_live_connection(org)
    monkeypatch.setenv("GOOGLE_ADS_MODE", "live")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "client-secret")
    _only_connections(monkeypatch, [live_id])
    monkeypatch.setattr(google_ads_client, "_http_client", _google_ads_transport(
        lambda request: httpx.Response(403, json={"error": {"message": "The caller does not have permission"}})
    ))
    response = client.post("/api/integrations/google-ads/sync", headers=admin["headers"])
    assert response.status_code == 503
    assert response.json()["detail"].startswith("Couldn't read Pytest Live Account: Google Ads refused access")
    status = client.get("/api/integrations/google-ads/sync-status", headers=admin["headers"]).json()
    assert status["last_error"] == response.json()["detail"]


# --- Permissions ---

def test_sync_needs_permission(client, viewer):
    assert client.post("/api/integrations/google-ads/sync", headers=viewer["headers"]).status_code == 403
    assert client.get("/api/integrations/google-ads/sync-status", headers=viewer["headers"]).status_code == 403
    assert client.post("/api/integrations/google-ads/sync").status_code == 401
