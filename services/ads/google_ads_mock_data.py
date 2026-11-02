"""Made-up Google Ads accounts, campaigns and weekly results for GOOGLE_ADS_MODE=mock.

Everything is worked out from the account id (and each campaign's id and week), never from the clock or a random
seed, so every run gives the same campaigns and the same figures for a past week. The current week grows with the
days gone by, like a real account. Campaign dates are fixed dates in 2026, so they don't move from one day to the next.
"""
import hashlib
import random
from datetime import date, timedelta
from decimal import Decimal

# What connecting in mock mode finds: a manager account (never synced) and one client account
MOCK_ACCOUNTS = [
    {
        "account_id": "987-654-3210",
        "account_name": "ZenaTech Group Master Google Ads (MCC)",
        "currency": "USD",
        "time_zone": "America/New_York",
        "is_manager": True,
    },
    {
        "account_id": "412-880-9921",
        "account_name": "DeskFlex US & Canada Search Ads",
        "currency": "USD",
        "time_zone": "America/Chicago",
        "is_manager": False,
    },
]

_BRANDS = ("DeskFlex", "Othership", "WorkAware", "ZenaTech", "Jadian", "TillerStack")
# Theme, campaign type (advertising channel), objective hint
_THEMES = (
    ("Brand Terms", "SEARCH"), ("Competitor Terms", "SEARCH"), ("Hot Desking", "SEARCH"), ("Room Booking", "SEARCH"),
    ("Free Trial", "SEARCH"), ("Workplace Analytics", "SEARCH"), ("Remarketing", "DISPLAY"),
    ("Hybrid Work", "PERFORMANCE_MAX"), ("Product Demo", "VIDEO"), ("Office Visitors", "DISPLAY"),
)
_TYPE_LABELS = {"SEARCH": "Search", "DISPLAY": "Display", "PERFORMANCE_MAX": "Performance Max", "VIDEO": "YouTube"}
# Weekly impressions, CTR, CPC and conversion rate ranges per campaign type
_RATES = {
    "SEARCH": {"impressions": (2_500, 11_000), "ctr": (0.035, 0.075), "cpc": (2.4, 6.2), "cvr": (0.04, 0.09)},
    "DISPLAY": {"impressions": (35_000, 120_000), "ctr": (0.003, 0.008), "cpc": (0.45, 1.4), "cvr": (0.006, 0.02)},
    "PERFORMANCE_MAX": {"impressions": (18_000, 60_000), "ctr": (0.01, 0.025), "cpc": (0.9, 2.8), "cvr": (0.03, 0.06)},
    "VIDEO": {"impressions": (25_000, 90_000), "ctr": (0.004, 0.012), "cpc": (0.25, 0.9), "cvr": (0.005, 0.015)},
}
_FIRST_START = date(2026, 1, 5)


def _rng(*parts) -> random.Random:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _brand_for(account: dict, rng: random.Random) -> str:
    name = (account.get("account_name") or "").lower()
    for brand in _BRANDS:
        if brand.lower() in name:
            return brand
    return rng.choice(_BRANDS)


def campaign_id_for(account_id: str, index: int) -> str:
    """A stable, Google-like campaign id (11 digits) for the account's n-th mock campaign."""
    digest = hashlib.sha256(f"mock-campaign:{account_id}:{index}".encode()).hexdigest()
    return str(10_000_000_000 + int(digest[:12], 16) % 90_000_000_000)


def mock_campaigns(account: dict) -> list[dict]:
    """The account's 5 to 8 campaigns, without metrics (same shape as google_ads_client.parse_campaign_row)."""
    account_id = account["account_id"]
    rng = _rng("mock-account", account_id)
    brand = _brand_for(account, rng)
    count = rng.randint(5, 8)
    themes = rng.sample(_THEMES, count)
    # Brand terms first: every account has them
    themes.sort(key=lambda t: t[0] != "Brand Terms")
    if themes[0][0] != "Brand Terms":
        themes[0] = ("Brand Terms", "SEARCH")
    campaigns = []
    for index, (theme, kind) in enumerate(themes):
        crng = _rng("mock-campaign", account_id, index)
        start = _FIRST_START + timedelta(days=crng.randint(0, 170))
        status, end = "ENABLED", None
        # About one in four is paused, and one in eight has ended (never the brand campaign)
        roll = crng.random()
        if index > 0 and roll < 0.25:
            status = "PAUSED"
        elif index > 0 and roll < 0.38:
            end = start + timedelta(days=crng.randint(45, 100))
        # The campaign's usual week; each week then varies a little around it
        rates = _RATES[kind]
        base = {key: crng.uniform(*rates[key]) for key in ("impressions", "ctr", "cpc", "cvr")}
        weekly_spend = base["impressions"] * base["ctr"] * base["cpc"]
        daily = Decimal(max(5, round(weekly_spend / 7 * 1.4 / 5) * 5))
        campaigns.append({
            "campaign_id": campaign_id_for(account_id, index),
            "name": f"{brand} - {theme} - {_TYPE_LABELS[kind]}",
            "status": status,
            "channel_type": kind,
            "start_date": start,
            "end_date": end,
            "daily_budget": daily.quantize(Decimal("0.01")),
            "total_budget": None,
            "weeks": {},
            # When a paused campaign stopped spending (mock only)
            "_paused_from": start + timedelta(days=crng.randint(35, 110)) if status == "PAUSED" else None,
            "_base": base,
        })
    return campaigns


def _week_figures(campaign: dict, base: dict, week: date, days: int) -> dict:
    """One week's figures: `days` of the week counted (7, or fewer for the current week)."""
    rng = _rng("mock-week", campaign["campaign_id"], week.isoformat())
    impressions = int(base["impressions"] * rng.uniform(0.8, 1.2) * days / 7)
    clicks = int(impressions * base["ctr"] * rng.uniform(0.85, 1.15))
    spend = Decimal(str(round(clicks * base["cpc"] * rng.uniform(0.9, 1.1), 2)))
    conversions = Decimal(str(round(clicks * base["cvr"] * rng.uniform(0.7, 1.3), 1)))
    return {
        "spend": spend.quantize(Decimal("0.01")),
        "impressions": impressions,
        "clicks": clicks,
        "conversions": conversions.quantize(Decimal("0.01")),
    }


def mock_account_campaigns(account: dict, date_from: date, date_to: date) -> list[dict]:
    """The account's campaigns with weekly figures for the weeks from date_from (a Monday) up to date_to, like
    google_ads_client.fetch_campaigns. A week only has figures for the days the campaign was running."""
    campaigns = mock_campaigns(account)
    for campaign in campaigns:
        base = campaign.pop("_base")
        last_day = min(d for d in (date_to, campaign["end_date"], campaign.pop("_paused_from")) if d is not None)
        week = date_from - timedelta(days=date_from.weekday())
        while week <= date_to:
            first = max(week, campaign["start_date"], date_from)
            last = min(week + timedelta(days=6), last_day)
            if last >= first:
                figures = _week_figures(campaign, base, week, (last - first).days + 1)
                if figures["impressions"] > 0:
                    campaign["weeks"][week] = figures
            week += timedelta(days=7)
    return campaigns
