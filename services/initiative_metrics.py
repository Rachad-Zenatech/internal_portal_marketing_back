"""Turns an initiative's costs and results into the cost-per-outcome ladder, ROI and a suggested rating.

Pure functions, no database: everything here is calculated on request and never stored.

The CEO's example is the yardstick: a $20,000 trade show with 200 leads, 20 demos and 2 customers costs
$100 per lead, $1,000 per demo and $10,000 per customer. If those customers bring in $20,000, the show only
broke even (ROI 0%), and breaking even does not count as a success.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

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

# How close to the target counts as Medium rather than Low (20% either side, depending on direction)
MEDIUM_BAND = 0.20

# Once revenue is recorded, an initiative that broke even or lost money is Low whatever its KPI says
BREAK_EVEN_IS_LOW = True

# The decision the system suggests for each rating; Marketing makes the actual call
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
    fits = {key: value if kpi_fits_channel(key.upper(), channel_code) else None for key, value in ratios.items()}
    return {**fits, "roi_percent": roi_percent(cost, revenue)}


# --- Primary KPIs: how to read each one's actual value, and which direction is better ---

@dataclass(frozen=True)
class KpiDefinition:
    # "lower" for costs (CPL, CAC…), "higher" for things you want more of
    direction: str
    # How the value is shown and targeted: MONEY (in the initiative's currency), PERCENT or COUNT
    unit: str
    actual: Callable[[float, dict[str, float], float | None, float | None], float | None]


def _cost_per(metric: str) -> KpiDefinition:
    return KpiDefinition("lower", "MONEY", lambda cost, counts, revenue, pipeline: _money(_ratio(cost, counts.get(metric))) if cost else None)


KPI_DEFINITIONS: dict[str, KpiDefinition] = {
    "IMPRESSIONS": KpiDefinition("higher", "COUNT", lambda cost, counts, revenue, pipeline: counts.get("IMPRESSIONS")),
    "CTR": KpiDefinition("higher", "PERCENT", lambda cost, counts, revenue, pipeline: _percent(_ratio(counts.get("CLICKS"), counts.get("IMPRESSIONS")))),
    "CPC": _cost_per("CLICKS"),
    "CPM": KpiDefinition("lower", "MONEY", lambda cost, counts, revenue, pipeline: _money(_ratio(cost * 1000, counts.get("IMPRESSIONS"))) if cost else None),
    "CP_VISITOR": _cost_per("VISITORS"),
    "CPL": _cost_per("LEADS"),
    "CP_FOLLOW_UP": _cost_per("FOLLOW_UPS"),
    "CP_MQL": _cost_per("MQLS"),
    "CP_SQL": _cost_per("SQLS"),
    "CP_TRIAL": _cost_per("TRIALS"),
    "CP_DEMO": _cost_per("DEMOS"),
    "CAC": _cost_per("CUSTOMERS"),
    "PIPELINE_GENERATED": KpiDefinition("higher", "MONEY", lambda cost, counts, revenue, pipeline: pipeline),
    "REVENUE": KpiDefinition("higher", "MONEY", lambda cost, counts, revenue, pipeline: revenue),
    "ROI": KpiDefinition("higher", "PERCENT", lambda cost, counts, revenue, pipeline: roi_percent(cost, revenue)),
}
# Not calculated yet: Conversion Rate needs Marketing to say which stage to which; LTV/CAC needs a lifetime value source
UNSUPPORTED_KPIS = {"CONVERSION_RATE", "LTV_CAC"}

# --- What each channel can record and be judged on (proposed, to confirm with Marketing) ---
# Ad measures (impressions, clicks, CTR, CPM…) mean nothing for a trade show, and booth visitors and follow-ups
# mean nothing for a search ad. Only "Other" is open to everything.

# Results a channel can record, in the order the results form shows them
_SALES = ["LEADS", "MQLS", "SQLS"]
_WON = ["CUSTOMERS", "PIPELINE", "REVENUE"]
RESULT_METRICS_BY_CHANNEL: dict[str, list[str]] = {
    "PAID_SEARCH": ["IMPRESSIONS", "CLICKS", "CONVERSIONS", *_SALES, "TRIALS", *_WON],
    "PAID_SOCIAL": ["IMPRESSIONS", "CLICKS", "ENGAGEMENTS", "CONVERSIONS", *_SALES, "TRIALS", *_WON],
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

# Primary KPIs a channel can be judged on. Each one only uses results the channel can record (a test checks this);
# CPC and CPM are left to channels that actually pay per click or per thousand impressions.
_COST_AND_RETURN = ["CPL", "CP_MQL", "CP_SQL", "CAC", "PIPELINE_GENERATED", "REVENUE", "ROI"]
_ADS = ["IMPRESSIONS", "CTR", "CPC", "CPM"]
KPI_CHOICES_BY_CHANNEL: dict[str, list[str]] = {
    "PAID_SEARCH": [*_ADS, *_COST_AND_RETURN, "CP_TRIAL", "CONVERSION_RATE", "LTV_CAC"],
    "PAID_SOCIAL": [*_ADS, *_COST_AND_RETURN, "CP_TRIAL", "CONVERSION_RATE", "LTV_CAC"],
    "SEO": ["IMPRESSIONS", "CTR", "CP_VISITOR", *_COST_AND_RETURN, "CP_TRIAL"],
    "CONTENT_MARKETING": ["CP_VISITOR", *_COST_AND_RETURN, "CP_TRIAL"],
    "EMAIL_MARKETING": ["CP_VISITOR", *_COST_AND_RETURN, "CP_TRIAL", "CP_DEMO"],
    "MARKETING_AUTOMATION": ["CP_VISITOR", *_COST_AND_RETURN, "CP_TRIAL", "CP_DEMO"],
    "EVENTS": ["CP_VISITOR", "CPL", "CP_FOLLOW_UP", "CP_MQL", "CP_SQL", "CP_DEMO", "CAC", "PIPELINE_GENERATED", "REVENUE", "ROI"],
    "PARTNERSHIPS": ["CP_VISITOR", *_COST_AND_RETURN, "CP_DEMO"],
    "AFFILIATES": ["CPC", "CP_VISITOR", *_COST_AND_RETURN, "CP_TRIAL", "CONVERSION_RATE"],
    "INFLUENCER_MARKETING": [*_ADS, "CP_VISITOR", *_COST_AND_RETURN, "CP_TRIAL", "CONVERSION_RATE"],
    "PR_COMMUNICATIONS": ["IMPRESSIONS", "CP_VISITOR", *_COST_AND_RETURN],
    "OUTBOUND_MARKETING": [*_COST_AND_RETURN, "CP_DEMO"],
    "PRODUCT_LED_GROWTH": ["CP_VISITOR", *_COST_AND_RETURN, "CP_TRIAL", "CONVERSION_RATE", "LTV_CAC"],
    "MARKETPLACE": ["IMPRESSIONS", "CTR", "CP_VISITOR", *_COST_AND_RETURN, "CP_TRIAL", "CONVERSION_RATE"],
}

# The results each calculated KPI is worked out from
KPI_NEEDS: dict[str, set[str]] = {
    "IMPRESSIONS": {"IMPRESSIONS"}, "CTR": {"IMPRESSIONS", "CLICKS"}, "CPC": {"CLICKS"}, "CPM": {"IMPRESSIONS"},
    "CP_VISITOR": {"VISITORS"}, "CPL": {"LEADS"}, "CP_FOLLOW_UP": {"FOLLOW_UPS"}, "CP_MQL": {"MQLS"},
    "CP_SQL": {"SQLS"}, "CP_TRIAL": {"TRIALS"}, "CP_DEMO": {"DEMOS"}, "CAC": {"CUSTOMERS"},
    "PIPELINE_GENERATED": {"PIPELINE"}, "REVENUE": {"REVENUE"}, "ROI": {"REVENUE"},
}


def kpi_fits_channel(kpi_code: str | None, channel_code: str | None) -> bool:
    allowed = KPI_CHOICES_BY_CHANNEL.get(channel_code or "")
    return kpi_code is None or allowed is None or kpi_code in allowed


def result_fits_channel(metric_code: str, channel_code: str | None) -> bool:
    allowed = RESULT_METRICS_BY_CHANNEL.get(channel_code or "")
    return allowed is None or metric_code in allowed


def kpi_actual(code: str | None, cost: float, counts: dict[str, float], revenue: float | None, pipeline: float | None) -> float | None:
    definition = KPI_DEFINITIONS.get(code or "")
    return definition.actual(cost, counts, revenue, pipeline) if definition else None


# --- The suggested rating ---

def matures_on(start: date | None, end: date | None, maturity_days: int) -> date | None:
    """When the rating becomes final: maturity_days after the initiative ends (or starts, without an end date)."""
    anchor = end or start
    return anchor + timedelta(days=maturity_days) if anchor else None


def _format_target(value: float, unit: str, currency: str) -> str:
    if unit == "PERCENT":
        return f"{value:g}%"
    if unit == "MONEY":
        return f"{currency} {value:,.2f}".replace(".00", "")
    return f"{value:,.0f}"


def _result(code: str, reason: str, gap: float | None = None) -> dict:
    return {"code": code, "name": RATING_NAMES[code], "reason": reason, "gap_percent": gap}


def _kpi_rating(kpi_code: str | None, kpi_name: str | None, actual: float | None, target: float | None, currency: str) -> dict:
    """The primary KPI against its target: High when met, Medium within 20%, Low beyond — or Test without what it needs."""
    if not kpi_code:
        return _result("TEST", "Choose a primary KPI to get a rating.")
    if kpi_code in UNSUPPORTED_KPIS or kpi_code not in KPI_DEFINITIONS:
        return _result("TEST", f"{kpi_name or kpi_code} isn't calculated yet, so there's no rating.")
    if target is None:
        return _result("TEST", f"Set a target for {kpi_name or kpi_code} to get a rating.")
    if actual is None:
        return _result("TEST", f"Not enough results yet to work out {kpi_name or kpi_code}.")

    definition = KPI_DEFINITIONS[kpi_code]
    shown_actual = _format_target(actual, definition.unit, currency)
    shown_target = _format_target(target, definition.unit, currency)
    if definition.direction == "lower":
        met = actual <= target
        gap = (actual - target) / target if target else (0.0 if actual <= 0 else float("inf"))
    else:
        met = actual >= target
        gap = (target - actual) / target if target else 0.0
    gap_percent = None if gap == float("inf") else round(abs(gap) * 100, 1)

    if met:
        return _result("HIGH", f"{kpi_name} {shown_actual} met the target of {shown_target}.", gap_percent)
    if gap <= MEDIUM_BAND + 1e-9:
        return _result("MEDIUM", f"{kpi_name} {shown_actual} is within {MEDIUM_BAND:.0%} of the target of {shown_target} "
                                 f"(missed by {gap_percent:g}%).", gap_percent)
    missed = f"missed by {gap_percent:g}%" if gap_percent is not None else "missed it"
    return _result("LOW", f"{kpi_name} {shown_actual} {missed} against the target of {shown_target}.", gap_percent)


def verdict_stage(start: date | None, matures: date | None, today: date) -> str:
    """UPCOMING before it starts, EARLY until the maturity window ends, then FINAL."""
    if start and today < start:
        return "UPCOMING"
    if matures and today < matures:
        return "EARLY"
    return "FINAL"


def early_read(*, kpi_code: str | None, kpi_name: str | None, actual: float | None, target: float | None, currency: str) -> dict:
    """A provisional rating on the leading KPI (cost per lead, demo…), available days after an event — revenue comes later."""
    return _kpi_rating(kpi_code, kpi_name, actual, target, currency)


def suggest_rating(
    *,
    kpi_code: str | None,
    kpi_name: str | None,
    actual: float | None,
    target: float | None,
    matures: date | None,
    maturity_days: int,
    today: date,
    roi: float | None,
    revenue_recorded: bool,
    currency: str,
) -> dict:
    """The final High / Medium / Low / Test, with a one-sentence reason a person can act on.

    Once revenue is in, the return leads: breaking even or losing money is Low; making money is High when the KPI
    was also met (or has no target), Medium when it made money but cost more per outcome than planned.
    Without revenue (e.g. an awareness push) it's judged on the KPI alone.
    """
    if matures and today < matures:
        days = (matures - today).days
        final_on = f"{matures.day} {matures.strftime('%B %Y')}"
        return _result("TEST", f"Too early to judge — {days} {'day' if days == 1 else 'days'} left in the {maturity_days}-day window "
                               f"(final on {final_on}).")
    kpi = _kpi_rating(kpi_code, kpi_name, actual, target, currency)
    if not (revenue_recorded and roi is not None):
        return kpi
    if roi <= 0 and BREAK_EVEN_IS_LOW:
        verdict = "broke even" if roi == 0 else "lost money"
        return _result("LOW", f"It {verdict} (ROI {roi:g}%) — not a success, whatever the KPI says.")
    if kpi["code"] in ("HIGH", "TEST"):
        detail = f" {kpi['reason']}" if kpi["code"] == "HIGH" else ""
        return _result("HIGH", f"It made money: ROI {roi:g}%.{detail}", kpi["gap_percent"])
    return _result("MEDIUM", f"It made money (ROI {roi:g}%), but {kpi['reason']}", kpi["gap_percent"])


def money_outcome(cost: float, revenue: float | None, roi: float | None) -> str | None:
    """Whether the money came back: PROFIT, BROKE_EVEN, LOST_MONEY, or NO_REVENUE yet. The page words it."""
    if not cost:
        return None
    if not revenue or roi is None:
        return "NO_REVENUE"
    if roi > 0:
        return "PROFIT"
    return "BROKE_EVEN" if roi == 0 else "LOST_MONEY"
