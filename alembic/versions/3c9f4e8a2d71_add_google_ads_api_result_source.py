"""add google ads api result source

Ad results synced from the Google Ads API (services/google_ads_sync_job.py) get their own source, GOOGLE_ADS_API.

Revision ID: 3c9f4e8a2d71
Revises: b7e2d4c9a1f3
Create Date: 2026-10-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

_OLD_SOURCES = "source IN ('MANUAL', 'SPREADSHEET', 'DATA_STUDIO')"
_NEW_SOURCES = "source IN ('MANUAL', 'SPREADSHEET', 'DATA_STUDIO', 'GOOGLE_ADS_API')"

# revision identifiers, used by Alembic.
revision: str = '3c9f4e8a2d71'
down_revision: Union[str, Sequence[str], None] = 'b7e2d4c9a1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Written by hand: autogenerate doesn't detect check constraint changes
    op.drop_constraint('ad_results_source_check', 'ad_results', type_='check')
    op.create_check_constraint('ad_results_source_check', 'ad_results', _NEW_SOURCES)


def downgrade() -> None:
    """Downgrade schema."""
    # The synced results can't keep their source: they go, with the cost lines that mirror them and the sync's state.
    # The campaigns the sync created and their Google Ads links stay.
    op.execute(
        "DELETE FROM spend_lines WHERE external_ref IN "
        "(SELECT 'ad_result:' || id::text FROM ad_results WHERE source = 'GOOGLE_ADS_API')"
    )
    op.execute("DELETE FROM ad_results WHERE source = 'GOOGLE_ADS_API'")
    op.execute("DELETE FROM integration_sync_state WHERE name IN ('google_ads', 'google_ads_mock')")
    op.drop_constraint('ad_results_source_check', 'ad_results', type_='check')
    op.create_check_constraint('ad_results_source_check', 'ad_results', _OLD_SOURCES)
