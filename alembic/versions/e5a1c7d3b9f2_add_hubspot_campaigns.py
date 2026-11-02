"""add hubspot campaigns

HubSpot campaigns as the HubSpot sync (services/hubspot/hubspot_campaign_sync_job.py) last read them: properties, the
campaign report's figures and the campaign's assets. A new table only; nothing else changes.

Revision ID: e5a1c7d3b9f2
Revises: 8d4b2f6e1a90
Create Date: 2026-10-30 10:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e5a1c7d3b9f2'
down_revision: Union[str, Sequence[str], None] = '8d4b2f6e1a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('hubspot_campaigns',
    sa.Column('id', sa.VARCHAR(length=64), nullable=False),
    sa.Column('name', sa.VARCHAR(length=500), nullable=False),
    sa.Column('status', sa.VARCHAR(length=30), nullable=True),
    sa.Column('start_date', sa.DATE(), nullable=True),
    sa.Column('end_date', sa.DATE(), nullable=True),
    sa.Column('currency', sa.VARCHAR(length=3), nullable=True),
    sa.Column('budget_total', sa.NUMERIC(precision=14, scale=2), nullable=True),
    sa.Column('spend_total', sa.NUMERIC(precision=14, scale=2), nullable=True),
    sa.Column('owner_name', sa.VARCHAR(length=255), nullable=True),
    sa.Column('utm', sa.VARCHAR(length=500), nullable=True),
    sa.Column('notes', sa.TEXT(), nullable=True),
    sa.Column('audience', sa.TEXT(), nullable=True),
    sa.Column('sessions', sa.INTEGER(), nullable=True),
    sa.Column('new_contacts_first_touch', sa.INTEGER(), nullable=True),
    sa.Column('new_contacts_last_touch', sa.INTEGER(), nullable=True),
    sa.Column('influenced_contacts', sa.INTEGER(), nullable=True),
    sa.Column('assets', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('hubspot_created_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('hubspot_updated_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('synced_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('end_date IS NULL OR start_date IS NULL OR end_date >= start_date', name='hubspot_campaigns_dates_check'),
    sa.CheckConstraint("jsonb_typeof(assets) = 'object'", name='hubspot_campaigns_assets_check'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_hubspot_campaigns_name', 'hubspot_campaigns', ['name'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM integration_sync_state WHERE name = 'hubspot_campaigns'")
    op.drop_index('idx_hubspot_campaigns_name', table_name='hubspot_campaigns')
    op.drop_table('hubspot_campaigns')
