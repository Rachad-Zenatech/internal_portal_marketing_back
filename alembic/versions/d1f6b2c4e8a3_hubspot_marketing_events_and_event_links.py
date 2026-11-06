"""hubspot marketing events and event links

hubspot_marketing_events: HubSpot's marketing events with their attendance, filled by the HubSpot sync.
event_hubspot_links: the HubSpot campaign / marketing event someone picked for an event on the Events Overview.

Revision ID: d1f6b2c4e8a3
Revises: c9e5a1b3d7f2
Create Date: 2026-11-06 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd1f6b2c4e8a3'
down_revision: Union[str, Sequence[str], None] = 'c9e5a1b3d7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'hubspot_marketing_events',
        sa.Column('id', sa.VARCHAR(length=64), primary_key=True),
        sa.Column('name', sa.VARCHAR(length=500), nullable=False),
        sa.Column('event_type', sa.VARCHAR(length=120)),
        sa.Column('start_at', postgresql.TIMESTAMP(timezone=True)),
        sa.Column('end_at', postgresql.TIMESTAMP(timezone=True)),
        sa.Column('status', sa.VARCHAR(length=30)),
        sa.Column('event_url', sa.VARCHAR(length=1000)),
        sa.Column('registrants', sa.INTEGER()),
        sa.Column('attendees', sa.INTEGER()),
        sa.Column('cancellations', sa.INTEGER()),
        sa.Column('no_shows', sa.INTEGER()),
        sa.Column('participants', postgresql.JSONB()),
        sa.Column('hubspot_created_at', postgresql.TIMESTAMP(timezone=True)),
        sa.Column('hubspot_updated_at', postgresql.TIMESTAMP(timezone=True)),
        sa.Column('synced_at', postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('idx_hubspot_marketing_events_start', 'hubspot_marketing_events', ['start_at'])
    op.create_table(
        'event_hubspot_links',
        sa.Column('initiative_id', sa.UUID(), sa.ForeignKey('initiatives.id', name='event_hubspot_links_initiative_id_fkey', ondelete='CASCADE'), primary_key=True),
        sa.Column('hubspot_campaign_id', sa.VARCHAR(length=64)),
        sa.Column('hubspot_marketing_event_id', sa.VARCHAR(length=64)),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_by', sa.UUID(), sa.ForeignKey('users.id', name='event_hubspot_links_updated_by_fkey', ondelete='SET NULL')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('event_hubspot_links')
    op.drop_index('idx_hubspot_marketing_events_start', table_name='hubspot_marketing_events')
    op.drop_table('hubspot_marketing_events')
