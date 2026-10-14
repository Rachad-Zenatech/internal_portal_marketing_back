"""only invoiced or paid costs can be reconciled

Revision ID: 10ee8279809e
Revises: 3d0f8c7132df
Create Date: 2026-10-14 09:55:15.745039

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '10ee8279809e'
down_revision: Union[str, Sequence[str], None] = '3d0f8c7132df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Written by hand: autogenerate doesn't detect check constraints on existing tables
    op.create_check_constraint(
        'spend_lines_reconciled_status_check',
        'spend_lines',
        "reconciled_month IS NULL OR status IN ('INVOICED', 'PAID')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('spend_lines_reconciled_status_check', 'spend_lines', type_='check')
