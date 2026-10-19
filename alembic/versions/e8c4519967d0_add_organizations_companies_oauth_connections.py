"""add organizations companies oauth_connections

Revision ID: e8c4519967d0
Revises: 04c07e13860f
Create Date: 2026-10-19 12:47:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e8c4519967d0'
down_revision: Union[str, Sequence[str], None] = '04c07e13860f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. organizations
    op.create_table(
        'organizations',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('name', sa.VARCHAR(length=255), nullable=False),
        sa.Column('slug', sa.VARCHAR(length=100), nullable=False),
        sa.Column('description', sa.TEXT(), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug', name='organizations_slug_key')
    )

    # 2. companies
    op.create_table(
        'companies',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.VARCHAR(length=255), nullable=False),
        sa.Column('code', sa.VARCHAR(length=50), nullable=False),
        sa.Column('parent_company_id', sa.UUID(), nullable=True),
        sa.Column('brand_id', sa.INTEGER(), nullable=True),
        sa.Column('currency', sa.VARCHAR(length=3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column('is_active', sa.BOOLEAN(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['brand_id'], ['brands.id'], name='companies_brand_id_fkey', ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='companies_organization_id_fkey', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_company_id'], ['companies.id'], name='companies_parent_company_id_fkey', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'code', name='companies_org_code_key')
    )
    op.create_index('idx_companies_organization', 'companies', ['organization_id'])
    op.create_index('idx_companies_brand', 'companies', ['brand_id'])

    # 3. oauth_connections
    op.create_table(
        'oauth_connections',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('company_id', sa.UUID(), nullable=True),
        sa.Column('provider', sa.VARCHAR(length=50), nullable=False),
        sa.Column('provider_account_id', sa.VARCHAR(length=255), nullable=True),
        sa.Column('account_name', sa.VARCHAR(length=255), nullable=True),
        sa.Column('access_token_encrypted', sa.TEXT(), nullable=False),
        sa.Column('refresh_token_encrypted', sa.TEXT(), nullable=True),
        sa.Column('token_type', sa.VARCHAR(length=50), server_default=sa.text("'Bearer'"), nullable=False),
        sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('scopes', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), nullable=True),
        sa.Column('status', sa.VARCHAR(length=50), server_default=sa.text("'CONNECTED'"), nullable=False),
        sa.Column('error_detail', sa.TEXT(), nullable=True),
        sa.Column('connection_metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('updated_by', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], name='oauth_connections_company_id_fkey', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name='oauth_connections_created_by_fkey', ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='oauth_connections_organization_id_fkey', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name='oauth_connections_updated_by_fkey', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_oauth_connections_org_company', 'oauth_connections', ['organization_id', 'company_id'])
    op.create_index('idx_oauth_connections_provider', 'oauth_connections', ['provider'])
    op.create_index('idx_oauth_connections_status', 'oauth_connections', ['status'])

    # Seed initial Organization and Companies matching the ZenaTech group & brands
    op.execute("""
        INSERT INTO organizations (id, name, slug, description)
        VALUES ('00000000-0000-0000-0000-000000000001', 'ZenaTech Group', 'zenatech-group', 'ZenaTech Group Parent Organization')
        ON CONFLICT (slug) DO NOTHING
    """)
    op.execute("""
        INSERT INTO companies (organization_id, name, code, brand_id, currency)
        SELECT 
            '00000000-0000-0000-0000-000000000001'::uuid,
            b.name,
            b.code,
            b.id,
            'USD'
        FROM brands b
        ON CONFLICT (organization_id, code) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_index('idx_oauth_connections_status', table_name='oauth_connections')
    op.drop_index('idx_oauth_connections_provider', table_name='oauth_connections')
    op.drop_index('idx_oauth_connections_org_company', table_name='oauth_connections')
    op.drop_table('oauth_connections')
    op.drop_index('idx_companies_brand', table_name='companies')
    op.drop_index('idx_companies_organization', table_name='companies')
    op.drop_table('companies')
    op.drop_table('organizations')
