"""Events Overview: GET /api/events/performance (contract: events-overview-contract.md).

Budgets, actual spend, budget variance, prior year spend, leads and cost per lead per currency (with the previous
period), monthly rows, the funnel, the cost breakdown, and each event with its prior year cost, results and ROI.

Each test uses its own years in the past and asks only for the temporary admin's events (owner filter), so other
events in the local database don't show up. Everything is created by the temporary admin and removed with it.
"""
import uuid
from datetime import date

import pytest

from services.event_performance_service import name_key_sql
from tests.conftest import run_sql

CATEGORIES = ("BOOTH_SPACE", "TRAVEL", "GROUND_TRANSPORT", "STAFF_TIME", "MEALS_PER_DIEMS", "MATERIALS", "OTHER")
FIGURES = ("approved_budget", "actual_spend", "budget_variance", "prior_year_spend", "leads", "cost_per_lead")
EVENT_FIELDS = {
    "id", "name", "event_code", "brand_name", "market_name", "city", "country_code", "event_type_name", "owner_name",
    "start_date", "end_date", "status", "currency", "approved_budget", "actual_cost", "awaiting_cost",
    "prior_year_cost", "prior_year_event_id", "variance", "attendees", "leads", "qualified_leads", "demos",
    "customers", "revenue", "cost_per_lead", "roi_percent", "cost_categories", "other_currency_lines",
}


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
        "budget_amount": None,
        "currency": "USD",
        "event": {"city": "Las Vegas", "country_code": "US"},
        **overrides,
    }
    response = client.post("/api/initiatives", json=body, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def _line(initiative_id, category_id, amount, status="PAID", currency="USD", source="MANUAL"):
    run_sql(
        "INSERT INTO spend_lines (initiative_id, category_id, amount, currency, incurred_on, status, paid_on, source) "
        "VALUES ($1, $2, $3, $4, '2011-01-01', $5::text, CASE WHEN $5::text = 'PAID' THEN DATE '2011-01-01' END, $6)",
        uuid.UUID(initiative_id), category_id, amount, currency, status, source,
    )


def _results(client, admin, initiative_id, period, values):
    response = client.put(
        f"/api/initiatives/{initiative_id}/outcomes/{period}",
        json={"values": [{"metric_code": k, "value": v} for k, v in values.items()]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text


def _get(client, admin, **params):
    """The temporary admin's events only, unless another owner is asked for."""
    params.setdefault("owner_user_id", str(admin["id"]))
    response = client.get("/api/events/performance", params=params, headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(body):
    return {e["id"]: e for e in body["events"]}


def _totals(body):
    return {t["currency"]: t for t in body["totals"]}


# --- One event ---


def test_event_row(client, admin, ref, codes):
    """$20,000 actual on a $25,000 budget: 200 leads, $100 per lead, $30,000 revenue → ROI 50%."""
    event = _event(client, admin, ref, date(2011, 3, 4), date(2011, 3, 6), budget_amount=25000)
    booth = codes["categories"]["BOOTH_SPACE"]
    _line(event["id"], booth, 20000)
    _line(event["id"], booth, 1000, status="ESTIMATED")
    _results(client, admin, event["id"], "2011-03-01",
             {"VISITORS": 400, "LEADS": 200, "MQLS": 50, "DEMOS": 20, "CUSTOMERS": 2, "REVENUE": 30000})

    body = _get(client, admin, **{"from": "2011-01-01", "to": "2011-12-31"})
    assert set(body) == {"period", "previous_period", "totals", "monthly", "funnel", "cost_breakdown", "events"}
    e = _by_id(body)[event["id"]]

    assert set(e) == EVENT_FIELDS
    assert (e["approved_budget"], e["actual_cost"], e["awaiting_cost"], e["variance"]) == (25000, 20000, 1000, -5000)
    assert (e["attendees"], e["leads"], e["qualified_leads"], e["demos"], e["customers"]) == (400, 200, 50, 20, 2)
    assert (e["revenue"], e["cost_per_lead"], e["roi_percent"]) == (30000, 100, 50.0)
    assert e["prior_year_cost"] is None and e["prior_year_event_id"] is None
    assert e["status"] == "PLANNED" and e["currency"] == "USD"
    assert e["event_code"] and e["brand_name"] and e["owner_name"] == "Pytest User"
    assert (e["city"], e["country_code"]) == ("Las Vegas", "US")
    assert e["market_name"] == run_sql("SELECT name FROM markets WHERE code = 'UNASSIGNED'")[0]["name"]

    [totals] = body["totals"]
    assert totals["currency"] == "USD" and totals["event_count"] == 1
    assert [totals[f] for f in FIGURES] == [25000, 20000, -5000, None, 200, 100]


def test_event_without_budget_results_or_cost(client, admin, ref):
    event = _event(client, admin, ref, date(2011, 8, 1))

    body = _get(client, admin, **{"from": "2011-08-01", "to": "2011-08-31"})
    e = _by_id(body)[event["id"]]

    assert (e["approved_budget"], e["variance"], e["actual_cost"], e["awaiting_cost"]) == (None, None, 0, 0)
    assert all(e[k] is None for k in ("attendees", "leads", "qualified_leads", "demos", "customers", "revenue"))
    assert e["cost_per_lead"] is None and e["roi_percent"] is None
    assert e["cost_categories"] == dict.fromkeys(CATEGORIES, 0)
    [totals] = body["totals"]
    assert (totals["approved_budget"], totals["budget_variance"], totals["actual_spend"]) == (None, None, 0)
    assert totals["leads"] is None and totals["cost_per_lead"] is None


def test_actual_cost_in_the_seven_categories_and_other_currencies(client, admin, ref, codes):
    """Actual is invoiced and paid only; estimates and commitments are awaiting. Each actual cost lands in one of the
    seven categories: a Navan cost is travel whatever its category, ground transport and meals are split out.
    Lines in another currency are counted, never converted."""
    event = _event(client, admin, ref, date(2013, 5, 6))
    cat = codes["categories"]
    _line(event["id"], cat["BOOTH_SPACE"], 8000)
    _line(event["id"], cat["FLOORING"], 600, status="INVOICED")
    _line(event["id"], cat["FLIGHTS"], 1500)
    _line(event["id"], cat["OTHER_TRAVEL"], 900, status="INVOICED", source="NAVAN")
    _line(event["id"], cat["GROUND_TRANSPORT"], 200)
    _line(event["id"], cat["PER_DIEMS_MEALS"], 120)
    _line(event["id"], cat["PER_DIEMS_MEALS"], 300, status="COMMITTED")
    _line(event["id"], cat["STAFF_TIME"], 50)
    _line(event["id"], cat["STAFF_TIME"], 2400, status="COMMITTED")
    _line(event["id"], cat["PRINT_SIGNAGE"], 100)
    _line(event["id"], cat["PRINT_SIGNAGE"], 700, status="ESTIMATED")
    _line(event["id"], cat["SPONSORSHIP_PACKAGE"], 5000)
    _line(event["id"], cat["AV_TECH"], 400, status="INVOICED")
    _line(event["id"], cat["BOOTH_SPACE"], 9999, currency="EUR")

    body = _get(client, admin, **{"from": "2013-01-01", "to": "2013-12-31"})
    e = _by_id(body)[event["id"]]

    expected = {"BOOTH_SPACE": 8600, "TRAVEL": 2400, "GROUND_TRANSPORT": 200, "STAFF_TIME": 50,
                "MEALS_PER_DIEMS": 120, "MATERIALS": 100, "OTHER": 5400}
    assert e["cost_categories"] == expected
    assert (e["actual_cost"], e["awaiting_cost"]) == (16870, 3400)
    assert sum(e["cost_categories"].values()) == e["actual_cost"]
    assert e["other_currency_lines"] == 1
    # The event is in USD, so the cost breakdown has only USD, and the EUR line is in no total
    assert body["cost_breakdown"] == [{"currency": "USD", **expected}]
    assert [t["currency"] for t in body["totals"]] == ["USD"]


def test_synced_results_beat_typed_and_demo_log_replaces_typed_demos(client, admin, ref, codes):
    event = _event(client, admin, ref, date(2013, 6, 10))
    _line(event["id"], codes["categories"]["BOOTH_SPACE"], 3000)
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

    body = _get(client, admin, **{"from": "2013-06-01", "to": "2013-06-30"})
    e = _by_id(body)[event["id"]]

    assert e["demos"] == 3
    assert e["revenue"] == 6000
    assert e["roi_percent"] == 100.0
    assert body["funnel"]["demos"] == 3


def test_revenue_in_another_currency_is_left_out(client, admin, ref, codes):
    event = _event(client, admin, ref, date(2013, 9, 2))
    _line(event["id"], codes["categories"]["BOOTH_SPACE"], 1000)
    response = client.put(
        f"/api/initiatives/{event['id']}/outcomes/2013-09-01",
        json={"values": [{"metric_code": "REVENUE", "value": 5000, "currency": "GBP"}]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text

    e = _by_id(_get(client, admin, **{"from": "2013-09-01", "to": "2013-09-30"}))[event["id"]]
    assert e["revenue"] is None and e["roi_percent"] is None


# --- Totals, previous period and a year earlier ---


def test_totals_previous_period_and_prior_year_spend(client, admin, ref, codes):
    """Period Q2 2012. Previous period: the 91 days before it. Prior year spend: Q2 2011 (and for the previous
    period, the 91 days a year before that)."""
    booth = codes["categories"]["BOOTH_SPACE"]
    budgeted = _event(client, admin, ref, date(2012, 5, 10), budget_amount=10000)
    unbudgeted = _event(client, admin, ref, date(2012, 6, 15))
    previous = _event(client, admin, ref, date(2012, 2, 1), budget_amount=5000)
    prior_year = _event(client, admin, ref, date(2011, 5, 1), budget_amount=9000)
    previous_prior_year = _event(client, admin, ref, date(2011, 1, 15))
    euros = _event(client, admin, ref, date(2012, 4, 20), currency="EUR", budget_amount=700)
    _line(budgeted["id"], booth, 8000)
    _line(unbudgeted["id"], booth, 2000, status="INVOICED")
    _line(unbudgeted["id"], booth, 999, status="ESTIMATED")  # not actual spend
    _line(previous["id"], booth, 6000)
    _line(prior_year["id"], booth, 3000)
    _line(previous_prior_year["id"], booth, 1500)
    _line(euros["id"], booth, 800, currency="EUR")
    _results(client, admin, budgeted["id"], "2012-05-01", {"LEADS": 100})
    _results(client, admin, previous["id"], "2012-02-01", {"LEADS": 40})

    body = _get(client, admin, **{"from": "2012-04-01", "to": "2012-06-30"})

    assert body["period"] == {"from": "2012-04-01", "to": "2012-06-30"}
    assert body["previous_period"] == {"from": "2012-01-01", "to": "2012-03-31"}
    assert set(_by_id(body)) == {budgeted["id"], unbudgeted["id"], euros["id"]}
    totals = _totals(body)
    usd = totals["USD"]
    assert usd["event_count"] == 2
    # Variance compares the budget only with the spend of the events that have one: 8,000 - 10,000
    assert {f: usd[f] for f in FIGURES} == {
        "approved_budget": 10000, "actual_spend": 10000, "budget_variance": -2000,
        "prior_year_spend": 3000, "leads": 100, "cost_per_lead": 100,
    }
    assert usd["previous"] == {
        "approved_budget": 5000, "actual_spend": 6000, "budget_variance": 1000,
        "prior_year_spend": 1500, "leads": 40, "cost_per_lead": 150,
    }
    # Nothing in euros before: what can't be worked out is null
    eur = totals["EUR"]
    assert (eur["approved_budget"], eur["actual_spend"], eur["budget_variance"]) == (700, 800, 100)
    assert eur["prior_year_spend"] is None
    assert eur["previous"] == dict.fromkeys(FIGURES)
    # Most events first
    assert [t["currency"] for t in body["totals"]] == ["USD", "EUR"]


def test_no_period_has_no_comparisons(client, admin, ref, codes):
    event = _event(client, admin, ref, date(2010, 3, 1), budget_amount=100)
    _line(event["id"], codes["categories"]["BOOTH_SPACE"], 50)

    body = _get(client, admin)

    assert body["period"] == {"from": None, "to": None}
    assert body["previous_period"] is None
    [usd] = body["totals"]
    assert (usd["actual_spend"], usd["prior_year_spend"]) == (50, None)
    assert usd["previous"] == dict.fromkeys(FIGURES)
    # Monthly rows span the events' own months
    assert [(m["month"], m["actual_spend"]) for m in body["monthly"]] == [("2010-03-01", 50)]


def test_monthly_rows(client, admin, ref, codes):
    """One row per month of the period and currency, by the month the event starts; an event that started before
    the period counts in its first month. Prior year spend sits in the month a year later."""
    booth = codes["categories"]["BOOTH_SPACE"]
    spans = _event(client, admin, ref, date(2015, 12, 30), date(2016, 1, 2), budget_amount=1000)
    march = _event(client, admin, ref, date(2016, 3, 20), budget_amount=2000)
    last_year = _event(client, admin, ref, date(2015, 2, 10))
    gbp = _event(client, admin, ref, date(2016, 2, 5), currency="GBP")
    _line(spans["id"], booth, 500)
    _line(march["id"], booth, 700)
    _line(last_year["id"], booth, 300)
    _line(gbp["id"], booth, 90, currency="GBP")

    body = _get(client, admin, **{"from": "2016-01-01", "to": "2016-03-31"})

    rows = [(m["currency"], m["month"], m["approved_budget"], m["actual_spend"], m["prior_year_spend"]) for m in body["monthly"]]
    assert rows == [
        ("GBP", "2016-01-01", 0, 0, 0),
        ("GBP", "2016-02-01", 0, 90, 0),
        ("GBP", "2016-03-01", 0, 0, 0),
        ("USD", "2016-01-01", 1000, 500, 0),
        ("USD", "2016-02-01", 0, 0, 300),
        ("USD", "2016-03-01", 2000, 700, 0),
    ]


def test_funnel_counts_every_event_whatever_its_currency(client, admin, ref):
    usd = _event(client, admin, ref, date(2017, 4, 3))
    cad = _event(client, admin, ref, date(2017, 5, 3), currency="CAD")
    _results(client, admin, usd["id"], "2017-04-01", {"VISITORS": 500, "LEADS": 100, "MQLS": 30, "DEMOS": 10})
    _results(client, admin, cad["id"], "2017-05-01", {"VISITORS": 250, "LEADS": 50, "DEMOS": 5})

    funnel = _get(client, admin, **{"from": "2017-01-01", "to": "2017-12-31"})["funnel"]

    assert funnel == {"attendees": 750, "leads": 150, "qualified_leads": 30, "demos": 15, "customers": None}


def test_nothing_in_scope(client, admin):
    body = _get(client, admin, **{"from": "2017-01-01", "to": "2017-01-31"})
    assert body["events"] == [] and body["totals"] == [] and body["monthly"] == [] and body["cost_breakdown"] == []
    assert body["funnel"] == dict.fromkeys(("attendees", "leads", "qualified_leads", "demos", "customers"))
    assert body["previous_period"] == {"from": "2016-12-01", "to": "2016-12-31"}


# --- Prior year event ---


@pytest.mark.parametrize(
    "name, key",
    [
        ("SaaS North Expo 2025", "saas north expo"),
        ("  SaaS North Expo 2026 ", "saas north expo"),
        ("2026 SaaS   North Expo", "saas north expo"),
        ("SaaS 2026 North Expo", "saas north expo"),
        ("SaaS North Expo 1999", "saas north expo 1999"),     # only 2000 to 2099
        ("SaaS North Expo 20261", "saas north expo 20261"),   # not a 4-digit year
        ("SaaS North Expo 2100", "saas north expo 2100"),
    ],
)
def test_name_key(name, key):
    assert run_sql(f"SELECT {name_key_sql('$1::text')} AS key", name)[0]["key"] == key


def test_prior_year_event_by_name(client, admin, ref, codes):
    """Last year's event: same name once lower-cased, trimmed and without the year, started 300 to 430 days before;
    the one closest to exactly a year earlier wins."""
    tag = uuid.uuid4().hex[:6]
    booth = codes["categories"]["BOOTH_SPACE"]
    current = _event(client, admin, ref, date(2014, 6, 10), name=f"Pytest Summit {tag} 2014")
    closest = _event(client, admin, ref, date(2013, 6, 12), name=f"  pytest summit {tag} 2013 ")   # 2 days off
    further = _event(client, admin, ref, date(2013, 6, 5), name=f"Pytest Summit {tag}")            # 5 days off
    _event(client, admin, ref, date(2013, 6, 10), name=f"Pytest Summit {tag} 1999")                # not a year
    _event(client, admin, ref, date(2013, 6, 10), name=f"Pytest Summit {tag} Europe 2013")         # another name
    _event(client, admin, ref, date(2012, 6, 10), name=f"Pytest Summit {tag} 2012")                # 730 days before
    _event(client, admin, ref, date(2013, 11, 1), name=f"Pytest Summit {tag} 2013")                # 221 days before
    _line(closest["id"], booth, 4000)
    _line(closest["id"], booth, 500, status="ESTIMATED")  # not actual
    _line(further["id"], booth, 1)

    e = _by_id(_get(client, admin, **{"from": "2014-06-01", "to": "2014-06-30"}))[current["id"]]
    assert (e["prior_year_event_id"], e["prior_year_cost"]) == (closest["id"], 4000)

    # Without the closest one, the next closest within the window
    run_sql("DELETE FROM initiatives WHERE id = $1", uuid.UUID(closest["id"]))
    e = _by_id(_get(client, admin, **{"from": "2014-06-01", "to": "2014-06-30"}))[current["id"]]
    assert (e["prior_year_event_id"], e["prior_year_cost"]) == (further["id"], 1)


def test_prior_year_window_edges_and_draft_or_cancelled_last_year(client, admin, ref):
    """300 and 430 days before both count; any event in the Events channel counts, whatever its status."""
    tag = uuid.uuid4().hex[:6]
    at_300 = _event(client, admin, ref, date(2018, 10, 28), name=f"Pytest Edge {tag} 2018")
    cancelled = _event(client, admin, ref, date(2018, 1, 1), name=f"Pytest Edge {tag} 2017", status="CANCELLED")  # 300 days
    at_430 = _event(client, admin, ref, date(2019, 3, 7), name=f"Pytest Fringe {tag}")
    old = _event(client, admin, ref, date(2018, 1, 1), name=f"Pytest Fringe {tag}", status="DRAFT")  # 430 days
    assert (date(2018, 10, 28) - date(2018, 1, 1)).days == 300 and (date(2019, 3, 7) - date(2018, 1, 1)).days == 430

    first = _by_id(_get(client, admin, **{"from": "2018-10-01", "to": "2018-10-31"}))[at_300["id"]]
    second = _by_id(_get(client, admin, **{"from": "2019-03-01", "to": "2019-03-31"}))[at_430["id"]]

    assert (first["prior_year_event_id"], first["prior_year_cost"]) == (cancelled["id"], 0)
    assert second["prior_year_event_id"] == old["id"]


def test_prior_year_cost_only_in_the_same_currency(client, admin, ref, codes):
    tag = uuid.uuid4().hex[:6]
    current = _event(client, admin, ref, date(2014, 9, 10), name=f"Pytest Fair {tag} 2014")
    euros = _event(client, admin, ref, date(2013, 9, 10), name=f"Pytest Fair {tag} 2013", currency="EUR")
    _line(euros["id"], codes["categories"]["BOOTH_SPACE"], 2000, currency="EUR")

    e = _by_id(_get(client, admin, **{"from": "2014-09-01", "to": "2014-09-30"}))[current["id"]]
    assert (e["prior_year_event_id"], e["prior_year_cost"]) == (euros["id"], None)


# --- Scope, order and filters ---


def test_period_overlap_drafts_and_undated_events(client, admin, ref):
    spans = _event(client, admin, ref, date(2014, 12, 30), date(2015, 1, 2))
    inside = _event(client, admin, ref, date(2015, 3, 1))
    after = _event(client, admin, ref, date(2015, 4, 1))
    draft = _event(client, admin, ref, date(2015, 3, 2), status="DRAFT")

    body = _get(client, admin, **{"from": "2015-01-01", "to": "2015-03-31"})

    # Newest start first
    assert [e["id"] for e in body["events"]] == [inside["id"], spans["id"]]
    assert after["id"] not in _by_id(body) and draft["id"] not in _by_id(body)
    # Without a period every dated event counts; undated ones never do
    undated = _event(client, admin, ref, date(2015, 3, 3), status="DRAFT")
    run_sql("UPDATE initiatives SET start_date = NULL, end_date = NULL, status = 'PLANNED' WHERE id = $1", uuid.UUID(undated["id"]))
    all_ids = set(_by_id(_get(client, admin)))
    assert {spans["id"], inside["id"], after["id"]} <= all_ids
    assert undated["id"] not in all_ids


def test_open_ended_period(client, admin, ref):
    """from and to are each optional: a missing side has no limit ("All time" sends neither)."""
    early = _event(client, admin, ref, date(2018, 2, 1))
    late = _event(client, admin, ref, date(2018, 11, 1))

    only_from = _get(client, admin, **{"from": "2018-06-01"})
    only_to = set(_by_id(_get(client, admin, to="2018-06-01")))

    assert late["id"] in _by_id(only_from) and early["id"] not in _by_id(only_from)
    assert early["id"] in only_to and late["id"] not in only_to
    assert only_from["previous_period"] is None


def test_filters(client, admin, ref, codes, viewer):
    """Brand, market (the Region filter), event type, owner and initiative status."""
    period = {"from": "2016-06-01", "to": "2016-06-30"}
    london = codes["markets"]["LONDON"]
    trade_show = run_sql("SELECT id FROM event_types WHERE code = 'TRADE_SHOW'")[0]["id"]
    plain = _event(client, admin, ref, date(2016, 6, 1))
    other_brand = _event(client, admin, ref, date(2016, 6, 2), brand_id=ref["brands"]["OTHERSHIP"])
    in_london = _event(client, admin, ref, date(2016, 6, 3), market_id=london)
    show = _event(client, admin, ref, date(2016, 6, 4), event={"city": "Las Vegas", "country_code": "US", "event_type_id": trade_show})
    cancelled = _event(client, admin, ref, date(2016, 6, 5), status="CANCELLED")
    someone_elses = _event(client, admin, ref, date(2016, 6, 6), owner_user_id=str(viewer["id"]))

    everything = set(_by_id(_get(client, admin, **period)))
    assert everything == {plain["id"], other_brand["id"], in_london["id"], show["id"], cancelled["id"]}
    assert set(_by_id(_get(client, admin, **period, brand_id=ref["brands"]["OTHERSHIP"]))) == {other_brand["id"]}
    assert set(_by_id(_get(client, admin, **period, market_id=london))) == {in_london["id"]}
    by_type = _by_id(_get(client, admin, **period, event_type_id=trade_show))
    assert set(by_type) == {show["id"]} and by_type[show["id"]]["event_type_name"] == "Trade show"
    by_status = _get(client, admin, **period, status="CANCELLED")
    assert set(_by_id(by_status)) == {cancelled["id"]} and by_status["totals"][0]["event_count"] == 1
    assert set(_by_id(_get(client, admin, **period, owner_user_id=str(viewer["id"])))) == {someone_elses["id"]}
    # Totals come from the filtered events
    assert _get(client, admin, **period, market_id=london)["totals"][0]["event_count"] == 1


def test_filters_apply_to_the_previous_period_and_prior_year(client, admin, ref, codes):
    booth = codes["categories"]["BOOTH_SPACE"]
    london = codes["markets"]["LONDON"]
    now = _event(client, admin, ref, date(2019, 7, 10), market_id=london)
    before = _event(client, admin, ref, date(2019, 6, 10), market_id=london)
    before_elsewhere = _event(client, admin, ref, date(2019, 6, 11))
    last_year = _event(client, admin, ref, date(2018, 7, 10), market_id=london)
    last_year_elsewhere = _event(client, admin, ref, date(2018, 7, 11))
    for event, amount in ((now, 100), (before, 200), (before_elsewhere, 1000), (last_year, 300), (last_year_elsewhere, 1000)):
        _line(event["id"], booth, amount)

    [usd] = _get(client, admin, **{"from": "2019-07-01", "to": "2019-07-31"}, market_id=london)["totals"]

    assert (usd["actual_spend"], usd["previous"]["actual_spend"], usd["prior_year_spend"]) == (100, 200, 300)


def test_rejects_a_backwards_period_and_unknown_status(client, admin):
    assert client.get("/api/events/performance", params={"from": "2020-02-01", "to": "2020-01-01"}, headers=admin["headers"]).status_code == 422
    assert client.get("/api/events/performance", params={"status": "UNKNOWN"}, headers=admin["headers"]).status_code == 422


# --- Access ---


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
