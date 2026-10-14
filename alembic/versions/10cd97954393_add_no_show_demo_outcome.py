"""add no-show demo outcome

Revision ID: 10cd97954393
Revises: fbe76c69be9a
Create Date: 2026-10-14 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '10cd97954393'
down_revision: Union[str, Sequence[str], None] = 'fbe76c69be9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A demo booked at the event that never happened. It doesn't count as a demo given, so it can't
    # make the event look cheaper per demo. Sorted after "No outcome yet" (10), before "Not a fit" (20).
    op.execute(
        """
        INSERT INTO demo_outcomes (code, name, funnel_rank, sort_order)
        VALUES ('NO_SHOW', 'No-show', 0, 15)
        ON CONFLICT (code) DO NOTHING
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Demos marked as no-shows go back to "No outcome yet" before the outcome is removed
    op.execute(
        """
        UPDATE event_demos SET outcome_id = (SELECT id FROM demo_outcomes WHERE code = 'PENDING')
        WHERE outcome_id = (SELECT id FROM demo_outcomes WHERE code = 'NO_SHOW')
        """
    )
    op.execute("DELETE FROM demo_outcomes WHERE code = 'NO_SHOW'")
