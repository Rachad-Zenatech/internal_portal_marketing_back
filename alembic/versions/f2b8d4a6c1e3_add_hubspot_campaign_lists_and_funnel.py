"""add hubspot campaign lists and funnel

Per HubSpot campaign: the people on its contact lists (counted once), and how many of its contacts reached each
lifecycle stage, by first touch, by last touch and among the list members. Two new columns on hubspot_campaigns;
nothing else changes.

Revision ID: f2b8d4a6c1e3
Revises: e5a1c7d3b9f2
Create Date: 2026-10-30 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f2b8d4a6c1e3'
down_revision: Union[str, Sequence[str], None] = 'e5a1c7d3b9f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('hubspot_campaigns', sa.Column('list_contacts', sa.INTEGER(), nullable=True))
    op.add_column('hubspot_campaigns', sa.Column('funnel', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.create_check_constraint('hubspot_campaigns_funnel_check', 'hubspot_campaigns', "jsonb_typeof(funnel) = 'object'")
    op.create_check_constraint('hubspot_campaigns_list_contacts_check', 'hubspot_campaigns', 'list_contacts >= 0')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('hubspot_campaigns_list_contacts_check', 'hubspot_campaigns', type_='check')
    op.drop_constraint('hubspot_campaigns_funnel_check', 'hubspot_campaigns', type_='check')
    op.drop_column('hubspot_campaigns', 'funnel')
    op.drop_column('hubspot_campaigns', 'list_contacts')
