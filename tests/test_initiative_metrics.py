"""The calculations behind "was it worth it?" — pure functions, so these tests need no database."""
from datetime import date

import pytest

from services import initiative_metrics as m


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
    assert m.suggest_rating(roi=0.0, revenue_recorded=True)["code"] == "LOW"


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


@pytest.mark.parametrize("channel", sorted(m.AD_RATIOS_BY_CHANNEL))
def test_ad_ratios_only_use_results_the_channel_records(channel):
    """An ad ratio shown for a channel must be measurable from results that channel can record."""
    recorded = set(m.RESULT_METRICS_BY_CHANNEL[channel])
    for ratio in m.AD_RATIOS_BY_CHANNEL[channel]:
        assert m.AD_RATIO_NEEDS[ratio] <= recorded, f"{channel} shows {ratio} but can't record {m.AD_RATIO_NEEDS[ratio] - recorded}"


def test_every_channel_has_ad_ratio_rules():
    assert set(m.AD_RATIOS_BY_CHANNEL) == set(m.RESULT_METRICS_BY_CHANNEL)


def test_events_never_get_ad_measures():
    assert not m.AD_RATIOS_BY_CHANNEL["EVENTS"]
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
    "roi, revenue_recorded, expected, phrase",
    [
        (50.0, True, "HIGH", "It made money: ROI 50%."),
        (0.1, True, "HIGH", "It made money: ROI 0.1%."),
        # The CEO's rule: breaking even or losing money isn't a success
        (0.0, True, "LOW", "It broke even (ROI 0%), so it's not a success."),
        (-10.0, True, "LOW", "It lost money (ROI -10%), so it's not a success."),
        # No revenue yet (e.g. an awareness push): no verdict, whatever the leads or demos
        (None, False, "TEST", "No revenue recorded yet"),
        # Revenue but no cost: the return can't be worked out
        (None, True, "TEST", "No costs recorded yet"),
    ],
)
def test_rating_is_judged_on_the_return(roi, revenue_recorded, expected, phrase):
    rating = m.suggest_rating(roi=roi, revenue_recorded=revenue_recorded)
    assert rating["code"] == expected
    assert rating["name"] == m.RATING_NAMES[expected]
    assert rating["reason"].startswith(phrase)


def test_verdict_stage():
    today = date(2026, 10, 14)
    assert m.verdict_stage(date(2026, 11, 4), date(2027, 5, 5), today) == "UPCOMING"
    assert m.verdict_stage(date(2026, 9, 1), date(2027, 3, 1), today) == "EARLY"
    assert m.verdict_stage(date(2026, 1, 1), date(2026, 7, 1), today) == "FINAL"
    assert m.verdict_stage(None, None, today) == "FINAL"


def test_ad_ratio_fits_channel():
    assert not m.ad_ratio_fits_channel("CPM", "EVENTS")
    assert m.ad_ratio_fits_channel("CPM", "PAID_SEARCH")
    assert not m.ad_ratio_fits_channel("CPM", "SEO")  # organic search doesn't buy impressions
    assert m.ad_ratio_fits_channel("CTR", "SEO")
    assert m.ad_ratio_fits_channel("CPM", "OTHER")  # only "Other" gets every ratio


def test_suggested_decisions():
    assert m.SUGGESTED_DECISION == {"HIGH": "SCALE", "MEDIUM": "OPTIMIZE", "LOW": "PAUSE", "TEST": "TEST_MORE"}
