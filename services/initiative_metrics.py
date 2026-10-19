"""Turns an initiative's costs and results into the cost-per-outcome ladder, ROI and a suggested rating.

The rating is judged on the return (ROI from revenue and cost); there are no KPIs or targets to set.

Pure functions, no database: everything here is calculated on request and never stored.

The CEO's example is the yardstick: a $20,000 trade show with 200 leads, 20 demos and 2 customers costs
$100 per lead, $1,000 per demo and $10,000 per customer. If those customers bring in $20,000, the show only
broke even (ROI 0%), and breaking even does not count as a success.
"""
from datetime import date, timedelta

# Funnel order for the cost-per-outcome ladder. Each step shows its count, what each one cost, and how
# many of the step before it made it this far.
LADDER = (
    ("IMPRESSIONS", "Impressions"),
    ("CLICKS", "Clicks"),
    ("VISITORS", "Visitors"),
    ("LEADS", "Leads"),
    ("FOLLOW_UPS", "Follow-ups"),
    ("MQLS", "MQLs"),
    ("SQLS", "SQLs"),
    ("TRIALS", "Trials"),
    ("DEMOS", "Demos"),
    ("CONVERSIONS", "Conversions"),
    ("CUSTOMERS", "Customers"),
)

# The decision the system suggests for each rating; Marketing makes the actual call. Medium is no longer suggested
# (it meant "made money but missed the KPI target"); it stays for decisions recorded before.
SUGGESTED_DECISION = {"HIGH": "SCALE", "MEDIUM": "OPTIMIZE", "LOW": "PAUSE", "TEST": "TEST_MORE"}

