"""add paid social ad spend sources

Ad results now cover every Paid Search and Paid Social platform, so their spend lines need a source for each
platform that isn't Google Ads or Microsoft Ads (YouTube Ads spend is billed through Google Ads).

Revision ID: b7e2d4c9a1f3
Revises: a6c3e1f05b72
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

_OLD_SOURCES = (
    "source IN ('MANUAL', 'STAFF_CALCULATOR', 'COST_SHEET_UPLOAD', 'TRAVEL_FEED', 'GOOGLE_ADS', 'MICROSOFT_ADS', 'NAVAN')"
)
_NEW_SOURCES = (
    "source IN ('MANUAL', 'STAFF_CALCULATOR', 'COST_SHEET_UPLOAD', 'TRAVEL_FEED', 'GOOGLE_ADS', 'MICROSOFT_ADS', 'NAVAN', "
    "'LINKEDIN_ADS', 'META_ADS', 'TIKTOK_ADS', 'X_ADS', 'REDDIT_ADS', 'OTHER_ADS')"
)
_ADDED = "('LINKEDIN_ADS', 'META_ADS', 'TIKTOK_ADS', 'X_ADS', 'REDDIT_ADS', 'OTHER_ADS')"

# revision identifiers, used by Alembic.
revision: str = 'b7e2d4c9a1f3'
down_revision: Union[str, Sequence[str], None] = 'a6c3e1f05b72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Written by hand: autogenerate doesn't detect check constraint changes
    op.drop_constraint('spend_lines_source_check', 'spend_lines', type_='check')
    op.create_check_constraint('spend_lines_source_check', 'spend_lines', _NEW_SOURCES)


def downgrade() -> None:
    """Downgrade schema."""
    # Those lines mirror ad results, so they go with the sources (the results themselves stay)
    op.execute(f"DELETE FROM spend_lines WHERE source IN {_ADDED} AND external_ref LIKE 'ad\\_result:%'")
    op.drop_constraint('spend_lines_source_check', 'spend_lines', type_='check')
    op.create_check_constraint('spend_lines_source_check', 'spend_lines', _OLD_SOURCES)
