"""GET /api/initiatives/{id}/spend-summary, and cost totals on the initiatives list."""
import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import run_sql


@pytest.fixture(scope="module")
def categories():
    return {r["code"]: r["id"] for r in run_sql("SELECT id, code FROM spend_categories")}


@pytest.fixture
def make_event(client, admin, ref):
    """Create an in-person event; budget, currency and dates can be overridden."""

    def _make(**overrides):
        payload = {
            "name": f"Pytest Summary Expo {uuid.uuid4().hex[:6]}",
            "brand_id": ref["brands"]["DESKFLEX"],
            "channel_id": ref["channels"]["EVENTS"],
            "sub_channel_id": ref["sub_channels"]["IN_PERSON_EVENTS"],
            "budget_amount": 10000,
            "currency": "USD",
            "start_date": "2026-11-04",
            "end_date": "2026-11-06",
            "event": {"city": "Las Vegas", "country_code": "US"},
            **overrides,
        }
        response = client.post("/api/initiatives", json=payload, headers=admin["headers"])
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
def add_cost(client, admin, categories):
    def _add(initiative_id, code, amount, status="COMMITTED", **fields):
        body = {"category_id": categories[code], "amount": amount, "status": status, "incurred_on": "2026-11-04", **fields}
        if status == "PAID":
            body.setdefault("paid_on", "2026-11-10")
        response = client.post(f"/api/initiatives/{initiative_id}/spend-lines", json=body, headers=admin["headers"])
        assert response.status_code == 201, response.text
        return response.json()

    return _add


