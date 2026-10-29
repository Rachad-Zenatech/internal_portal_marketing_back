# Antigravity Project Instructions

Use these rules when changing the enterprise backend or its internal portal.

<!-- project-structure:start -->
## Project Structure

```text
server.py             # FastAPI app: startup/shutdown, middleware, router registration
tools/                # HTTP routers (FastAPI APIRouter), grouped by feature
  shared/             # Auth, roles and permissions, logs, health/observability and search, plus shared marketing domain routes: demos, spend lines, cost sheet imports
    auth_router.py, demo_router.py, import_router.py
    log_router.py, observability.py, rbac_router.py
    search.py, spend_router.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    notification_router.py
  ads/                # Paid ads overview and uploads, Google/Microsoft Ads clients and sync, ad platform OAuth
    ads_router.py, google_ads_router.py, microsoft_ads_router.py
    oauth_router.py
  budgets/            # Marketing budgets and budget spreadsheet uploads
    budget_router.py
  events/             # Event management (team, travel) and the Events Overview (spend, results, ROI)
    event_management_router.py
  initiatives/        # Initiatives (CRUD, summary, owners), activity feed and bulk event budget uploads
    initiative_router.py
  microsoft_graph/    # Microsoft Graph (Entra users/org) client and background sync
    graph_router.py
  navan/              # Navan booking API client, demo bookings and the Navan cost sync into event spend
    navan_router.py
  performance/        # Results per month, success settings, scale-or-cut decisions and the performance view
    performance_router.py
  reference_data/     # Marketing reference lists (brands, channels, funnel stages, KPIs), cached
    reference_data_router.py
services/             # Business logic, grouped by feature
  shared/             # Used by several features: auth, RBAC, caching, logging, audit, security
                      # plus core marketing domain used across features: initiatives, spend lines,
                      # ads results, demos, spreadsheet import parsing/cost sheets, travel cost rules
    ads_service.py, audit_service.py, auth_service.py
    cache_service.py, cost_sheet_import_service.py, demo_service.py
    import_parsing.py, initiative_metrics.py, initiative_service.py
    logging_service.py, rbac_service.py, service_security_policy.py
    spend_service.py, travel_cost_rules.py
  ai/                 # Gemini clients, assistant chat, guardrails, tracing, semantic cache, usage limits
    ai_guardrails.py, ai_observability.py, ai_usage_service.py
    assistant_chat_service.py, gemini_service.py, model_router.py
    semantic_cache.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    mqtt_presence.py, notification_service.py, rabbitmq_service.py
  ads/                # Paid ads overview and uploads, Google/Microsoft Ads clients and sync, ad platform OAuth
    ad_platform_sync.py, ads_import_service.py, ads_overview_service.py
    google_ads_client.py, google_ads_mock_data.py, google_ads_sync_job.py
    microsoft_ads_client.py, microsoft_ads_mock_data.py, microsoft_ads_sync_job.py
    oauth_connection_service.py, token_crypto_service.py
    oauth_adapters/ (package): base.py, google_ads.py, linkedin_ads.py, meta_ads.py, microsoft_ads.py, social_adapters.py
  budgets/            # Marketing budgets and budget spreadsheet uploads
    budget_import_service.py, budget_service.py
  events/             # Event management (team, travel) and the Events Overview (spend, results, ROI)
    event_management_service.py, event_performance_service.py
  initiatives/        # Initiative activity feed and bulk event budget uploads
    event_budget_import_service.py, initiative_activity_service.py
  microsoft_graph/    # Microsoft Graph (Entra users/org) client and background sync
    graph_service.py, graph_sync_job.py
  navan/              # Navan booking API client, demo bookings and the Navan cost sync into event spend
    navan_cost_sync_job.py, navan_demo_service.py, navan_service.py
  performance/        # Results per month, success settings, scale-or-cut decisions and the performance view
    performance_service.py
  reference_data/     # Marketing reference lists (brands, channels, funnel stages, KPIs), cached
    reference_data_service.py
models/               # Pydantic request/response models, grouped by feature
  shared/             # Used by several features: users, roles, permissions, common types
                      # plus the shared marketing domain: initiatives, channel details, spend lines,
                      # demos, cost sheet imports
    channel_details_model.py, demo_model.py, import_model.py
    initiative_model.py, rbac_model.py, spend_model.py
  notifications/      # Notifications and messaging (SSE, RabbitMQ, MQTT, cross-portal events)
    notification_model.py
  ads/                # Paid ads overview and uploads, Google/Microsoft Ads clients and sync, ad platform OAuth
    ads_model.py, google_ads_model.py, microsoft_ads_model.py
    oauth_connection_model.py
  budgets/            # Marketing budgets and budget spreadsheet uploads
    budget_model.py
  events/             # Event management (team, travel) and the Events Overview (spend, results, ROI)
    event_management_model.py, event_performance_model.py
  initiatives/        # Initiative activity feed and bulk event budget uploads
    event_budget_import_model.py
  navan/              # Navan booking API client, demo bookings and the Navan cost sync into event spend
    navan_model.py
  performance/        # Results per month, success settings, scale-or-cut decisions and the performance view
    performance_model.py
  reference_data/     # Marketing reference lists (brands, channels, funnel stages, KPIs), cached
    reference_data_model.py
mcp_tools/            # MCP server: registry, middleware, prompts; tools grouped by feature
    __init__.py (registry), middleware.py, prompts.py
  shared/             # Tools for several features: permissions and account lookups
    permissions.py
postgresql_db/        # database.py (asyncpg pool), schema_metadata.py, table scripts in schemas/
  schemas/            # setup_<feature>_schema.py: CREATE TABLE / migrations per feature
    notifications_schema.py, setup_ai_observability_schema.py, setup_graph_sync_schema.py
    setup_pbac_schema.py
alembic/              # Database migrations
scripts/              # Dev/ops scripts (seed, reset, pull prod data)
docs/                 # Longer design and integration docs
```

