"""The calculations behind "was it worth it?" — pure functions, so these tests need no database."""
from datetime import date

import pytest

from services import initiative_metrics as m


def _rating(**overrides):
    args = {
        "kpi_code": "CPL", "kpi_name": "CPL", "actual": 100.0, "target": 100.0, "matures": date(2026, 1, 1),
        "maturity_days": 180, "today": date(2026, 10, 14), "roi": None, "revenue_recorded": False, "currency": "USD",
    }
    args.update(overrides)
    return m.suggest_rating(**args)


def test_ceo_example():
    """$20,000, 200 leads, 20 demos, 2 customers, $20,000 revenue: $100 / $1,000 / $10,000, ROI 0 — broke even."""
    counts = {"LEADS": 200, "DEMOS": 20, "CUSTOMERS": 2}
    ladder = {step["code"]: step for step in m.cost_ladder(20000, counts)}

    assert ladder["LEADS"]["cost_per"] == 100
    assert ladder["DEMOS"]["cost_per"] == 1000
    assert ladder["CUSTOMERS"]["cost_per"] == 10000
    assert ladder["LEADS"]["from_previous"] is None
    assert ladder["DEMOS"]["from_previous"] == 10.0  # 10% of leads got a demo
    assert ladder["CUSTOMERS"]["from_previous"] == 10.0  # 10% of demos became customers
    assert m.roi_percent(20000, 20000) == 0.0
    assert m.money_outcome(20000, 20000, 0.0) == "BROKE_EVEN"
    assert m.kpi_actual("CPL", 20000, counts, 20000, None) == 100
    assert m.kpi_actual("CP_DEMO", 20000, counts, 20000, None) == 1000
    assert m.kpi_actual("CAC", 20000, counts, 20000, None) == 10000


def test_ladder_skips_missing_steps_and_never_divides_by_zero():
    ladder = m.cost_ladder(5000, {"VISITORS": 500, "LEADS": 0, "CUSTOMERS": 5})
    assert [s["code"] for s in ladder] == ["VISITORS", "CUSTOMERS"]
    assert ladder[1]["from_previous"] == 1.0
    assert ladder[1]["previous_name"] == "Visitors"
    # No cost yet: counts still show, cost per step doesn't
    assert m.cost_ladder(0, {"LEADS": 10})[0]["cost_per"] is None
    assert m.cost_ladder(1000, {}) == []


def test_summary_ratios():
    ratios = m.summary_ratios(2000, {"IMPRESSIONS": 100000, "CLICKS": 1500}, 5000)
    assert ratios == {"ctr": 1.5, "cpc": 1.33, "cpm": 20.0, "roi_percent": 150.0}
    assert m.summary_ratios(0, {}, None) == {"ctr": None, "cpc": None, "cpm": None, "roi_percent": None}
    # Ad ratios only where the channel uses them: none on an event, click-through but no CPC/CPM for SEO
    counts = {"IMPRESSIONS": 100000, "CLICKS": 1500}
    assert m.summary_ratios(2000, counts, 5000, "EVENTS") == {"ctr": None, "cpc": None, "cpm": None, "roi_percent": 150.0}
    assert m.summary_ratios(2000, counts, 5000, "SEO") == {"ctr": 1.5, "cpc": None, "cpm": None, "roi_percent": 150.0}


@pytest.mark.parametrize("channel", sorted(m.KPI_CHOICES_BY_CHANNEL))
def test_channel_kpis_only_use_results_it_records(channel):
    """A KPI a channel can pick must be measurable from results that channel can record."""
    recorded = set(m.RESULT_METRICS_BY_CHANNEL[channel])
    for kpi in m.KPI_CHOICES_BY_CHANNEL[channel]:
        assert m.KPI_NEEDS.get(kpi, set()) <= recorded, f"{channel} can pick {kpi} but can't record {m.KPI_NEEDS[kpi] - recorded}"


def test_every_calculated_kpi_says_what_it_needs():
    assert set(m.KPI_NEEDS) == set(m.KPI_DEFINITIONS)
    assert set(m.KPI_CHOICES_BY_CHANNEL) == set(m.RESULT_METRICS_BY_CHANNEL)


def test_events_never_get_ad_measures():
    assert not {"IMPRESSIONS", "CTR", "CPC", "CPM"} & set(m.KPI_CHOICES_BY_CHANNEL["EVENTS"])
    assert not {"IMPRESSIONS", "CLICKS", "CONVERSIONS"} & set(m.RESULT_METRICS_BY_CHANNEL["EVENTS"])
    assert not m.result_fits_channel("IMPRESSIONS", "EVENTS")
    assert m.result_fits_channel("IMPRESSIONS", "OTHER")


def test_money_outcome():
    assert m.money_outcome(0, 100, None) is None
    assert m.money_outcome(1000, None, None) == "NO_REVENUE"
    assert m.money_outcome(1000, 2000, 100.0) == "PROFIT"
    assert m.money_outcome(1000, 500, -50.0) == "LOST_MONEY"


