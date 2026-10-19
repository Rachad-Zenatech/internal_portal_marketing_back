"""replaced cost sheet uploads

Revision ID: 7d3e9a51c2b8
Revises: 088ebf56cf5d
Create Date: 2026-10-19 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7d3e9a51c2b8'
down_revision: Union[str, Sequence[str], None] = '088ebf56cf5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Written by hand: autogenerate doesn't detect check constraints on existing tables.
    # REPLACED: a cost sheet upload whose costs a newer upload of the same event's sheet took over.
    op.drop_constraint('import_batches_status_check', 'import_batches', type_='check')
    op.create_check_constraint(
        'import_batches_status_check',
        'import_batches',
        "status IN ('PREVIEW', 'CONFIRMED', 'UNDONE', 'EXPIRED', 'REPLACED')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # A replaced upload's costs are gone, so it reads as undone
    op.execute("UPDATE import_batches SET status = 'UNDONE' WHERE status = 'REPLACED'")
    op.drop_constraint('import_batches_status_check', 'import_batches', type_='check')
    op.create_check_constraint(
        'import_batches_status_check',
        'import_batches',
        "status IN ('PREVIEW', 'CONFIRMED', 'UNDONE', 'EXPIRED')",
    )
