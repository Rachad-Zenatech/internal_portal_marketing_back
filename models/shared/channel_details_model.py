"""Channel-specific initiative fields (initiatives.channel_details) and the allowed values the wizard offers.

Every field is optional. Each channel accepts only its own fields: anything else is refused with a plain message.
CHOICES is served by GET /api/reference-data as `choices`, so the front end never hard-codes these lists.
"""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Field → [(code, label)], in the order the wizard shows them
CHOICES: dict[str, list[tuple[str, str]]] = {
    "campaign_objective": [
        ("AWARENESS", "Awareness"), ("TRAFFIC", "Traffic"), ("LEADS", "Leads"), ("CONVERSIONS", "Conversions"),
        ("ENGAGEMENT", "Engagement"), ("VIDEO_VIEWS", "Video views"),
    ],
    "focus_area": [
        ("TECHNICAL", "Technical"), ("CONTENT", "Content"), ("LINK_BUILDING", "Link building"), ("LOCAL", "Local"),
        ("OTHER", "Other"),
    ],
    "content_type": [
        ("BLOG_POST", "Blog post"), ("CASE_STUDY", "Case study"), ("WHITEPAPER", "Whitepaper"), ("EBOOK", "eBook"),
        ("VIDEO", "Video"), ("WEBINAR", "Webinar"), ("INFOGRAPHIC", "Infographic"), ("OTHER", "Other"),
    ],
    "email_platform": [("HUBSPOT", "HubSpot"), ("MAILCHIMP", "Mailchimp"), ("OTHER", "Other")],
    "partnership_type": [
        ("REFERRAL", "Referral"), ("RESELLER", "Reseller"), ("CO_MARKETING", "Co-marketing"),
        ("SPONSORSHIP", "Sponsorship"), ("TECHNOLOGY", "Technology"), ("OTHER", "Other"),
    ],
    # Systems an external campaign can be linked from
    "link_system": [
        ("HUBSPOT", "HubSpot"), ("GOOGLE_ADS", "Google Ads"), ("MICROSOFT_ADS", "Microsoft Ads"),
        ("LINKEDIN_ADS", "LinkedIn Ads"), ("META_ADS", "Meta Ads"),
    ],
    # Where an initiative's results come from
    "data_source": [
        ("LINKED", "Link an existing external campaign"), ("MANUAL", "Enter results manually"),
        ("LATER", "Connect a data source later"),
    ],
    # Shown on the evaluation step; not editable
    "revenue_basis": [("CLOSED_REVENUE", "Closed revenue")],
}


def _codes(field: str) -> tuple[str, ...]:
    return tuple(code for code, _ in CHOICES[field])


CampaignObjective = Literal[_codes("campaign_objective")]
FocusArea = Literal[_codes("focus_area")]
ContentType = Literal[_codes("content_type")]
EmailPlatform = Literal[_codes("email_platform")]
PartnershipType = Literal[_codes("partnership_type")]
LinkSystem = Literal[_codes("link_system")]
DataSource = Literal[_codes("data_source")]

# Labels used in error messages
FIELD_LABELS = {
    "ad_account_id": "Ad account ID",
    "campaign_objective": "Campaign objective",
    "website": "Website",
    "focus_area": "Focus area",
    "content_type": "Content type",
    "distribution_channel": "Distribution channel",
    "email_platform": "Email platform",
    "campaign_or_workflow": "Campaign or workflow",
    "partner_name": "Partner name",
    "partnership_type": "Partnership type",
    "profile_account": "Profile or account",
    "content_group": "Content group",
}

_WEBSITE = re.compile(r"^(https://\S+|([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(/\S*)?)$", re.IGNORECASE)


class _Details(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class NoDetails(_Details):
    """Channels without fields of their own (Events keep theirs in `event`)."""


class PaidMediaDetails(_Details):
    """Paid Search and Paid Social: the ad platform is the sub-channel."""

    ad_account_id: str | None = Field(default=None, max_length=100)
    campaign_objective: CampaignObjective | None = None


class SeoDetails(_Details):
    website: str | None = Field(default=None, max_length=255)
    focus_area: FocusArea | None = None

    @field_validator("website")
    @classmethod
    def check_website(cls, value: str | None) -> str | None:
        if value is not None and not _WEBSITE.match(value):
            raise ValueError("Website must be a domain (e.g. example.com) or a link starting with https://")
        return value


class ContentDetails(_Details):
    content_type: ContentType | None = None
    distribution_channel: str | None = Field(default=None, max_length=120)


class EmailDetails(_Details):
    """Email Marketing and Marketing Automation."""

    email_platform: EmailPlatform | None = None
    campaign_or_workflow: str | None = Field(default=None, max_length=200)


class PartnerDetails(_Details):
    """Partnerships and Affiliates."""

    partner_name: str | None = Field(default=None, max_length=200)
    partnership_type: PartnershipType | None = None


class OrganicSocialDetails(_Details):
    """Organic Social: the platform is the sub-channel."""

    profile_account: str | None = Field(default=None, max_length=200)
    content_group: str | None = Field(default=None, max_length=200)


# Channel code → its fields; channels not listed have none
CHANNEL_DETAILS_MODELS: dict[str, type[_Details]] = {
    "PAID_SEARCH": PaidMediaDetails,
    "PAID_SOCIAL": PaidMediaDetails,
    "SEO": SeoDetails,
    "CONTENT_MARKETING": ContentDetails,
    "EMAIL_MARKETING": EmailDetails,
    "MARKETING_AUTOMATION": EmailDetails,
    "PARTNERSHIPS": PartnerDetails,
    "AFFILIATES": PartnerDetails,
    "ORGANIC_SOCIAL": OrganicSocialDetails,
}


def details_fields(channel_code: str) -> set[str]:
    return set(CHANNEL_DETAILS_MODELS.get(channel_code, NoDetails).model_fields)


def _plain_message(exc: ValidationError, channel_name: str) -> str:
    error = exc.errors()[0]
    field = str(error["loc"][0]) if error["loc"] else ""
    label = FIELD_LABELS.get(field, field)
    kind = error["type"]
    if kind == "extra_forbidden":
        return f"{channel_name} initiatives don't have a field called {field}."
    if kind == "literal_error":
        return f"{label} must be one of: {', '.join(name for _, name in CHOICES[field])}."
    if kind == "string_too_long":
        return f"{label} can be at most {error['ctx']['max_length']} characters."
    if kind == "string_type":
        return f"{label} must be text."
    if kind == "value_error":
        return str(error["ctx"]["error"])
    return f"{label} is not valid."


def validate_channel_details(channel_code: str, channel_name: str, details: object) -> dict:
    """Return the channel's details with empty values left out; raise ValueError with a plain message."""
    if details is None:
        return {}
    if not isinstance(details, dict):
        raise ValueError("Channel details must be an object.")
    model = CHANNEL_DETAILS_MODELS.get(channel_code, NoDetails)
    try:
        return model.model_validate(details).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ValueError(_plain_message(exc, channel_name)) from exc
