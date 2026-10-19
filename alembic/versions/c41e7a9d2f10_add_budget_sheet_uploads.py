"""add budget sheet uploads

Revision ID: c41e7a9d2f10
Revises: f7d535a85927
Create Date: 2026-10-16 14:20:00.000000

"""
from typing import Sequence, Union

from alembic import op

_OLD_KINDS = "kind IN ('COST_SHEET', 'EVENTS', 'GOOGLE_ADS', 'MICROSOFT_ADS', 'ADS_RESULTS')"
_NEW_KINDS = "kind IN ('COST_SHEET', 'EVENTS', 'GOOGLE_ADS', 'MICROSOFT_ADS', 'ADS_RESULTS', 'BUDGETS')"

# revision identifiers, used by Alembic.
revision: str = 'c41e7a9d2f10'
down_revision: Union[str, Sequence[str], None] = 'f7d535a85927'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Written by hand: autogenerate doesn't detect check constraint changes
    op.drop_constraint('import_batches_kind_check', 'import_batches', type_='check')
    op.create_check_constraint('import_batches_kind_check', 'import_batches', _NEW_KINDS)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM import_batches WHERE kind = 'BUDGETS'")
    op.drop_constraint('import_batches_kind_check', 'import_batches', type_='check')
    op.create_check_constraint('import_batches_kind_check', 'import_batches', _OLD_KINDS)
