"""add social post media

The attached photos/videos (HubSpot-hosted file url, type, size) and the link to the live post, for each row of
hubspot_social_posts. Two nullable columns; nothing else changes.

Revision ID: b8d4f0a2c6e1
Revises: a7c3e9d1b4f6
Create Date: 2026-11-04 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b8d4f0a2c6e1'
down_revision: Union[str, Sequence[str], None] = 'a7c3e9d1b4f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('hubspot_social_posts', sa.Column('media', postgresql.JSONB(), nullable=True))
    op.add_column('hubspot_social_posts', sa.Column('post_url', sa.VARCHAR(length=1000), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('hubspot_social_posts', 'post_url')
    op.drop_column('hubspot_social_posts', 'media')
