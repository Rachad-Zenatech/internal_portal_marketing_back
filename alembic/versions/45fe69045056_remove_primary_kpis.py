"""remove primary kpis

KPIs are no longer set when anything is created by hand, and the verdict is judged on the return (ROI), so the main
KPI of initiatives, the default KPI per sub-channel and the KPI list go. Downgrading brings back the list and the
sub-channel defaults, but not the KPIs initiatives had.

Revision ID: 45fe69045056
Revises: 7d3e9a51c2b8
Create Date: 2026-10-19 11:47:53.803418

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '45fe69045056'
down_revision: Union[str, Sequence[str], None] = '7d3e9a51c2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The KPI list as it was: (code, name, sort_order)
PRIMARY_KPIS = [
    ("IMPRESSIONS", "Impressions", 10), ("CTR", "CTR", 20), ("CPC", "CPC", 30), ("CPM", "CPM", 40),
    ("CP_VISITOR", "CP-Visitor", 45), ("CPL", "CPL", 50), ("CP_FOLLOW_UP", "CP-Follow-up", 55),
    ("CP_MQL", "CP-MQL", 60), ("CP_SQL", "CP-SQL", 70), ("CP_TRIAL", "CP-Trial", 80), ("CP_DEMO", "CP-Demo", 85),
    ("CAC", "CAC", 90), ("CONVERSION_RATE", "Conversion Rate", 100), ("PIPELINE_GENERATED", "Pipeline Generated", 110),
    ("REVENUE", "Revenue", 120), ("ROI", "ROI", 130), ("LTV_CAC", "LTV/CAC", 140),
]
# Default KPI per sub-channel: (sub-channel code, KPI code)
DEFAULT_KPIS = [
    ("GOOGLE_ADS", "CPL"), ("BING_ADS", "CPL"), ("WEBINARS", "CPL"), ("VIRTUAL_EVENTS", "CPL"),
    ("SPONSORSHIPS", "CPL"), ("IN_PERSON_EVENTS", "CP_DEMO"),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('evaluation_defaults_primary_kpi_id_fkey', 'evaluation_defaults', type_='foreignkey')
    op.drop_column('evaluation_defaults', 'primary_kpi_id')
    op.drop_constraint('initiatives_primary_kpi_id_fkey', 'initiatives', type_='foreignkey')
    op.drop_column('initiatives', 'primary_kpi_id')
    op.drop_table('primary_kpis')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table('primary_kpis',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('code', sa.VARCHAR(length=50), nullable=False),
    sa.Column('name', sa.VARCHAR(length=100), nullable=False),
    sa.Column('sort_order', sa.INTEGER(), server_default=sa.text('0'), nullable=False),
    sa.Column('is_active', sa.BOOLEAN(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name='primary_kpis_pkey'),
    sa.UniqueConstraint('code', name='primary_kpis_code_key')
    )
    op.bulk_insert(
        sa.table("primary_kpis", sa.column("code"), sa.column("name"), sa.column("sort_order")),
        [{"code": c, "name": n, "sort_order": order} for c, n, order in PRIMARY_KPIS],
    )
    op.add_column('evaluation_defaults', sa.Column('primary_kpi_id', sa.INTEGER(), nullable=True))
    op.create_foreign_key('evaluation_defaults_primary_kpi_id_fkey', 'evaluation_defaults', 'primary_kpis', ['primary_kpi_id'], ['id'])
    op.add_column('initiatives', sa.Column('primary_kpi_id', sa.INTEGER(), nullable=True))
    op.create_foreign_key('initiatives_primary_kpi_id_fkey', 'initiatives', 'primary_kpis', ['primary_kpi_id'], ['id'])
    op.get_bind().execute(
        sa.text("""
            UPDATE evaluation_defaults e SET primary_kpi_id = k.id
            FROM unnest(CAST(:subs AS text[]), CAST(:kpis AS text[])) AS d(sub_channel, kpi)
            JOIN sub_channels sc ON sc.code = d.sub_channel
            JOIN primary_kpis k ON k.code = d.kpi
            WHERE e.sub_channel_id = sc.id
        """),
        {"subs": [s for s, _ in DEFAULT_KPIS], "kpis": [k for _, k in DEFAULT_KPIS]},
    )