RATING_NAMES = {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low", "TEST": "Test"}


def _ratio(part: float | None, whole: float | None) -> float | None:
    """part ÷ whole, or None when either is missing or the whole is zero — never an error."""
    if part is None or not whole:
        return None
    return part / whole


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _percent(value: float | None) -> float | None:
    return None if value is None else round(value * 100, 1)


def roi_percent(cost: float, revenue: float | None) -> float | None:
    """(revenue − cost) ÷ cost, as a percentage; None without cost or revenue."""
    if revenue is None or not cost:
        return None
    return round((revenue - cost) / cost * 100, 1)


def cost_ladder(cost: float, counts: dict[str, float]) -> list[dict]:
    """The funnel steps that have a number, each with cost per step and conversion from the step before."""
    steps = []
    previous = None
    for code, name in LADDER:
        count = counts.get(code)
        if not count:
            continue
        steps.append({
            "code": code,
            "name": name,
            "count": count,
            "cost_per": _money(_ratio(cost, count)) if cost else None,
            "from_previous": _percent(_ratio(count, previous["count"])) if previous else None,
            "previous_name": previous["name"] if previous else None,
        })
        previous = steps[-1]
    return steps


def summary_ratios(cost: float, counts: dict[str, float], revenue: float | None, channel_code: str | None = None) -> dict:
    """Ratios that sit outside the ladder: click-through, cost per click / thousand impressions, ROI.

    The ad ratios are only given where they mean something for the channel (never on an event).
    """
    impressions, clicks = counts.get("IMPRESSIONS"), counts.get("CLICKS")
    ratios = {
        "ctr": _percent(_ratio(clicks, impressions)),
        "cpc": _money(_ratio(cost, clicks)) if cost else None,
        "cpm": _money(_ratio(cost * 1000, impressions)) if cost else None,
    }
    fits = {key: value if ad_ratio_fits_channel(key.upper(), channel_code) else None for key, value in ratios.items()}
    return {**fits, "roi_percent": roi_percent(cost, revenue)}


# --- What each channel can record (proposed, to confirm with Marketing) ---
# Ad measures (impressions, clicks, CTR, CPM…) mean nothing for a trade show, and booth visitors and follow-ups
# mean nothing for a search ad. Only "Other" is open to everything.

# Results a channel can record, in the order the results form shows them
_SALES = ["LEADS", "MQLS", "SQLS"]
_WON = ["CUSTOMERS", "PIPELINE", "REVENUE"]
RESULT_METRICS_BY_CHANNEL: dict[str, list[str]] = {
    "PAID_SEARCH": ["IMPRESSIONS", "CLICKS", "CONVERSIONS", *_SALES, "TRIALS", *_WON],
    "PAID_SOCIAL": ["IMPRESSIONS", "CLICKS", "ENGAGEMENTS", "CONVERSIONS", *_SALES, "TRIALS", *_WON],
    "ORGANIC_SOCIAL": ["IMPRESSIONS", "CLICKS", "ENGAGEMENTS", "VISITORS", "CONVERSIONS", *_SALES, "TRIALS", *_WON],
    "SEO": ["IMPRESSIONS", "CLICKS", "VISITORS", *_SALES, "TRIALS", *_WON],
    "CONTENT_MARKETING": ["VISITORS", "ENGAGEMENTS", *_SALES, "TRIALS", *_WON],
    "EMAIL_MARKETING": ["CLICKS", "VISITORS", *_SALES, "TRIALS", "DEMOS", *_WON],
    "MARKETING_AUTOMATION": ["CLICKS", "VISITORS", *_SALES, "TRIALS", "DEMOS", *_WON],
    "EVENTS": ["VISITORS", "LEADS", "FOLLOW_UPS", "DEMOS", "MQLS", "SQLS", *_WON],
    "PARTNERSHIPS": ["VISITORS", *_SALES, "DEMOS", *_WON],
    "AFFILIATES": ["CLICKS", "VISITORS", "CONVERSIONS", *_SALES, "TRIALS", *_WON],
    "INFLUENCER_MARKETING": ["IMPRESSIONS", "CLICKS", "VISITORS", "ENGAGEMENTS", "CONVERSIONS", *_SALES, "TRIALS", *_WON],
    "PR_COMMUNICATIONS": ["IMPRESSIONS", "VISITORS", "ENGAGEMENTS", *_SALES, *_WON],
    "OUTBOUND_MARKETING": ["ENGAGEMENTS", *_SALES, "DEMOS", *_WON],
    "PRODUCT_LED_GROWTH": ["VISITORS", "ENGAGEMENTS", "TRIALS", "CONVERSIONS", *_SALES, *_WON],
    "MARKETPLACE": ["IMPRESSIONS", "CLICKS", "VISITORS", "CONVERSIONS", *_SALES, "TRIALS", *_WON],
}

# Ad ratios (click-through, cost per click, cost per thousand impressions) only mean something where the channel
# records the clicks or impressions and, for CPC and CPM, pays for them. Channels not listed (Other) get all three.
AD_RATIOS_BY_CHANNEL: dict[str, set[str]] = {
    "PAID_SEARCH": {"CTR", "CPC", "CPM"},
    "PAID_SOCIAL": {"CTR", "CPC", "CPM"},
    "INFLUENCER_MARKETING": {"CTR", "CPC", "CPM"},
    # Organic posts aren't paid per click or impression
    "ORGANIC_SOCIAL": {"CTR"},
    "SEO": {"CTR"},
    "MARKETPLACE": {"CTR"},
    "AFFILIATES": {"CPC"},
    **{channel: set() for channel in (
        "CONTENT_MARKETING", "EMAIL_MARKETING", "MARKETING_AUTOMATION", "EVENTS", "PARTNERSHIPS",
        "PR_COMMUNICATIONS", "OUTBOUND_MARKETING", "PRODUCT_LED_GROWTH",
    )},
}
# The results each ad ratio is worked out from
AD_RATIO_NEEDS: dict[str, set[str]] = {"CTR": {"IMPRESSIONS", "CLICKS"}, "CPC": {"CLICKS"}, "CPM": {"IMPRESSIONS"}}


def ad_ratio_fits_channel(ratio: str, channel_code: str | None) -> bool:
    allowed = AD_RATIOS_BY_CHANNEL.get(channel_code or "")
    return allowed is None or ratio in allowed


def result_fits_channel(metric_code: str, channel_code: str | None) -> bool:
    allowed = RESULT_METRICS_BY_CHANNEL.get(channel_code or "")
    return allowed is None or metric_code in allowed


# --- The suggested rating ---

def matures_on(start: date | None, end: date | None, maturity_days: int) -> date | None:
    """When the rating becomes final: maturity_days after the initiative ends (or starts, without an end date)."""
    anchor = end or start
    return anchor + timedelta(days=maturity_days) if anchor else None


def _result(code: str, reason: str) -> dict:
    return {"code": code, "name": RATING_NAMES[code], "reason": reason}


def verdict_stage(start: date | None, matures: date | None, today: date) -> str:
    """UPCOMING before it starts, EARLY until the maturity window ends, then FINAL."""
    if start and today < start:
        return "UPCOMING"
    if matures and today < matures:
        return "EARLY"
    return "FINAL"


def suggest_rating(*, roi: float | None, revenue_recorded: bool) -> dict:
    """The final verdict from the return, as the Events Overview judges events: no KPI or target needed.

    High when it made money (ROI above 0); Low when it broke even or lost money (breaking even is not a success);
    Test while there's no revenue recorded, or no cost to work the return out from.
    """
    if not revenue_recorded:
        return _result("TEST", "No revenue recorded yet, so there's no verdict. Add the closed revenue under Results.")
    if roi is None:
        return _result("TEST", "No costs recorded yet, so the return can't be worked out.")
    if roi <= 0:
        verdict = "broke even" if roi == 0 else "lost money"
        return _result("LOW", f"It {verdict} (ROI {roi:g}%), so it's not a success.")
    return _result("HIGH", f"It made money: ROI {roi:g}%.")


def money_outcome(cost: float, revenue: float | None, roi: float | None) -> str | None:
    """Whether the money came back: PROFIT, BROKE_EVEN, LOST_MONEY, or NO_REVENUE yet. The page words it."""
    if not cost:
        return None
    if revenue is None or roi is None:
        return "NO_REVENUE"
    if roi > 0:
        return "PROFIT"
    return "BROKE_EVEN" if roi == 0 else "LOST_MONEY"
