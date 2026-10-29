# GitHub Copilot Instructions

- Use FastAPI routers in `tools/`, services in `services/`, models in `models/`, and database helpers in `postgresql_db/database.py`.
- Synchronous database pool access: `pool = get_pool()`.
- Use the caching layer (`services.shared.cache_service`) for high-frequency queries and emit invalidation events on data modification.
- Never write SQL queries inside loops; batch multi-row operations using PostgreSQL `UNNEST`.
- Validate code with `wsl python3 -m py_compile <files>`.

<!-- project-structure:start -->
## Project Structure

```text
server.py             # FastAPI app: startup/shutdown, middleware, router registration
tools/                # HTTP routers (FastAPI APIRouter), one file per feature
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
postgresql_db/        # database.py (asyncpg pool), schema_metadata.py; table scripts in schemas/
  schemas/            # setup_<feature>_schema.py: CREATE TABLE / migrations per feature
    notifications_schema.py, setup_ai_observability_schema.py, setup_graph_sync_schema.py
    setup_pbac_schema.py
alembic/              # Database migrations
scripts/              # Dev/ops scripts (seed, reset, pull prod data)
docs/                 # Longer design and integration docs
```

### Where new code goes

* **New feature `<x>`:** logic in `services/<x>/`, endpoints in `tools/<x>_router.py`, models in `models/<x>/`, MCP tools in `mcp_tools/<x>/` (register them in `mcp_tools/__init__.py`), tables in `postgresql_db/schemas/setup_<x>_schema.py` (run with `python -m postgresql_db.schemas.setup_<x>_schema`).
* **Code two or more features use** goes in the `shared/` folder of that layer (or `ai/`, `notifications/` when it fits there). A feature folder must not import another feature's folder; move the common piece to `shared/` instead.
* **No loose files** at the root of `services/` or `models/`. In `mcp_tools/` only the registry (`__init__.py`), `middleware.py` and `prompts.py` sit at the root.
* **Imports are absolute** (`from services.shared.auth_service import ...`). Feature folders are namespace packages with no `__init__.py`, like the rest of the repo (the one exception is the existing `services/ads/oauth_adapters/` package).
* **When you move a module,** update every import, `mock.patch("...")` string, `python -m` command (CI, scripts) and doc path, and fix any `__file__`-relative path in it (one more folder deep means one more `dirname`).
<!-- project-structure:end -->