### Where new code goes

* **New feature `<x>`:** logic in `services/<x>/`, endpoints in `tools/<x>/<x>_router.py`, models in `models/<x>/`, MCP tools in `mcp_tools/<x>/` (register them in `mcp_tools/__init__.py`), tables in `postgresql_db/schemas/setup_<x>_schema.py` (run with `python -m postgresql_db.schemas.setup_<x>_schema`).
* **Code two or more features use** goes in the `shared/` folder of that layer (or `ai/`, `notifications/` when it fits there). A feature folder must not import another feature's folder; move the common piece to `shared/` instead.
* **No loose files** at the root of `tools/`, `services/` or `models/`. In `mcp_tools/` only the registry (`__init__.py`), `middleware.py` and `prompts.py` sit at the root.
* **Imports are absolute** (`from services.shared.auth_service import ...`). Feature folders are namespace packages with no `__init__.py`, like the rest of the repo (the one exception is the existing `services/ads/oauth_adapters/` package).
* **When you move a module,** update every import, `mock.patch("...")` string, `python -m` command (CI, scripts) and doc path, and fix any `__file__`-relative path in it (one more folder deep means one more `dirname`).
<!-- project-structure:end -->

## Workspace & Stack

- Backend: `internal_portal_administration_backend` (`server.py`, `tools/`, `services/`, `models/`, `postgresql_db/`)
- FastAPI routers belong in `tools/`, async business and database logic in `services/`, Pydantic schemas in `models/`, and database helpers in `postgresql_db/database.py`.
- Keep backend request and response contracts synchronized with frontend `src/types`.

## Core Architecture & Services

### Database Connection Pool (`postgresql_db/database.py`)
- **Synchronous Pool Access:** `get_pool()` is a synchronous helper returning `_pool: asyncpg.Pool`. **NEVER** write `await get_pool()`.
- **Acquiring Connections:** Always use `pool = get_pool()` followed by `async with pool.acquire() as conn:`.
- **Configurable Limits:** Pool parameters (`DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_MAX_QUERIES`, `DB_POOL_MAX_INACTIVE_LIFETIME`, `DB_COMMAND_TIMEOUT`, `DATABASE_SSL`) must be loaded from `.env` with fallback defaults.

### Tier 1 In-Memory Caching & Event Invalidation (`services/shared/cache_service.py`)
- **Local In-Memory Cache:**
  - `permission_cache` (60s TTL) for `get_my_permissions(user_id)`.
  - `master_data_cache` (300s TTL) for reference/master data.
  - `config_cache` (300s TTL) for `get_all_roles()`, `get_role_tree()`, `get_permission_modules()`.
- **Distributed PostgreSQL LISTEN/NOTIFY Pub/Sub:**
  - Whenever a mutation occurs (CRUD on users, roles, permissions, master data), **always** call `await emit_cache_invalidation(scope="...", key="...")`.
  - Do not bypass the event system with raw in-memory mutations.

## Database & SQL Performance Standards

### Eliminating N+1 Queries
- **NEVER execute SQL queries inside loops.**
- Use PostgreSQL `UNNEST` or single atomic batch statements for multi-row operations:
  ```python
  # Good (Batch atomic update)
  await conn.execute("""
      INSERT INTO user_roles (user_id, role_id, assigned_at, assigned_by, is_active)
      SELECT $1, unnest($2::uuid[]), now(), $3, true
      ON CONFLICT (user_id, role_id)
      DO UPDATE SET is_active = true, assigned_at = now()
  """, user_id, role_ids, actor_id)
  ```
- Use asyncpg placeholders `$1, $2, ...`. Never interpolate user-supplied values into SQL.
- Use transactions for related writes, imports, saves, and other atomic operations.

## Validation & Cleanup

- After backend changes, run `wsl python3 -m py_compile <touched files>`.
- Run `git diff --check` on touched files before reporting completion.
- Any task-specific test, probe, fixture, snapshot, generated output, or scratch file created by an LLM is temporary by default. Run validation and remove temporary files before finishing.
- Never delete or modify pre-existing repository tests merely as cleanup.
