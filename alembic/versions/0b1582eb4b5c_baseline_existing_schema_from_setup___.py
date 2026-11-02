"""baseline: existing schema from setup_*_schema scripts

Revision ID: 0b1582eb4b5c
Revises: 
Create Date: 2026-10-13 09:16:00.441498

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0b1582eb4b5c'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # No-op: this revision marks the schema already created by the
    # postgresql_db/schemas/setup_*_schema.py scripts. A fresh database runs those
    # scripts, then `alembic upgrade head`. A database that predates Alembic
    # is brought to this point once with `alembic stamp 0b1582eb4b5c`.
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
