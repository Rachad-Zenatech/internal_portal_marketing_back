"""
SQLAlchemy Core schema mirror used ONLY for Alembic autogenerate.
Contains core RBAC, Auth, Audit, and Notification tables for the template.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

app_settings = sa.Table(
    "app_settings", metadata,
    sa.Column("key", sa.VARCHAR(length=100), primary_key=True),
    sa.Column("settings_json", sa.TEXT()),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("email", sa.VARCHAR(length=255), nullable=False),
    sa.Column("full_name", sa.VARCHAR(length=255)),
    sa.Column("department", sa.VARCHAR(length=255)),
    sa.Column("job_title", sa.VARCHAR(length=255)),
    sa.Column("auth_provider", sa.VARCHAR(length=50), server_default=sa.text("'microsoft'::character varying")),
    sa.Column("entra_oid", sa.VARCHAR(length=255)),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("is_super_admin", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("last_login_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("last_activity_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("last_logout_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("deleted_by", sa.UUID()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_by", sa.UUID()),
    # Added by setup_graph_sync_schema.py
    sa.Column("graph_account_enabled", sa.BOOLEAN()),
    sa.Column("last_graph_sync", postgresql.TIMESTAMP(timezone=True)),
    # Microsoft SSO / user-management columns (match the Administration portal)
    sa.Column("microsoft_tenant_id", sa.VARCHAR(length=255)),
    sa.Column("microsoft_object_id", sa.VARCHAR(length=255)),
    sa.Column("microsoft_subject_id", sa.VARCHAR(length=255)),
    sa.Column("sso_enabled", sa.BOOLEAN(), nullable=False, server_default=sa.text("false")),
    sa.Column("last_sso_login_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("force_password_change", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", name="users_created_by_fkey")),
    sa.UniqueConstraint("email", name="users_email_key"),
    sa.UniqueConstraint("entra_oid", name="users_entra_oid_key"),
    sa.Index("idx_users_email", "email"),
    sa.Index("idx_users_is_active", "is_active"),
    sa.Index("idx_users_microsoft_subject", "auth_provider", "microsoft_subject_id", unique=True),
    sa.Index("idx_users_microsoft_tenant_object", "microsoft_tenant_id", "microsoft_object_id", unique=True),
)

roles = sa.Table(
    "roles", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("is_system_role", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("parent_role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="SET NULL")),
    sa.Column("display_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("department", sa.VARCHAR(length=255)),
    sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="roles_code_key"),
    sa.Index("idx_roles_code", "code"),
)

user_roles = sa.Table(
    "user_roles", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("assigned_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("assigned_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.UniqueConstraint("user_id", "role_id", name="user_roles_user_id_role_id_key"),
    sa.Index("idx_user_roles_user_id", "user_id"),
    sa.Index("idx_user_roles_role_id", "role_id"),
)

navigation_items = sa.Table(
    "navigation_items", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("route_path", sa.VARCHAR(length=255)),
    sa.Column("parent_code", sa.VARCHAR(length=100)),
    sa.Column("display_order", sa.INTEGER(), server_default=sa.text("0")),
    sa.Column("icon", sa.VARCHAR(length=100)),
    sa.Column("is_menu_item", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="navigation_items_code_key"),
)

permission_actions = sa.Table(
    "permission_actions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_actions_code_key"),
)

role_navigation_permissions = sa.Table(
    "role_navigation_permissions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("navigation_item_id", sa.UUID(), sa.ForeignKey("navigation_items.id", ondelete="CASCADE"), nullable=False),
    sa.Column("action_id", sa.UUID(), sa.ForeignKey("permission_actions.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_allowed", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "navigation_item_id", "action_id", name="role_navigation_permissions_role_id_navigation_item_id_acti_key"),
)

permission_modules = sa.Table(
    "permission_modules", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_modules_id_seq'::regclass)")),
    sa.Column("code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("sort_order", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_modules_code_key"),
)

permission_groups = sa.Table(
    "permission_groups", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_groups_id_seq'::regclass)")),
    sa.Column("module_id", sa.INTEGER(), sa.ForeignKey("permission_modules.id", ondelete="CASCADE")),
    sa.Column("code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("sort_order", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="permission_groups_code_key"),
)

permission_group_actions = sa.Table(
    "permission_group_actions", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('permission_group_actions_id_seq'::regclass)")),
    sa.Column("permission_group_id", sa.INTEGER(), sa.ForeignKey("permission_groups.id", ondelete="CASCADE")),
    sa.Column("api_module_code", sa.VARCHAR(length=255), nullable=False),
    sa.Column("action", sa.VARCHAR(length=255), nullable=False),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
)

role_permission_groups = sa.Table(
    "role_permission_groups", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('role_permission_groups_id_seq'::regclass)")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("permission_group_id", sa.INTEGER(), sa.ForeignKey("permission_groups.id", ondelete="CASCADE")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "permission_group_id", name="role_permission_groups_role_id_permission_group_id_key"),
)

mcp_tools = sa.Table(
    "mcp_tools", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("code", sa.VARCHAR(length=100), nullable=False),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("server_name", sa.VARCHAR(length=100), nullable=False),
    sa.Column("description", sa.TEXT()),
    sa.Column("is_read_only", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_sensitive", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("code", name="mcp_tools_code_key"),
)

role_mcp_tool_permissions = sa.Table(
    "role_mcp_tool_permissions", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("mcp_tool_id", sa.UUID(), sa.ForeignKey("mcp_tools.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_allowed", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("access_level", sa.VARCHAR(length=50), server_default=sa.text("'ALLOW'::character varying")),
    sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.UniqueConstraint("role_id", "mcp_tool_id", name="role_mcp_tool_permissions_role_id_mcp_tool_id_key"),
)

audit_logs = sa.Table(
    "audit_logs", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("actor_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("action", sa.VARCHAR(length=100), nullable=False),
    sa.Column("entity_type", sa.VARCHAR(length=100), nullable=False),
    sa.Column("entity_id", sa.VARCHAR(length=100)),
    sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("ip_address", postgresql.INET()),
    sa.Column("user_agent", sa.TEXT()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Index("idx_audit_logs_actor", "actor_user_id"),
    sa.Index("idx_audit_logs_created_at", sa.text("created_at DESC")),
)

login_activity_logs = sa.Table(
    "login_activity_logs", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('login_activity_logs_id_seq'::regclass)")),
    sa.Column("email", sa.VARCHAR(length=255), nullable=False),
    sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("success", sa.BOOLEAN(), nullable=False),
    sa.Column("failure_reason", sa.TEXT()),
    sa.Column("ip_address", sa.VARCHAR(length=100)),
    sa.Column("user_agent", sa.TEXT()),
    sa.Column("logout_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Index("idx_login_activity_logs_user_id", "user_id"),
    sa.Index("idx_login_activity_logs_created_at", sa.text("created_at DESC")),
)

notifications = sa.Table(
    "notifications", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('notifications_id_seq'::regclass)")),
    sa.Column("user_id", sa.VARCHAR(length=255), nullable=False),
    sa.Column("type", sa.VARCHAR(length=100), nullable=False),
    sa.Column("title", sa.VARCHAR(length=255), nullable=False),
    sa.Column("message", sa.TEXT(), nullable=False),
    sa.Column("link_url", sa.VARCHAR(length=500)),
    sa.Column("entity_type", sa.VARCHAR(length=100)),
    sa.Column("entity_id", sa.VARCHAR(length=100)),
    sa.Column("sender_name", sa.VARCHAR(length=255)),
    sa.Column("sender_avatar", sa.VARCHAR(length=500)),
    sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("is_read", sa.BOOLEAN(), server_default=sa.text("false")),
    sa.Column("read_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Index("idx_notifications_user_id", "user_id"),
    sa.Index("idx_notifications_is_read", "is_read"),
)

workflow_assignments = sa.Table(
    "workflow_assignments", metadata,
    sa.Column("id", sa.INTEGER(), primary_key=True, server_default=sa.text("nextval('workflow_assignments_id_seq'::regclass)")),
    sa.Column("role", sa.VARCHAR(length=100), nullable=False),
    sa.Column("user_id", sa.UUID()),
    sa.Column("user_ids", postgresql.ARRAY(sa.UUID()), server_default=sa.text("ARRAY[]::uuid[]")),
    sa.Column("team_id", sa.UUID()),
    sa.Column("request_type", sa.VARCHAR(length=100)),
    sa.Column("active", sa.BOOLEAN(), server_default=sa.text("true")),
    sa.Column("effective_from", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    sa.Column("effective_to", postgresql.TIMESTAMP(timezone=True)),
    sa.UniqueConstraint("role", "request_type", name="workflow_assignments_role_request_type_key"),
)


# --- Marketing reference data (taxonomy from Marketing's channel sheet) ---

def _reference_table(name: str, *extra: sa.Column | sa.Constraint) -> sa.Table:
    return sa.Table(
        name, metadata,
        sa.Column("id", sa.INTEGER(), primary_key=True),
        sa.Column("code", sa.VARCHAR(length=50), nullable=False),
        sa.Column("name", sa.VARCHAR(length=100), nullable=False),
        *extra,
        sa.Column("sort_order", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.BOOLEAN(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("code", name=f"{name}_code_key"),
    )


brands = _reference_table("brands")
channels = _reference_table("channels")
sub_channels = _reference_table("sub_channels")
funnel_stages = _reference_table("funnel_stages")
goals = _reference_table(
    "goals",
    # Rule 1: each goal belongs to exactly one funnel stage
    sa.Column("funnel_stage_id", sa.INTEGER(), sa.ForeignKey("funnel_stages.id", name="goals_funnel_stage_id_fkey"), nullable=False),
    # Target for initiatives' (goal, funnel stage) foreign key, which enforces Rule 1
    sa.UniqueConstraint("id", "funnel_stage_id", name="goals_id_funnel_stage_id_key"),
)
primary_kpis = _reference_table("primary_kpis")
performance_ratings = _reference_table("performance_ratings", sa.Column("description", sa.TEXT()))
scale_or_cut_options = _reference_table("scale_or_cut_options", sa.Column("description", sa.TEXT()))
# Used by event details
regions = _reference_table("regions")
event_types = _reference_table("event_types")

# Classifying costs: expense types group categories for reporting; a category is picked on each cost
expense_types = _reference_table(
    "expense_types",
    sa.Column("description", sa.TEXT()),
    # Where costs of this type come from: entered in this portal, or fed by the travel management system
    sa.Column("source", sa.VARCHAR(length=20), nullable=False),
    sa.CheckConstraint("source IN ('PORTAL', 'TRAVEL_SYSTEM')", name="expense_types_source_check"),
)
spend_categories = _reference_table(
    "spend_categories",
    sa.Column("expense_type_id", sa.INTEGER(), sa.ForeignKey("expense_types.id", name="spend_categories_expense_type_id_fkey"), nullable=False),
    sa.Column("applies_to", sa.VARCHAR(length=20), nullable=False, server_default=sa.text("'ALL'")),
    # False for categories that only arrive from another system (e.g. travel)
    sa.Column("manual_entry_allowed", sa.BOOLEAN(), nullable=False, server_default=sa.text("true")),
    sa.Column("default_cost_type", sa.VARCHAR(length=5)),
    # Shown under the category picker
    sa.Column("help_text", sa.VARCHAR(length=255)),
    sa.CheckConstraint("applies_to IN ('ALL', 'EVENTS', 'ADS')", name="spend_categories_applies_to_check"),
    sa.CheckConstraint("default_cost_type IN ('OPEX', 'CAPEX')", name="spend_categories_default_cost_type_check"),
)

channel_sub_channels = sa.Table(
    "channel_sub_channels", metadata,
    sa.Column("channel_id", sa.INTEGER(), sa.ForeignKey("channels.id", name="channel_sub_channels_channel_id_fkey", ondelete="CASCADE"), primary_key=True),
    sa.Column("sub_channel_id", sa.INTEGER(), sa.ForeignKey("sub_channels.id", name="channel_sub_channels_sub_channel_id_fkey", ondelete="CASCADE"), primary_key=True),
)


# --- Marketing initiatives (any channel) ---

initiatives = sa.Table(
    "initiatives", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("name", sa.VARCHAR(length=255), nullable=False),
    sa.Column("brand_id", sa.INTEGER(), sa.ForeignKey("brands.id", name="initiatives_brand_id_fkey"), nullable=False),
    sa.Column("channel_id", sa.INTEGER(), sa.ForeignKey("channels.id", name="initiatives_channel_id_fkey"), nullable=False),
    sa.Column("sub_channel_id", sa.INTEGER(), sa.ForeignKey("sub_channels.id", name="initiatives_sub_channel_id_fkey")),
    sa.Column("owner_user_id", sa.UUID(), sa.ForeignKey("users.id", name="initiatives_owner_user_id_fkey", ondelete="SET NULL")),
    sa.Column("start_date", sa.DATE()),
    sa.Column("end_date", sa.DATE()),
    sa.Column("budget_amount", sa.NUMERIC(precision=14, scale=2)),
    sa.Column("currency", sa.CHAR(length=3), nullable=False, server_default=sa.text("'USD'::bpchar")),
    sa.Column("status", sa.VARCHAR(length=20), nullable=False, server_default=sa.text("'PLANNED'::character varying")),
    sa.Column("funnel_stage_id", sa.INTEGER(), sa.ForeignKey("funnel_stages.id", name="initiatives_funnel_stage_id_fkey")),
    sa.Column("goal_id", sa.INTEGER(), sa.ForeignKey("goals.id", name="initiatives_goal_id_fkey")),
    sa.Column("primary_kpi_id", sa.INTEGER(), sa.ForeignKey("primary_kpis.id", name="initiatives_primary_kpi_id_fkey")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", name="initiatives_created_by_fkey", ondelete="SET NULL")),
    sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id", name="initiatives_updated_by_fkey", ondelete="SET NULL")),
    # The sub-channel must be one that belongs to the chosen channel
    sa.ForeignKeyConstraint(
        ["channel_id", "sub_channel_id"],
        ["channel_sub_channels.channel_id", "channel_sub_channels.sub_channel_id"],
        name="initiatives_channel_sub_channel_fkey",
    ),
    # Rule 1: the goal must belong to the chosen funnel stage
    sa.ForeignKeyConstraint(
        ["goal_id", "funnel_stage_id"],
        ["goals.id", "goals.funnel_stage_id"],
        name="initiatives_goal_funnel_stage_fkey",
    ),
    sa.CheckConstraint("goal_id IS NULL OR funnel_stage_id IS NOT NULL", name="initiatives_goal_requires_stage_check"),
    sa.CheckConstraint(
        "status IN ('PLANNED', 'ACTIVE', 'COMPLETED', 'PAUSED', 'CANCELLED')",
        name="initiatives_status_check",
    ),
    sa.CheckConstraint("end_date IS NULL OR start_date IS NULL OR end_date >= start_date", name="initiatives_dates_check"),
    sa.CheckConstraint("budget_amount IS NULL OR budget_amount >= 0", name="initiatives_budget_check"),
    sa.Index("idx_initiatives_channel", "channel_id"),
    sa.Index("idx_initiatives_brand", "brand_id"),
    sa.Index("idx_initiatives_status", "status"),
)


# --- Event details (one row per event initiative, in-person or virtual) ---

# Codes look like EVT-2026-0007: the year the row was created + a global counter. They are generated in
# the database by next_event_code() (reads event_code_seq once), so concurrent creates can't collide.
# The sequence and function are created by hand in the event_details migration, because Alembic
# autogenerate doesn't track either.
event_details = sa.Table(
    "event_details", metadata,
    sa.Column("initiative_id", sa.UUID(), sa.ForeignKey("initiatives.id", name="event_details_initiative_id_fkey", ondelete="CASCADE"), primary_key=True),
    sa.Column("event_code", sa.VARCHAR(length=20), nullable=False, server_default=sa.text("next_event_code()")),
    sa.Column("event_type_id", sa.INTEGER(), sa.ForeignKey("event_types.id", name="event_details_event_type_id_fkey")),
    sa.Column("venue", sa.VARCHAR(length=255)),
    sa.Column("city", sa.VARCHAR(length=120)),
    sa.Column("country_code", sa.CHAR(length=2)),
    sa.Column("region_id", sa.INTEGER(), sa.ForeignKey("regions.id", name="event_details_region_id_fkey")),
    # Virtual events and webinars only (e.g. "Zoom", "ON24")
    sa.Column("platform", sa.VARCHAR(length=80)),
    sa.Column("event_url", sa.VARCHAR(length=500)),
    # In-person events only: lets costs be compared across events (cost per m²)
    sa.Column("booth_size_sqm", sa.NUMERIC(precision=8, scale=2)),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", name="event_details_created_by_fkey", ondelete="SET NULL")),
    sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id", name="event_details_updated_by_fkey", ondelete="SET NULL")),
    sa.UniqueConstraint("event_code", name="event_details_event_code_key"),
    sa.CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="event_details_country_code_check"),
    sa.CheckConstraint("event_url ~* '^https?://'", name="event_details_event_url_check"),
    sa.CheckConstraint("booth_size_sqm > 0", name="event_details_booth_size_check"),
)


# --- Spreadsheet uploads ---
# One upload, held between preview and confirm: the parsed, validated rows wait in staged_rows until
# someone confirms them (then they become real rows, e.g. spend lines) or the preview expires.
import_batches = sa.Table(
    "import_batches", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("kind", sa.VARCHAR(length=30), nullable=False),
    # Set for uploads that belong to one initiative, like a cost sheet
    sa.Column("initiative_id", sa.UUID(), sa.ForeignKey("initiatives.id", name="import_batches_initiative_id_fkey", ondelete="CASCADE")),
    sa.Column("filename", sa.VARCHAR(length=255), nullable=False),
    sa.Column("status", sa.VARCHAR(length=20), nullable=False, server_default=sa.text("'PREVIEW'")),
    sa.Column("total_rows", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
    sa.Column("ready_rows", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
    sa.Column("error_rows", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
    sa.Column("duplicate_rows", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
    sa.Column("imported_rows", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
    sa.Column("staged_rows", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", name="import_batches_created_by_fkey", ondelete="SET NULL")),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("confirmed_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("confirmed_by", sa.UUID(), sa.ForeignKey("users.id", name="import_batches_confirmed_by_fkey", ondelete="SET NULL")),
    sa.Column("undone_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("undone_by", sa.UUID(), sa.ForeignKey("users.id", name="import_batches_undone_by_fkey", ondelete="SET NULL")),
    sa.CheckConstraint("kind IN ('COST_SHEET', 'EVENTS', 'GOOGLE_ADS', 'MICROSOFT_ADS')", name="import_batches_kind_check"),
    sa.CheckConstraint("status IN ('PREVIEW', 'CONFIRMED', 'UNDONE', 'EXPIRED')", name="import_batches_status_check"),
    sa.Index("idx_import_batches_initiative", "initiative_id"),
)


# --- Costs against an initiative ---
# Supplier costs move ESTIMATED → COMMITTED → INVOICED → PAID. Travel and anything staff claim back
# come from the travel management system (source TRAVEL_FEED) and are never typed in here.
# Invoices: only the supplier's invoice number is stored, never the file — Finance keeps the invoices.
spend_lines = sa.Table(
    "spend_lines", metadata,
    sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("initiative_id", sa.UUID(), sa.ForeignKey("initiatives.id", name="spend_lines_initiative_id_fkey", ondelete="CASCADE"), nullable=False),
    sa.Column("category_id", sa.INTEGER(), sa.ForeignKey("spend_categories.id", name="spend_lines_category_id_fkey"), nullable=False),
    sa.Column("description", sa.VARCHAR(length=255)),
    sa.Column("vendor", sa.VARCHAR(length=255)),
    sa.Column("note", sa.TEXT()),
    sa.Column("status", sa.VARCHAR(length=20), nullable=False, server_default=sa.text("'COMMITTED'")),
    # The original estimate or quote, kept to show the variance once the actual is known
    sa.Column("estimated_amount", sa.NUMERIC(precision=14, scale=2)),
    # The current figure: the estimate until invoiced, then the actual. Negative for refunds and credits.
    sa.Column("amount", sa.NUMERIC(precision=14, scale=2), nullable=False),
    sa.Column("currency", sa.CHAR(length=3), nullable=False),
    # Invoice date, or the expected date while ESTIMATED / COMMITTED; decides the month the cost counts in
    sa.Column("incurred_on", sa.DATE(), nullable=False),
    sa.Column("invoice_ref", sa.VARCHAR(length=100)),
    sa.Column("paid_on", sa.DATE()),
    sa.Column("payment_method", sa.VARCHAR(length=20)),
    sa.Column("cost_type", sa.VARCHAR(length=5)),
    sa.Column("source", sa.VARCHAR(length=30), nullable=False, server_default=sa.text("'MANUAL'")),
    # The source system's id for this cost, so feeds and imports never create duplicates
    sa.Column("external_ref", sa.VARCHAR(length=100)),
    # Filled by the monthly reconciliation (later task)
    sa.Column("reconciled_month", sa.DATE()),
    sa.Column("reconciled_at", postgresql.TIMESTAMP(timezone=True)),
    sa.Column("reconciled_by", sa.UUID(), sa.ForeignKey("users.id", name="spend_lines_reconciled_by_fkey", ondelete="SET NULL")),
    # The spreadsheet upload that created the line (kept if the batch record is removed)
    sa.Column("import_batch_id", sa.UUID(), sa.ForeignKey("import_batches.id", name="spend_lines_import_batch_id_fkey", ondelete="SET NULL")),
    # Foreign key added by a later task: the monthly budget the line counts against
    sa.Column("monthly_budget_id", sa.UUID()),
    sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", name="spend_lines_created_by_fkey", ondelete="SET NULL")),
    sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id", name="spend_lines_updated_by_fkey", ondelete="SET NULL")),
    sa.CheckConstraint("amount <> 0", name="spend_lines_amount_check"),
    sa.CheckConstraint("status <> 'PAID' OR paid_on IS NOT NULL", name="spend_lines_paid_on_check"),
    sa.CheckConstraint("status IN ('ESTIMATED', 'COMMITTED', 'INVOICED', 'PAID')", name="spend_lines_status_check"),
    sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="spend_lines_currency_check"),
    sa.CheckConstraint(
        "source IN ('MANUAL', 'STAFF_CALCULATOR', 'COST_SHEET_UPLOAD', 'TRAVEL_FEED', 'GOOGLE_ADS', 'MICROSOFT_ADS')",
        name="spend_lines_source_check",
    ),
    sa.CheckConstraint("payment_method IN ('AP_INVOICE', 'CORPORATE_CARD', 'OTHER')", name="spend_lines_payment_method_check"),
    sa.CheckConstraint("cost_type IN ('OPEX', 'CAPEX')", name="spend_lines_cost_type_check"),
    # Only invoiced or paid costs can be reconciled
    sa.CheckConstraint("reconciled_month IS NULL OR status IN ('INVOICED', 'PAID')", name="spend_lines_reconciled_status_check"),
    sa.Index("idx_spend_lines_initiative", "initiative_id"),
    # One row per record in a source system
    sa.Index("spend_lines_source_ref_key", "source", "external_ref", unique=True, postgresql_where=sa.text("external_ref IS NOT NULL")),
    # The same supplier invoice can't be entered twice
    sa.Index(
        "spend_lines_vendor_invoice_key", sa.text("lower(vendor)"), "invoice_ref", unique=True,
        postgresql_where=sa.text("vendor IS NOT NULL AND invoice_ref IS NOT NULL"),
    ),
)
