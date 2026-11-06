"""ad results hubspot source

Lets an ad result come from the HubSpot sync (spend, contacts, MQLs and SQLs of a linked HubSpot campaign) until the
Google Ads and Microsoft Ads syncs can be used. Only the allowed values of ad_results.source change.

Revision ID: c9e5a1b3d7f2
Revises: b8d4f0a2c6e1
Create Date: 2026-11-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9e5a1b3d7f2'
down_revision: Union[str, Sequence[str], None] = 'b8d4f0a2c6e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WITH = "source IN ('MANUAL', 'SPREADSHEET', 'DATA_STUDIO', 'GOOGLE_ADS_API', 'MICROSOFT_ADS_API', 'HUBSPOT')"
_WITHOUT = "source IN ('MANUAL', 'SPREADSHEET', 'DATA_STUDIO', 'GOOGLE_ADS_API', 'MICROSOFT_ADS_API')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ad_results_source_check', 'ad_results', type_='check')
    op.create_check_constraint('ad_results_source_check', 'ad_results', _WITH)


def downgrade() -> None:
    """Downgrade schema."""
    # The HubSpot sync writes these again on its next run
    op.execute("DELETE FROM ad_results WHERE source = 'HUBSPOT'")
    op.drop_constraint('ad_results_source_check', 'ad_results', type_='check')
    op.create_check_constraint('ad_results_source_check', 'ad_results', _WITHOUT)
