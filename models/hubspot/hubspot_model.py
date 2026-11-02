"""HubSpot campaigns: the synced campaigns, and the sync's status."""
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, computed_field


class HubSpotCampaign(BaseModel):
    id: str
    name: str
    # HubSpot's own status: planned, in_progress, active, paused, completed; null when not set there
    status: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    # HubSpot's own currency; when it has none, that of the linked event's country, else the linked initiative's
    currency: str | None = None
    # Where the currency came from: HUBSPOT, COUNTRY or INITIATIVE; null when unknown
    currency_source: str | None = None
    budget_total: float | None = None
    spend_total: float | None = None
    owner_name: str | None = None
    utm: str | None = None
    notes: str | None = None
    audience: str | None = None
    sessions: int | None = None
    new_contacts_first_touch: int | None = None
    new_contacts_last_touch: int | None = None
    influenced_contacts: int | None = None
    # By asset type: a list of {"id", "name"}, or {"count": n} for meetings
    assets: dict[str, Any] = {}
    # People on its contact lists, each counted once; null without contact lists
    list_contacts: int | None = None
    # Contacts who reached each stage (lead, mql, sql, opportunity, customer), by "first_touch", "last_touch" and
    # among the list members ("lists"); empty when the funnel couldn't be read
    funnel: dict[str, dict[str, int]] = {}
    hubspot_created_at: datetime | None = None
    hubspot_updated_at: datetime | None = None
    synced_at: datetime
    # The initiative linked to this campaign, if any
    initiative_id: str | None = None
    initiative_name: str | None = None

    # Budget less spend, in the campaign's currency; null without a budget (negative when over budget)
    @computed_field
    @property
    def remaining_budget(self) -> float | None:
        if self.budget_total is None:
            return None
        return round(self.budget_total - (self.spend_total or 0), 2)

    # Spend as a percent of budget; null without a budget (or with a zero one)
    @computed_field
    @property
    def budget_used_pct(self) -> float | None:
        if not self.budget_total:
            return None
        return round((self.spend_total or 0) / self.budget_total * 100, 1)

    # Spend per new contact (first touch), in the campaign's currency; null without spend or without new contacts
    @computed_field
    @property
    def cost_per_contact(self) -> float | None:
        if not self.spend_total or not self.new_contacts_first_touch:
            return None
        return round(self.spend_total / self.new_contacts_first_touch, 2)

    # Money spent but HubSpot reports no new contact from it; false when the contacts weren't read
    @computed_field
    @property
    def spend_without_contacts(self) -> bool:
        return bool(self.spend_total) and self.spend_total > 0 and self.new_contacts_first_touch == 0


class HubSpotSocialPost(BaseModel):
    id: str
    # The connected account ("FacebookPage:123"), its platform (facebook, linkedin, instagram, x, youtube, tiktok) and name
    channel_key: str
    platform: str
    channel_name: str | None = None
    # HubSpot's own status: SUCCESS (published), DRAFT, CANCELED, SUCCESS_ARCHIVE, ERROR_ARCHIVE...
    status: str | None = None
    message: str | None = None
    media_type: str | None = None
    # The HubSpot campaign it belongs to, if any
    campaign_id: str | None = None
    campaign_name: str | None = None
    # Counters to date
    clicks: int | None = None
    likes: int | None = None
    replies: int | None = None
    shares: int | None = None
    interactions: int | None = None
    scheduled_at: datetime | None = None
    finished_at: datetime | None = None
    hubspot_created_at: datetime | None = None
    synced_at: datetime


class HubSpotSyncSummary(BaseModel):
    campaigns: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0
    # Asset lists HubSpot wouldn't give (campaigns x types); those campaigns show what could be read
    unreadable_asset_types: int = 0
    # Campaigns with no budget set in HubSpot
    without_budget: int = 0
    # Initiatives added for campaigns that had none, and the sync's own initiatives brought in step with HubSpot
    initiatives_created: int = 0
    initiatives_updated: int = 0
    # Why the funnel (contacts and lists) couldn't be read this time; the campaigns synced without it
    funnel_error: str | None = None
    # Social posts read this time, and why they couldn't be (the stored posts were left as they were)
    social_posts: int = 0
    social_error: str | None = None


class HubSpotSyncStarted(BaseModel):
    started: bool = True


class HubSpotSyncStatus(BaseModel):
    configured: bool
    enabled: bool
    interval_minutes: int
    # A sync is running on this server now
    running: bool
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    last_run_summary: HubSpotSyncSummary | None = None
