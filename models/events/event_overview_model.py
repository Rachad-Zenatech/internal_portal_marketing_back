"""Events Overview: the Navan events with their budget, cost, travel and the HubSpot results matched to them."""
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from models.events.event_performance_model import CurrencyCostBreakdown, EventPerformance, Period


class NavanFigures(BaseModel):
    """Travel booked through Navan (money in the event's currency)."""

    navan_event_name: str | None = None
    travel_cost: float = 0
    travel_budget: float | None = None
    navan_estimate: float | None = None
    travellers: int = 0
    bookings: int = 0
    first_travel: date | None = None
    last_travel: date | None = None


class HubSpotCampaignFigures(BaseModel):
    id: str
    name: str
    status: str | None = None
    currency: str | None = None
    budget_total: float | None = None
    spend_total: float | None = None
    # The currency of spend_total and budget_total: the campaign's own, else HubSpot's account currency
    spend_currency: str | None = None
    sessions: int | None = None
    new_contacts_first_touch: int | None = None
    new_contacts_last_touch: int | None = None
    influenced_contacts: int | None = None
    list_contacts: int | None = None
    # Contacts who reached each stage, by "first_touch", "last_touch" and "lists"
    funnel: dict[str, dict[str, int]] = {}


class HubSpotMarketingEventFigures(BaseModel):
    id: str
    name: str
    event_type: str | None = None
    status: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    event_url: str | None = None
    registrants: int | None = None
    attendees: int | None = None
    cancellations: int | None = None
    no_shows: int | None = None


class EventHubSpotMatch(BaseModel):
    """CAMPAIGN: the event is a HubSpot campaign. AUTO: found from the names. MANUAL: picked by someone. NONE: nothing
    matches (found, or picked as none)."""

    method: Literal["CAMPAIGN", "AUTO", "MANUAL", "NONE"]
    # False when the lack of a match was picked by hand, so the name match isn't tried again
    picked_none: bool = False
    score: float | None = None
    campaign: HubSpotCampaignFigures | None = None
    marketing_event: HubSpotMarketingEventFigures | None = None


class EventTurnout(BaseModel):
    """The results HubSpot has for the event; null where there's no match or HubSpot doesn't know."""

    registrants: int | None = None
    attended: int | None = None
    no_shows: int | None = None
    # The event's leads: the contacts on its campaign's contact lists (CAMPAIGN_LISTS), else on a list whose name
    # matches the event (NAMED_LIST, with its name), else the contacts the campaign is credited with as a first touch
    leads: int | None = None
    leads_basis: Literal["CAMPAIGN_LISTS", "NAMED_LIST", "FIRST_TOUCH", "TYPED"] | None = None
    leads_list_name: str | None = None
    # Contacts HubSpot credits to the event's campaign as a first touch, and those it influenced
    new_contacts: int | None = None
    influenced_contacts: int | None = None
    sessions: int | None = None
    # The funnel among the leads (lists: contacts at each stage; first touch: those credited to the campaign)
    mqls: int | None = None
    sqls: int | None = None
    opportunities: int | None = None
    customers: int | None = None
    # The figures above (attended, leads, mqls, customers) that HubSpot had nothing for and a typed-in result fills
    typed_fields: list[str] = []


class NavanTrip(BaseModel):
    """A Navan trip joined to a HubSpot event (AUTO: by name, MANUAL: joined by hand)."""

    id: UUID
    name: str
    method: Literal["AUTO", "MANUAL"]


class EventOverviewRow(EventPerformance):
    # HUBSPOT when the cost or the budget is the HubSpot campaign's (the portal has none of its own for the event)
    cost_source: Literal["HUBSPOT"] | None = None
    budget_source: Literal["HUBSPOT"] | None = None
    # HUBSPOT: a HubSpot event (a campaign), with its Navan trips. NAVAN: a Navan trip that matches no HubSpot event.
    source: Literal["HUBSPOT", "NAVAN"]
    # The Navan trips joined to this event (their travel is in `navan`, their costs in its cost)
    navan_events: list[NavanTrip] = []
    navan: NavanFigures
    hubspot: EventHubSpotMatch
    turnout: EventTurnout
    # Actual cost per HubSpot lead; null without cost or leads
    hubspot_cost_per_lead: float | None = None


class OverviewTotals(BaseModel):
    """One row per currency (money is never added across currencies)."""

    currency: str
    event_count: int
    approved_budget: float | None = None
    actual_spend: float | None = None
    budget_variance: float | None = None
    travel_cost: float = 0
    leads: int | None = None
    # The cost of the events that have leads, per lead
    cost_per_lead: float | None = None


class OverviewMonth(BaseModel):
    month: date
    currency: str
    approved_budget: float
    actual_spend: float
    travel_cost: float


class OverviewHubSpotSummary(BaseModel):
    matched: int
    unmatched: int
    registrants: int | None = None
    attended: int | None = None
    leads: int | None = None
    new_contacts: int | None = None
    influenced_contacts: int | None = None
    mqls: int | None = None
    sqls: int | None = None
    opportunities: int | None = None
    customers: int | None = None
    travellers: int = 0


class EventsOverviewResponse(BaseModel):
    period: Period
    totals: list[OverviewTotals]
    monthly: list[OverviewMonth]
    cost_breakdown: list[CurrencyCostBreakdown]
    hubspot: OverviewHubSpotSummary
    events: list[EventOverviewRow]


class Traveller(BaseModel):
    name: str
    email: str | None = None
    department: str | None = None
    booking_types: list[str]
    bookings: int
    amount: float
    currency: str
    first_travel: date | None = None
    last_travel: date | None = None


class Participant(BaseModel):
    name: str | None = None
    email: str | None = None
    state: str | None = None
    at: datetime | None = None


class EventOverviewDetail(EventOverviewRow):
    travellers: list[Traveller]
    # Who registered, attended, cancelled or missed the matched marketing event
    participants: list[Participant]
    # The matched campaign's assets by type: a list of {"id", "name"}, or {"count": n} for meetings
    campaign_assets: dict = {}


class HubSpotOption(BaseModel):
    id: str
    name: str
    starts_on: date | None = None


class HubSpotOptions(BaseModel):
    campaigns: list[HubSpotOption]
    marketing_events: list[HubSpotOption]


class NavanTripOption(BaseModel):
    id: UUID
    name: str
    starts_on: date | None = None


class NavanTripLinkUpdate(BaseModel):
    """Join a Navan trip to the event (link), or take it off again (link false)."""

    trip_id: UUID
    link: bool = True


class HubSpotLinkUpdate(BaseModel):
    """What to link the event to. Both empty with `none` set means "no HubSpot match"; both empty with `none` not set
    removes the pick, so the name match is used again."""

    campaign_id: str | None = None
    marketing_event_id: str | None = None
    none: bool = False
