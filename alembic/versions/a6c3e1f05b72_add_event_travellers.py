"""add event travellers

People travelling to each event, from its active Navan bookings (kept by the Navan sync). Only the name, email and
department come from Navan: never passports, managers, cost centres, employee ids or HR details.

Revision ID: a6c3e1f05b72
Revises: cddb88f18bd9
Create Date: 2026-10-19 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a6c3e1f05b72'
down_revision: Union[str, Sequence[str], None] = 'cddb88f18bd9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'event_travellers',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('initiative_id', sa.UUID(), nullable=False),
        sa.Column('navan_person_uuid', sa.VARCHAR(length=64), nullable=False),
        sa.Column('name', sa.VARCHAR(length=255), nullable=False),
        sa.Column('email', sa.VARCHAR(length=320), nullable=True),
        sa.Column('department', sa.VARCHAR(length=255), nullable=True),
        sa.Column('user_id', sa.UUID(), nullable=True),
        sa.Column('booking_types', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column('bookings', sa.INTEGER(), server_default=sa.text('0'), nullable=False),
        sa.Column('amount', sa.NUMERIC(precision=14, scale=2), server_default=sa.text('0'), nullable=False),
        sa.Column('currency', sa.CHAR(length=3), nullable=False),
        sa.Column('first_travel', sa.DATE(), nullable=True),
        sa.Column('last_travel', sa.DATE(), nullable=True),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['initiative_id'], ['initiatives.id'], name='event_travellers_initiative_id_fkey',
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='event_travellers_user_id_fkey', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('initiative_id', 'navan_person_uuid', name='event_travellers_initiative_person_key'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('event_travellers')