def test_matures_on():
    assert m.matures_on(date(2026, 5, 12), date(2026, 5, 14), 120) == date(2026, 9, 11)
    assert m.matures_on(date(2026, 5, 12), None, 10) == date(2026, 5, 22)
    assert m.matures_on(None, None, 180) is None


@pytest.mark.parametrize(
    "actual, expected",
    [
        (100.0, "HIGH"),    # exactly on target
        (80.0, "HIGH"),     # beat it
        (119.9, "MEDIUM"),  # 19.9% over
        (120.0, "MEDIUM"),  # 20% over: still within
        (120.1, "LOW"),     # 20.1% over
    ],
)
def test_rating_when_lower_is_better(actual, expected):
    assert _rating(actual=actual)["code"] == expected


@pytest.mark.parametrize(
    "actual, expected",
    [(25.0, "HIGH"), (30.0, "HIGH"), (20.0, "MEDIUM"), (20.1, "MEDIUM"), (19.9, "LOW")],
)
def test_rating_when_higher_is_better(actual, expected):
    assert _rating(kpi_code="ROI", kpi_name="ROI", actual=actual, target=25.0)["code"] == expected


def test_rating_is_test_inside_the_maturity_window():
    rating = _rating(matures=date(2026, 11, 13))
    assert rating["code"] == "TEST"
    assert "30 days left in the 180-day waiting period" in rating["reason"]
    assert "13 November 2026" in rating["reason"]


@pytest.mark.parametrize(
    "overrides, phrase",
    [
        ({"kpi_code": None, "kpi_name": None}, "Choose a main KPI"),
        ({"kpi_code": "CONVERSION_RATE", "kpi_name": "Conversion Rate"}, "isn't calculated yet"),
        ({"target": None}, "Set a target"),
        ({"actual": None}, "Not enough results"),
    ],
)
def test_rating_is_test_without_what_it_needs(overrides, phrase):
    rating = _rating(**overrides)
    assert rating["code"] == "TEST"
    assert phrase in rating["reason"]


def test_breaking_even_is_low_whatever_the_kpi_says():
    """The CEO's rule: once revenue is in, breaking even or losing money isn't a success."""
    even = _rating(actual=50.0, roi=0.0, revenue_recorded=True)
    assert even["code"] == "LOW"
    assert "broke even" in even["reason"]
    assert _rating(actual=50.0, roi=-10.0, revenue_recorded=True)["code"] == "LOW"
    # No revenue recorded (e.g. an awareness push): judged on the KPI alone
    assert _rating(actual=50.0, roi=None, revenue_recorded=False)["code"] == "HIGH"
    # Still inside the window: too early, even if it looks like break-even so far
    assert _rating(actual=50.0, roi=0.0, revenue_recorded=True, matures=date(2027, 1, 1))["code"] == "TEST"


def test_final_verdict_leads_with_the_return():
    # Made money and met the KPI
    assert _rating(actual=90.0, roi=50.0, revenue_recorded=True)["code"] == "HIGH"
    # Made money, no target set: still worth it
    made_money = _rating(target=None, roi=50.0, revenue_recorded=True)
    assert made_money["code"] == "HIGH"
    assert made_money["reason"] == "It made money: ROI 50%."
    # Made money but cost far more per lead than planned
    costly = _rating(actual=150.0, roi=20.0, revenue_recorded=True)
    assert costly["code"] == "MEDIUM"
    assert costly["reason"].startswith("It made money (ROI 20%), but CPL USD 150 missed the target of USD 100 by 50%")


def test_verdict_stage():
    today = date(2026, 10, 14)
    assert m.verdict_stage(date(2026, 11, 4), date(2027, 5, 5), today) == "UPCOMING"
    assert m.verdict_stage(date(2026, 9, 1), date(2027, 3, 1), today) == "EARLY"
    assert m.verdict_stage(date(2026, 1, 1), date(2026, 7, 1), today) == "FINAL"
    assert m.verdict_stage(None, None, today) == "FINAL"


def test_kpi_fits_channel():
    assert m.kpi_fits_channel("CP_DEMO", "EVENTS")
    assert not m.kpi_fits_channel("CPM", "EVENTS")
    assert m.kpi_fits_channel("CPM", "PAID_SEARCH")
    assert not m.kpi_fits_channel("CPM", "SEO")  # organic search doesn't buy impressions
    assert m.kpi_fits_channel("CPM", "OTHER")  # only "Other" can use any KPI
    assert m.kpi_fits_channel(None, "EVENTS")


def test_suggested_decisions():
    assert m.SUGGESTED_DECISION == {"HIGH": "SCALE", "MEDIUM": "OPTIMIZE", "LOW": "PAUSE", "TEST": "TEST_MORE"}
