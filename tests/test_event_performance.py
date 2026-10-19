"""Events Overview: GET /api/events/performance (cost, cost per outcome, ROI and verdict per event, totals per currency).

Each test uses its own period years in the past (or far ahead), so other events in the local database don't show up.
Everything is created by the temporary admin and removed with it.
"""
import uuid
from datetime import date, timedelta

import pytest

from services.event_performance_service import event_result
from tests.conftest import run_sql


@pytest.fixture(scope="module")
def codes():
    return {
        "categories": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM spend_categories")},
        "demo_types": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM demo_types")},
        "metrics": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM outcome_metrics")},
        "markets": {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM markets")},
    }


def _event(client, admin, ref, start, end=None, **overrides):
    body = {
        "name": f"Pytest Perf Expo {uuid.uuid4().hex[:6]}",
        "brand_id": ref["brands"]["DESKFLEX"],
        "channel_id": ref["channels"]["EVENTS"],
        "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
        "start_date": str(start),
        "end_date": str(end or start),
        "budget_amount": 25000,
        "currency": "USD",
        "event": {"city": "Las Vegas", "country_code": "US"},
        **overrides,
    }
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def _cost(initiative_id, category_id, amount, currency="USD"):
    run_sql(
        "INSERT INTO spend_lines (initiative_id, category_id, amount, currency, incurred_on) VALUES ($1, $2, $3, $4, '2011-01-01')",
        uuid.UUID(initiative_id), category_id, amount, currency,
    )


def _results(client, admin, initiative_id, period, values):
    response = client.put(
        f"/api/initiatives/{initiative_id}/outcomes/{period}",
        json={"values": [{"metric_code": k, "value": v} for k, v in values.items()]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text


def _get(client, user, **params):
    response = client.get("/api/events/performance", params=params, headers=user["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(body):
    return {e["id"]: e for e in body["events"]}


def test_ceo_example(client, admin, ref, codes):
    """$20,000 trade show: 200 leads, 20 demos, 2 customers, $20,000 revenue → $100 / $1,000 / $10,000, ROI 0, not successful."""
    event = _event(client, admin, ref, date(2011, 3, 4), date(2011, 3, 6))
    _cost(event["id"], codes["categories"]["BOOTH_SPACE"], 20000)
    _results(client, admin, event["id"], "2011-03-01", {"LEADS": 200, "DEMOS": 20, "CUSTOMERS": 2, "REVENUE": 20000, "VISITORS": 400})

    body = _get(client, admin, **{"from": "2011-01-01", "to": "2011-12-31"})
    e = _by_id(body)[event["id"]]

    assert e["cost"] == 20000
    assert (e["cost_per_lead"], e["cost_per_demo"], e["cost_per_customer"], e["cost_per_visitor"]) == (100, 1000, 10000, 50)
    assert e["cost_per_follow_up"] is None and e["follow_ups"] is None
    assert e["roi_percent"] == 0.0
    assert e["money_outcome"] == "BROKE_EVEN"
    assert e["timing"] == "PAST"
    assert e["matures_on"] == "2011-09-02"  # In-person events wait 180 days after the last day
    assert e["result"] == "NOT_SUCCESSFUL"
    assert e["event_code"] and e["brand_name"] and e["sub_channel_code"] == "IN_PERSON_EVENTS"
    assert e["market_name"] == run_sql("SELECT name FROM markets WHERE code = 'UNASSIGNED'")[0]["name"]

    [totals] = body["totals"]
    assert totals["currency"] == "USD" and totals["event_count"] == 1
    assert (totals["cost_per_lead"], totals["cost_per_demo"], totals["cost_per_customer"]) == (100, 1000, 10000)
    assert (totals["successful_count"], totals["unsuccessful_count"], totals["awaiting_count"]) == (0, 1, 0)


def test_cost_breakdown_groups_expense_types_and_skips_other_currencies(client, admin, ref, codes):
    event = _event(client, admin, ref, date(2012, 5, 1))
    c = codes["categories"]
    _cost(event["id"], c["FLIGHTS"], 1000)          # Travel
    _cost(event["id"], c["HOTELS"], 500)            # Travel
    _cost(event["id"], c["BOOTH_SPACE"], 3000)      # Booth (space & sponsorship)
    _cost(event["id"], c["ELECTRICAL"], 200)        # Booth (show services)
    _cost(event["id"], c["FREIGHT"], 300)           # Booth (shipping)
    _cost(event["id"], c["PRINT_SIGNAGE"], 400)     # Materials
    _cost(event["id"], c["BOOTH_CATERING"], 100)    # Materials (registration & hospitality)
    _cost(event["id"], c["STAFF_TIME"], 700)        # Staff & other
    _cost(event["id"], c["BOOTH_SPACE"], 9999, "EUR")  # another currency: left out, counted

    e = _by_id(_get(client, admin, **{"from": "2012-01-01", "to": "2012-12-31"}))[event["id"]]

    assert e["cost_breakdown"] == {"TRAVEL": 1500, "BOOTH": 3500, "MATERIALS": 500, "STAFF_OTHER": 700}
    assert e["cost"] == 6200
    assert e["other_currency_lines"] == 1
    # No results yet, matured long ago
    assert e["money_outcome"] == "NO_REVENUE"
    assert e["result"] == "AWAITING_REVENUE"
    assert e["leads"] is None and e["cost_per_lead"] is None


def test_synced_results_beat_typed_and_demo_log_replaces_typed_demos(client, admin, ref, codes):
    event = _event(client, admin, ref, date(2013, 6, 10))
    _cost(event["id"], codes["categories"]["BOOTH_SPACE"], 3000)
    _results(client, admin, event["id"], "2013-06-01", {"LEADS": 100, "DEMOS": 50, "REVENUE": 1000})
    run_sql(
        "INSERT INTO outcomes (initiative_id, period_start, metric_id, value, currency, source) VALUES ($1, '2013-06-01', $2, 6000, 'USD', 'HUBSPOT')",
        uuid.UUID(event["id"]), codes["metrics"]["REVENUE"],
    )
    for _ in range(3):
        response = client.post(
            f"/api/initiatives/{event['id']}/demos",
            json={"demo_type_id": codes["demo_types"]["BOOTH"], "demo_at": "2013-06-10T10:00:00Z"},
            headers=admin["headers"],
        )
        assert response.status_code == 201, response.text

    e = _by_id(_get(client, admin, **{"from": "2013-01-01", "to": "2013-12-31"}))[event["id"]]

    assert e["demos"] == 3
    assert e["cost_per_demo"] == 1000
    assert e["revenue"] == 6000
    assert e["roi_percent"] == 100.0
    assert e["money_outcome"] == "PROFIT"
    assert e["result"] == "SUCCESSFUL"


def test_period_overlap_drafts_and_undated_events(client, admin, ref):
    spans = _event(client, admin, ref, date(2014, 12, 30), date(2015, 1, 2))
    inside = _event(client, admin, ref, date(2015, 3, 1))
    after = _event(client, admin, ref, date(2015, 4, 1))
    draft = _event(client, admin, ref, date(2015, 3, 2), status="DRAFT")

    ids = set(_by_id(_get(client, admin, **{"from": "2015-01-01", "to": "2015-03-31"})))

    assert spans["id"] in ids and inside["id"] in ids
    assert after["id"] not in ids
    assert draft["id"] not in ids
    # Without a period every dated event counts; undated ones never do
    undated = _event(client, admin, ref, date(2015, 3, 3), status="DRAFT")
    run_sql("UPDATE initiatives SET start_date = NULL, end_date = NULL, status = 'PLANNED' WHERE id = $1", uuid.UUID(undated["id"]))
    all_ids = set(_by_id(_get(client, admin)))
    assert {spans["id"], inside["id"], after["id"]} <= all_ids
    assert undated["id"] not in all_ids


def test_filters_and_totals_per_currency(client, admin, ref, codes):
    period = {"from": "2016-01-01", "to": "2016-12-31"}
    london = codes["markets"]["LONDON"]
    usd = _event(client, admin, ref, date(2016, 2, 1), market_id=london)
    gbp = _event(client, admin, ref, date(2016, 3, 1), currency="GBP", market_id=london)
    virtual = _event(
        client, admin, ref, date(2016, 4, 1), sub_channel_id=ref["sub_channels"]["VIRTUAL_EVENTS"],
        event={"platform": "Zoom"},
    )
    _cost(usd["id"], codes["categories"]["BOOTH_SPACE"], 1000)
    _cost(gbp["id"], codes["categories"]["BOOTH_SPACE"], 800, "GBP")
    _cost(virtual["id"], codes["categories"]["PRINT_SIGNAGE"], 1000)
    _results(client, admin, usd["id"], "2016-02-01", {"LEADS": 10, "REVENUE": 500})
    _results(client, admin, virtual["id"], "2016-04-01", {"LEADS": 30, "REVENUE": 3500})

    body = _get(client, admin, **period)
    totals = {t["currency"]: t for t in body["totals"]}
    assert totals["USD"]["event_count"] == 2
    # Recomputed from the sums: $2,000 for 40 leads, $4,000 revenue
    assert (totals["USD"]["cost"], totals["USD"]["leads"], totals["USD"]["cost_per_lead"]) == (2000, 40, 50)
    assert totals["USD"]["revenue"] == 4000 and totals["USD"]["roi_percent"] == 100.0
    assert (totals["USD"]["successful_count"], totals["USD"]["unsuccessful_count"]) == (1, 1)
    assert totals["GBP"]["cost"] == 800 and totals["GBP"]["leads"] is None
    assert totals["GBP"]["awaiting_count"] == 1
    assert [t["currency"] for t in body["totals"]] == ["USD", "GBP"]

    assert set(_by_id(_get(client, admin, **period, market_id=london))) == {usd["id"], gbp["id"]}
    assert set(_by_id(_get(client, admin, **period, sub_channel_id=ref["sub_channels"]["VIRTUAL_EVENTS"]))) == {virtual["id"]}
    other_brand = _get(client, admin, **period, brand_id=ref["brands"]["DESKFLEX"] + 100000)
    assert other_brand == {"events": [], "totals": []}


def test_timing_order_and_too_early(client, admin, ref, codes):
    today = date.today()
    past = _event(client, admin, ref, today - timedelta(days=20), today - timedelta(days=10))
    now = _event(client, admin, ref, today - timedelta(days=1), today + timedelta(days=1))
    soon = _event(client, admin, ref, today + timedelta(days=5))
    later = _event(client, admin, ref, today + timedelta(days=50))
    _cost(past["id"], codes["categories"]["BOOTH_SPACE"], 1000)
    # Lost money so far, but inside the 180-day window: too early to call
    _results(client, admin, past["id"], today.replace(day=1).isoformat(), {"REVENUE": 100})

    period = {"from": str(today - timedelta(days=30)), "to": str(today + timedelta(days=60))}
    events = [e for e in _get(client, admin, **period)["events"] if e["id"] in {past["id"], now["id"], soon["id"], later["id"]}]

    assert [e["id"] for e in events] == [now["id"], soon["id"], later["id"], past["id"]]
    assert [e["timing"] for e in events] == ["NOW", "UPCOMING", "UPCOMING", "PAST"]
    assert [e["result"] for e in events] == ["TOO_EARLY", "UPCOMING", "UPCOMING", "TOO_EARLY"]
    assert events[3]["money_outcome"] == "LOST_MONEY"

    only_upcoming = _get(client, admin, **period, timing="UPCOMING")["events"]
    assert {e["timing"] for e in only_upcoming} == {"UPCOMING"}
    assert {soon["id"], later["id"]} <= {e["id"] for e in only_upcoming}


def test_maturity_window_from_the_events_own_evaluation(client, admin, ref, codes):
    event = _event(client, admin, ref, date(2017, 5, 1))
    response = client.put(f"/api/initiatives/{event['id']}/evaluation", json={"target_value": None, "maturity_days": 30}, headers=admin["headers"])
    assert response.status_code == 200, response.text

    e = _by_id(_get(client, admin, **{"from": "2017-01-01", "to": "2017-12-31"}))[event["id"]]
    assert e["matures_on"] == "2017-05-31"


@pytest.mark.parametrize(
    "timing, roi, revenue, matured, expected",
    [
        ("UPCOMING", 50.0, 3000, True, "UPCOMING"),
        ("NOW", 10.0, 1100, False, "SUCCESSFUL"),
        ("PAST", 0.0, 1000, True, "NOT_SUCCESSFUL"),
        ("PAST", -40.0, 600, True, "NOT_SUCCESSFUL"),
        ("PAST", -40.0, 600, False, "TOO_EARLY"),
        ("PAST", None, None, False, "TOO_EARLY"),
        ("PAST", None, None, True, "AWAITING_REVENUE"),
    ],
)
def test_result_rules(timing, roi, revenue, matured, expected):
    today = date(2026, 1, 1)
    matures = today - timedelta(days=1) if matured else today + timedelta(days=1)
    assert event_result(timing, roi, revenue, matures, today) == expected


def test_needs_initiatives_view(client, viewer):
    assert client.get("/api/events/performance", headers=viewer["headers"]).status_code == 403
    assert client.get("/api/events/performance").status_code == 401


def test_initiatives_viewer_can_read(client, viewer):
    """A regular user given only the "View Initiatives" permission group (permissions are read once per session)."""
    code = f"PYTEST_EVENT_PERF_VIEWER_{uuid.uuid4().hex[:8].upper()}"
    role_id = run_sql("INSERT INTO roles (code, name, is_active) VALUES ($1, 'Pytest Viewer', true) RETURNING id", code)[0]["id"]
    run_sql(
        "INSERT INTO role_permission_groups (role_id, permission_group_id) "
        "SELECT $1, id FROM permission_groups WHERE code = 'MARKETING_VIEW_INITIATIVES'",
        role_id,
    )
    run_sql("INSERT INTO user_roles (user_id, role_id, is_active) VALUES ($1, $2, true)", viewer["id"], role_id)
    response = client.get("/api/events/performance", params={"from": "2010-01-01", "to": "2010-01-31"}, headers=viewer["headers"])
    assert response.status_code == 200, response.text


def test_rejects_a_backwards_period_and_unknown_timing(client, admin):
    assert client.get("/api/events/performance", params={"from": "2020-02-01", "to": "2020-01-01"}, headers=admin["headers"]).status_code == 422
    assert client.get("/api/events/performance", params={"timing": "UNSCHEDULED"}, headers=admin["headers"]).status_code == 422


def test_open_ended_period(client, admin, ref):
    """from and to are each optional: a missing side has no limit ("All time" sends neither)."""
    early = _event(client, admin, ref, date(2018, 2, 1))
    late = _event(client, admin, ref, date(2018, 11, 1))

    only_from = set(_by_id(_get(client, admin, **{"from": "2018-06-01"})))
    only_to = set(_by_id(_get(client, admin, to="2018-06-01")))

    assert late["id"] in only_from and early["id"] not in only_from
    assert early["id"] in only_to and late["id"] not in only_to


def test_timing_filter_applies_to_totals(client, admin, ref, codes):
    today = date.today()
    past = _event(client, admin, ref, today - timedelta(days=400), currency="CAD")
    upcoming = _event(client, admin, ref, today + timedelta(days=400), currency="CAD")
    _cost(past["id"], codes["categories"]["BOOTH_SPACE"], 1000, "CAD")
    _cost(upcoming["id"], codes["categories"]["BOOTH_SPACE"], 300, "CAD")
    period = {"from": str(today - timedelta(days=401)), "to": str(today + timedelta(days=401))}

    for timing, ours, other in (("UPCOMING", upcoming, past), ("PAST", past, upcoming)):
        body = _get(client, admin, **period, timing=timing)
        ids = {e["id"] for e in body["events"]}
        assert ours["id"] in ids and other["id"] not in ids
        assert {e["timing"] for e in body["events"]} == {timing}
        # Totals come from the same filtered events (other CAD events may exist locally)
        cad_events = [e for e in body["events"] if e["currency"] == "CAD"]
        [cad] = [t for t in body["totals"] if t["currency"] == "CAD"]
        assert cad["event_count"] == len(cad_events)
        assert cad["cost"] == round(sum(e["cost"] for e in cad_events), 2)
        assert cad["cost"] >= (300 if timing == "UPCOMING" else 1000)


def test_cost_breakdown_always_has_all_four_groups(client, admin, ref):
    event = _event(client, admin, ref, date(2019, 4, 1))

    body = _get(client, admin, **{"from": "2019-01-01", "to": "2019-12-31"})
    zeros = {"TRAVEL": 0, "BOOTH": 0, "MATERIALS": 0, "STAFF_OTHER": 0}

    e = _by_id(body)[event["id"]]
    assert e["cost_breakdown"] == zeros
    assert e["cost"] == 0 and e["other_currency_lines"] == 0
    assert e["cost_per_lead"] is None and e["roi_percent"] is None and e["money_outcome"] is None
    [totals] = body["totals"]
    assert totals["cost_breakdown"] == zeros
