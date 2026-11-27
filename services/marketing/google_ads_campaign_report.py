"""Campaign metadata and date-filtered performance for the Google reader report."""
from datetime import date
from decimal import Decimal

from models.marketing.google_reporting_model import GoogleAdsCampaign

CAMPAIGN_FIELDS = (
    "campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
    "customer.currency_code, customer.time_zone"
)
CAMPAIGNS_QUERY = f"SELECT {CAMPAIGN_FIELDS} FROM campaign ORDER BY campaign.id"


def campaign_metrics_query(start: date, end: date) -> str:
    return (
        f"SELECT {CAMPAIGN_FIELDS}, metrics.cost_micros, metrics.clicks, metrics.impressions, "
        "metrics.conversions FROM campaign "
        f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}' ORDER BY campaign.id"
    )


def stream_rows(batches):
    if isinstance(batches, dict):
        batches = [batches]
    for batch in batches:
        yield from batch.get("results", [])


def parse_campaigns(metadata_batches, metric_batches) -> list[GoogleAdsCampaign]:
    campaigns, totals = {}, {}
    for row in stream_rows(metadata_batches):
        campaign = row["campaign"]
        key = str(campaign["id"])
        campaigns[key] = GoogleAdsCampaign(
            campaign_id=key, campaign_name=campaign.get("name") or f"Campaign {key}",
            status=campaign.get("status", "UNKNOWN"), channel=campaign.get("advertisingChannelType", "UNKNOWN"),
        )
    for row in stream_rows(metric_batches):
        campaign, values = row["campaign"], row.get("metrics", {})
        key = str(campaign["id"])
        # A campaign may be removed or created between the two read-only queries.
        if key not in campaigns:
            campaigns[key] = GoogleAdsCampaign(
                campaign_id=key, campaign_name=campaign.get("name") or f"Campaign {key}",
                status=campaign.get("status", "UNKNOWN"), channel=campaign.get("advertisingChannelType", "UNKNOWN"),
            )
        metrics = totals.setdefault(key, [0, 0, 0, Decimal(0)])
        metrics[0] += int(values.get("costMicros", 0))
        metrics[1] += int(values.get("clicks", 0))
        metrics[2] += int(values.get("impressions", 0))
        metrics[3] += Decimal(str(values.get("conversions", 0)))
    for key, (micros, clicks, impressions, conversions) in totals.items():
        spend = Decimal(micros) / 1_000_000
        campaigns[key] = campaigns[key].model_copy(update={
            "spend": float(spend), "clicks": clicks, "impressions": impressions, "conversions": float(conversions),
            "ctr": clicks / impressions * 100 if impressions else None,
            "cpc": float(spend / clicks) if clicks else None,
            "cost_per_conversion": float(spend / conversions) if conversions else None,
        })
    return sorted(campaigns.values(), key=lambda c: (-c.spend, c.campaign_name.casefold(), c.campaign_id))
