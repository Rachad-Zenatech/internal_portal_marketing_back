"""add paid search entries

The Paid Search board: one row per entry, with the 24 columns of Marketing's PPC sheet kept as typed (text, so a
half-filled row can be saved), the HubSpot campaign it takes its leads, MQLs and SQLs from, and its place on the
board. A new table; nothing else changes.

Revision ID: c4e8a2d6f9b1
Revises: b8d4f0a2c6e1
Create Date: 2026-11-06 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4e8a2d6f9b1'
down_revision: Union[str, Sequence[str], None] = 'b8d4f0a2c6e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The sheet's columns, as on the board (see models/paid_search/paid_search_model.py)
_TEXT_COLUMNS = (
    "brand", "market", "period_start", "period_end", "channel", "sub_channel", "campaign_name", "primary_kpi",
    "budget_allocated", "actual_spend", "impressions", "clicks", "ctr", "cpc", "ads_conversions", "hs_contacts",
    "ads_conv_rate", "cpl", "mqls", "sqls", "last_update", "status", "report_link", "tracking_detail",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'paid_search_entries',
        sa.Column('id', sa.UUID(), primary_key=True),
        # Order on the board
        sa.Column('position', sa.INTEGER(), nullable=False, server_default=sa.text('0')),
        *(sa.Column(name, sa.TEXT(), nullable=False, server_default=sa.text("''")) for name in _TEXT_COLUMNS),
        sa.Column('hubspot_campaign_id', sa.VARCHAR(length=64)),
        # AUTO (matched by name), MANUAL (picked by someone) or NONE (someone said it has no HubSpot campaign)
        sa.Column('hubspot_link', sa.VARCHAR(length=10)),
        sa.Column('leads', sa.VARCHAR(length=20)),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_by', sa.UUID(), sa.ForeignKey('users.id', name='paid_search_entries_updated_by_fkey', ondelete='SET NULL')),
    )
    op.create_index('idx_paid_search_entries_position', 'paid_search_entries', ['position'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_paid_search_entries_position', table_name='paid_search_entries')
    op.drop_table('paid_search_entries')
