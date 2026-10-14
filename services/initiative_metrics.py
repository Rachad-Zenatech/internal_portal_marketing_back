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


def summary_ratios(cost: float, counts: dict[str, float], revenue: float | None) -> dict:
    """Ratios that sit outside the ladder: click-through, cost per click / thousand impressions, ROI."""
    impressions, clicks = counts.get("IMPRESSIONS"), counts.get("CLICKS")
    return {
        "ctr": _percent(_ratio(clicks, impressions)),
        "cpc": _money(_ratio(cost, clicks)) if cost else None,
        "cpm": _money(_ratio(cost * 1000, impressions)) if cost else None,
        "roi_percent": roi_percent(cost, revenue),
    }


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
    """High / Medium / Low / Test with a one-sentence reason a person can act on."""

    def result(code: str, reason: str, gap: float | None = None) -> dict:
        return {"code": code, "name": RATING_NAMES[code], "reason": reason, "gap_percent": gap}

    if matures and today < matures:
        days = (matures - today).days
        final_on = f"{matures.day} {matures.strftime('%B %Y')}"
        return result("TEST", f"Too early to judge — {days} {'day' if days == 1 else 'days'} left in the {maturity_days}-day window "
                              f"(final on {final_on}).")
    if BREAK_EVEN_IS_LOW and revenue_recorded and roi is not None and roi <= 0:
        verdict = "broke even" if roi == 0 else "lost money"
        return result("LOW", f"It {verdict} (ROI {roi:g}%) — not a success, whatever the KPI says.")
    if not kpi_code:
        return result("TEST", "Choose a primary KPI to get a rating.")
    if kpi_code in UNSUPPORTED_KPIS or kpi_code not in KPI_DEFINITIONS:
        return result("TEST", f"{kpi_name or kpi_code} isn't calculated yet, so there's no rating.")
    if target is None:
        return result("TEST", f"Set a target for {kpi_name or kpi_code} to get a rating.")
    if actual is None:
        return result("TEST", f"Not enough results yet to work out {kpi_name or kpi_code}.")

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
        return result("HIGH", f"{kpi_name} {shown_actual} met the target of {shown_target}.", gap_percent)
    if gap <= MEDIUM_BAND + 1e-9:
        return result("MEDIUM", f"{kpi_name} {shown_actual} is within {MEDIUM_BAND:.0%} of the target of {shown_target} "
                                f"(missed by {gap_percent:g}%).", gap_percent)
    missed = f"missed by {gap_percent:g}%" if gap_percent is not None else "missed it"
    return result("LOW", f"{kpi_name} {shown_actual} {missed} against the target of {shown_target}.", gap_percent)


def money_outcome(cost: float, revenue: float | None, roi: float | None) -> str | None:
    """Whether the money came back: PROFIT, BROKE_EVEN, LOST_MONEY, or NO_REVENUE yet. The page words it."""
    if not cost:
        return None
    if not revenue or roi is None:
        return "NO_REVENUE"
    if roi > 0:
        return "PROFIT"
    return "BROKE_EVEN" if roi == 0 else "LOST_MONEY"
