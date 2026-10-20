"""Made-up Microsoft Advertising accounts, campaigns and weekly results for MICROSOFT_ADS_MODE=mock.

Like services/google_ads_mock_data.py: everything is worked out from the account id (and each campaign's id and
week), never from the clock or a random seed, so every run gives the same campaigns and the same figures for a past
week. The current week grows with the days gone by. Bing gets less traffic than Google, so the volumes are lower.
"""
import hashlib
import random
from datetime import date, timedelta
from decimal import Decimal

# What connecting in mock mode finds: two ad accounts under one customer (Microsoft has no manager accounts to skip)
MOCK_ACCOUNTS = [
    {
        "account_id": "187452031",
        "account_name": "DeskFlex Microsoft Search - North America",
        "account_number": "F187W2QK",
        "customer_id": "251874033",
        "currency": "USD",
        "time_zone": "EasternTimeUSCanada",
        "is_manager": False,
    },
    {
        "account_id": "187452266",
        "account_name": "DeskFlex Microsoft Audience Network",
        "account_number": "F187W9TX",
        "customer_id": "251874033",
        "currency": "USD",
        "time_zone": "CentralTimeUSCanada",
        "is_manager": False,
    },
]

_BRANDS = ("DeskFlex", "Othership", "WorkAware", "ZenaTech", "Jadian", "TillerStack")
# Theme and campaign type
_THEMES = (
    ("Brand Terms", "SEARCH"), ("Desk Booking", "SEARCH"), ("Meeting Rooms", "SEARCH"), ("Competitor Terms", "SEARCH"),
    ("Visitor Management", "SEARCH"), ("Website Pages", "DYNAMIC_SEARCH_ADS"), ("Remarketing", "AUDIENCE"),
    ("Hybrid Office", "AUDIENCE"), ("Hardware Kits", "SHOPPING"), ("Workplace Platform", "PERFORMANCE_MAX"),
)
_TYPE_LABELS = {
    "SEARCH": "Search", "DYNAMIC_SEARCH_ADS": "Dynamic Search", "AUDIENCE": "Audience", "SHOPPING": "Shopping",
    "PERFORMANCE_MAX": "Performance Max",
}
# Weekly impressions, CTR, CPC and conversion rate ranges per campaign type (about a fifth of Google's traffic)
_RATES = {
    "SEARCH": {"impressions": (500, 2_400), "ctr": (0.03, 0.07), "cpc": (1.6, 4.5), "cvr": (0.04, 0.09)},
    "DYNAMIC_SEARCH_ADS": {"impressions": (400, 1_800), "ctr": (0.025, 0.055), "cpc": (1.1, 3.2), "cvr": (0.03, 0.07)},
    "AUDIENCE": {"impressions": (8_000, 30_000), "ctr": (0.003, 0.009), "cpc": (0.4, 1.2), "cvr": (0.005, 0.02)},
    "SHOPPING": {"impressions": (1_500, 6_000), "ctr": (0.008, 0.02), "cpc": (0.5, 1.4), "cvr": (0.01, 0.03)},
    "PERFORMANCE_MAX": {"impressions": (4_000, 14_000), "ctr": (0.008, 0.02), "cpc": (0.7, 2.2), "cvr": (0.02, 0.05)},
}
_FIRST_START = date(2026, 1, 12)


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
    """A stable, Microsoft-like campaign id (9 digits) for the account's n-th mock campaign."""
    digest = hashlib.sha256(f"mock-msads-campaign:{account_id}:{index}".encode()).hexdigest()
    return str(400_000_000 + int(digest[:12], 16) % 500_000_000)


def mock_campaigns(account: dict) -> list[dict]:
    """The account's 4 to 7 campaigns, without metrics (same shape as microsoft_ads_client.parse_campaign)."""
    account_id = account["account_id"]
    rng = _rng("mock-msads-account", account_id)
    brand = _brand_for(account, rng)
    themes = rng.sample(_THEMES, rng.randint(4, 7))
    # Brand terms first: every account has them
    themes.sort(key=lambda t: t[0] != "Brand Terms")
    if themes[0][0] != "Brand Terms":
        themes[0] = ("Brand Terms", "SEARCH")
    campaigns = []
    for index, (theme, kind) in enumerate(themes):
        crng = _rng("mock-msads-campaign", account_id, index)
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
    rng = _rng("mock-msads-week", campaign["campaign_id"], week.isoformat())
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
    microsoft_ads_client.fetch_campaigns. A week only has figures for the days the campaign was running."""
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