def _summary(client, admin, initiative_id):
    response = client.get(f"/api/initiatives/{initiative_id}/spend-summary", headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def test_each_status_lands_in_the_right_totals(client, admin, make_event, add_cost):
    event = make_event()
    add_cost(event["id"], "FREIGHT", 1000, "ESTIMATED")
    add_cost(event["id"], "FLOORING", 2000, "COMMITTED")
    add_cost(event["id"], "BOOTH_BUILD", 3000, "INVOICED")
    add_cost(event["id"], "BOOTH_SPACE", 4000, "PAID")

    totals = _summary(client, admin, event["id"])["totals"]

    assert totals["expected_total"] == 10000
    assert totals["still_estimated"] == 1000
    assert totals["committed"] == 9000  # the estimate isn't committed
    assert totals["actual"] == 7000
    assert totals["paid"] == 4000
    assert (totals["forecast_remaining"], totals["remaining_committed"]) == (0, 1000)
    assert (totals["percent_expected"], totals["percent_committed"], totals["percent_actual"]) == (100, 90, 70)


def test_estimates_can_push_the_forecast_over_budget(client, admin, make_event, add_cost):
    event = make_event(budget_amount=5000)
    add_cost(event["id"], "BOOTH_SPACE", 4000, "PAID")
    add_cost(event["id"], "FREIGHT", 1500, "ESTIMATED")

    totals = _summary(client, admin, event["id"])["totals"]

    assert totals["forecast_remaining"] == -500
    assert totals["remaining_committed"] == 1000  # only what's committed is within budget
    assert totals["percent_expected"] == 110


def test_awaiting_invoice_only_counts_past_committed_costs(client, admin, make_event, add_cost):
    event = make_event()
    past = (date.today() - timedelta(days=10)).isoformat()
    future = (date.today() + timedelta(days=10)).isoformat()
    add_cost(event["id"], "FLOORING", 600, "COMMITTED", incurred_on=past)
    add_cost(event["id"], "LIGHTING", 450, "COMMITTED", incurred_on=future)
    add_cost(event["id"], "ELECTRICAL", 350, "INVOICED", incurred_on=past)

    awaiting = _summary(client, admin, event["id"])["totals"]["awaiting_invoice"]

    assert awaiting == {"count": 1, "amount": 600}


def test_a_refund_reduces_totals(client, admin, make_event, add_cost):
    event = make_event()
    add_cost(event["id"], "BOOTH_SPACE", 4000, "PAID")
    add_cost(event["id"], "BOOTH_SPACE", -500, "PAID", description="Deposit refund")

    summary = _summary(client, admin, event["id"])

    assert (summary["totals"]["expected_total"], summary["totals"]["paid"]) == (3500, 3500)
    assert summary["by_expense_type"][0]["categories"][0]["line_count"] == 2


def test_breakdown_nests_and_adds_up(client, admin, make_event, add_cost):
    event = make_event()
    add_cost(event["id"], "FLOORING", 333.33, "COMMITTED")
    add_cost(event["id"], "LIGHTING", 333.33, "COMMITTED")
    add_cost(event["id"], "BOOTH_SPACE", 333.34, "PAID")
    add_cost(event["id"], "PRINT_SIGNAGE", 1000, "ESTIMATED")

    summary = _summary(client, admin, event["id"])
    types = summary["by_expense_type"]

    assert [t["code"] for t in types] == ["SPACE_SPONSORSHIP", "SHOW_SERVICES", "MARKETING_PROMOTION"]  # sort order
    show_services = types[1]
    assert [c["name"] for c in show_services["categories"]] == ["Lighting", "Carpet & flooring"]
    assert show_services["expected_total"] == pytest.approx(sum(c["expected_total"] for c in show_services["categories"]))
    assert sum(t["expected_total"] for t in types) == pytest.approx(summary["totals"]["expected_total"])
    assert sum(t["percent_of_total"] for t in types) == pytest.approx(100, abs=0.2)


def test_variance_only_counts_invoiced_or_paid_lines_with_an_estimate(client, admin, make_event, add_cost):
    event = make_event()
    add_cost(event["id"], "FREIGHT", 1800, "INVOICED", estimated_amount=1500)  # +300
    add_cost(event["id"], "COURIER", 200, "COMMITTED", estimated_amount=100)  # not invoiced yet
    add_cost(event["id"], "DRAYAGE", 400, "PAID")  # no estimate

    shipping = _summary(client, admin, event["id"])["by_expense_type"][0]

    assert shipping["code"] == "SHIPPING_LOGISTICS"
    assert shipping["variance"] == 300
    assert {c["name"]: c["variance"] for c in shipping["categories"]} == {
        "Freight": 300, "Drayage / material handling": 0, "Courier": 0,
    }


def test_travel_feed_costs_show_under_travel(client, admin, make_event, add_cost, categories):
    event = make_event()
    add_cost(event["id"], "BOOTH_SPACE", 4000, "PAID")
    run_sql(
        "INSERT INTO spend_lines (initiative_id, category_id, status, amount, currency, incurred_on, paid_on, source, external_ref) "
        "VALUES ($1, $2, 'PAID', 1200, 'USD', $3, $3, 'TRAVEL_FEED', $4)",
        event["id"], categories["FLIGHTS"], date(2026, 11, 3), f"TRIP-{uuid.uuid4().hex[:6]}",
    )

    summary = _summary(client, admin, event["id"])

    travel = next(t for t in summary["by_expense_type"] if t["code"] == "TRAVEL_ACCOMMODATION")
    assert (travel["source"], travel["expected_total"], travel["categories"][0]["name"]) == ("TRAVEL_SYSTEM", 1200, "Flights")
    assert summary["by_source"] == {"supplier": 4000, "travel": 1200}


def test_other_currencies_are_reported_separately(client, admin, make_event, add_cost):
    event = make_event()
    add_cost(event["id"], "BOOTH_SPACE", 4000, "PAID")
    add_cost(event["id"], "PRINT_SIGNAGE", 500, "INVOICED", currency="GBP")

    summary = _summary(client, admin, event["id"])

    assert summary["totals"]["expected_total"] == 4000  # never converted into the USD totals
    assert summary["mixed_currencies"] is True
    assert summary["other_currencies"] == [{"currency": "GBP", "expected_total": 500, "committed": 500, "actual": 500}]
    assert [t["code"] for t in summary["by_expense_type"]] == ["SPACE_SPONSORSHIP"]
    assert sum(m["expected_total"] for m in summary["by_month"]) == 4000


def test_by_month_and_per_day(client, admin, make_event, add_cost):
    event = make_event()  # 4–6 November: 3 days
    add_cost(event["id"], "BOOTH_SPACE", 6000, "PAID", incurred_on="2026-06-15")
    add_cost(event["id"], "FLOORING", 3000, "COMMITTED", incurred_on="2026-11-04")

    summary = _summary(client, admin, event["id"])

    assert summary["per_day"] == 3000
    assert summary["by_month"] == [
        {"month": "2026-06-01", "expected_total": 6000, "committed": 6000, "actual": 6000},
        {"month": "2026-11-01", "expected_total": 3000, "committed": 3000, "actual": 0},
    ]


def test_without_budget_or_dates(client, admin, make_event, add_cost):
    event = make_event(budget_amount=None, start_date=None, end_date=None)
    add_cost(event["id"], "BOOTH_SPACE", 4000, "PAID")

    summary = _summary(client, admin, event["id"])

    assert summary["budget_amount"] is None
    totals = summary["totals"]
    assert (totals["percent_expected"], totals["percent_committed"], totals["percent_actual"]) == (None, None, None)
    assert (totals["forecast_remaining"], totals["remaining_committed"]) == (None, None)
    assert summary["per_day"] is None


def test_summary_of_an_event_without_costs(client, admin, make_event):
    summary = _summary(client, admin, make_event()["id"])

    assert summary["totals"]["expected_total"] == 0
    assert summary["by_expense_type"] == [] and summary["by_month"] == []
    assert summary["mixed_currencies"] is False


def test_summary_of_a_missing_initiative(client, admin):
    assert client.get(f"/api/initiatives/{uuid.uuid4()}/spend-summary", headers=admin["headers"]).status_code == 404


def test_list_returns_cost_totals(client, admin, make_event, add_cost):
    event = make_event()
    add_cost(event["id"], "FREIGHT", 1000, "ESTIMATED")
    add_cost(event["id"], "BOOTH_SPACE", 4000, "PAID")
    add_cost(event["id"], "PRINT_SIGNAGE", 500, "PAID", currency="GBP")

    response = client.get("/api/initiatives", params={"search": event["name"]}, headers=admin["headers"])

    item = response.json()["items"][0]
    assert (item["expected_total"], item["actual_amount"], item["has_mixed_currencies"]) == (5000, 4000, True)
