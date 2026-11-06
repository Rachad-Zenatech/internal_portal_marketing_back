"""hubspot contact lists

hubspot_contact_lists: HubSpot's contact lists (name and size), filled by the HubSpot sync, so an event's lead list can
be found by name when it isn't attached to the event's campaign.

Revision ID: e2a7c3d9b5f1
Revises: d1f6b2c4e8a3
Create Date: 2026-11-06 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e2a7c3d9b5f1'
down_revision: Union[str, Sequence[str], None] = 'd1f6b2c4e8a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'hubspot_contact_lists',
        sa.Column('id', sa.VARCHAR(length=64), primary_key=True),
        sa.Column('name', sa.VARCHAR(length=500), nullable=False),
        sa.Column('processing_type', sa.VARCHAR(length=20)),
        sa.Column('size', sa.INTEGER()),
        sa.Column('synced_at', postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('hubspot_contact_lists')
