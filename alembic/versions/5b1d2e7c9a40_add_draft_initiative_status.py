"""add draft initiative status

Revision ID: 5b1d2e7c9a40
Revises: c03b94e17306
Create Date: 2026-10-15 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5b1d2e7c9a40'
down_revision: Union[str, Sequence[str], None] = 'c03b94e17306'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Written by hand: autogenerate doesn't detect check constraints on existing tables.
    # A draft is an initiative saved part-way, to be finished later.
    op.drop_constraint('initiatives_status_check', 'initiatives', type_='check')
    op.create_check_constraint(
        'initiatives_status_check',
        'initiatives',
        "status IN ('DRAFT', 'PLANNED', 'ACTIVE', 'COMPLETED', 'PAUSED', 'CANCELLED')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drafts become Planned before the status is removed
    op.execute("UPDATE initiatives SET status = 'PLANNED' WHERE status = 'DRAFT'")
    op.drop_constraint('initiatives_status_check', 'initiatives', type_='check')
    op.create_check_constraint(
        'initiatives_status_check',
        'initiatives',
        "status IN ('PLANNED', 'ACTIVE', 'COMPLETED', 'PAUSED', 'CANCELLED')",
    )
