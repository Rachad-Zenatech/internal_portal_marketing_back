"""add hubspot social posts

Social posts as the HubSpot sync (services/hubspot/hubspot_campaign_sync_job.py) last read them: the account, status,
text and the counters (clicks, likes, replies, shares, interactions) of every post, with the campaign it belongs to.
One new table; nothing else changes.

Revision ID: a7c3e9d1b4f6
Revises: f2b8d4a6c1e3
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d1b4f6'
down_revision: Union[str, Sequence[str], None] = 'f2b8d4a6c1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('hubspot_social_posts',
    sa.Column('id', sa.VARCHAR(length=64), nullable=False),
    sa.Column('channel_key', sa.VARCHAR(length=120), nullable=False),
    sa.Column('platform', sa.VARCHAR(length=30), nullable=False),
    sa.Column('channel_name', sa.VARCHAR(length=255), nullable=True),
    sa.Column('status', sa.VARCHAR(length=30), nullable=True),
    sa.Column('message', sa.TEXT(), nullable=True),
    sa.Column('media_type', sa.VARCHAR(length=30), nullable=True),
    sa.Column('campaign_id', sa.VARCHAR(length=64), nullable=True),
    sa.Column('campaign_name', sa.VARCHAR(length=500), nullable=True),
    sa.Column('clicks', sa.INTEGER(), nullable=True),
    sa.Column('likes', sa.INTEGER(), nullable=True),
    sa.Column('replies', sa.INTEGER(), nullable=True),
    sa.Column('shares', sa.INTEGER(), nullable=True),
    sa.Column('interactions', sa.INTEGER(), nullable=True),
    sa.Column('scheduled_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('finished_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('hubspot_created_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('synced_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_hubspot_social_posts_campaign', 'hubspot_social_posts', ['campaign_id'], unique=False)
    op.create_index('idx_hubspot_social_posts_platform', 'hubspot_social_posts', ['platform'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_hubspot_social_posts_platform', table_name='hubspot_social_posts')
    op.drop_index('idx_hubspot_social_posts_campaign', table_name='hubspot_social_posts')
    op.drop_table('hubspot_social_posts')
