"""Marketing reference data (brands, channels, funnel stages, goals, KPIs, ratings).

Read-mostly lookup lists, served from master_data_cache. Any future write to these
tables must call emit_cache_invalidation(scope="master_data", key=REFERENCE_DATA_CACHE_KEY).
"""
from postgresql_db.database import get_pool
from services.cache_service import master_data_cache

REFERENCE_DATA_CACHE_KEY = "reference_data"
_CACHE_TTL_SECONDS = 300

_SIMPLE_LISTS = {
    "brands": "SELECT id, code, name, sort_order FROM brands WHERE is_active ORDER BY sort_order, name",
    "sub_channels": "SELECT id, code, name, sort_order FROM sub_channels WHERE is_active ORDER BY sort_order, name",
    "funnel_stages": "SELECT id, code, name, sort_order FROM funnel_stages WHERE is_active ORDER BY sort_order, name",
    "primary_kpis": "SELECT id, code, name, sort_order FROM primary_kpis WHERE is_active ORDER BY sort_order, name",
    "performance_ratings": (
        "SELECT id, code, name, description, sort_order FROM performance_ratings "
        "WHERE is_active ORDER BY sort_order, name"
    ),
    "scale_or_cut_options": (
        "SELECT id, code, name, description, sort_order FROM scale_or_cut_options "
        "WHERE is_active ORDER BY sort_order, name"
    ),
    "goals": (
        "SELECT id, code, name, funnel_stage_id, sort_order FROM goals "
        "WHERE is_active ORDER BY sort_order, name"
    ),
    "regions": "SELECT id, code, name, sort_order FROM regions WHERE is_active ORDER BY sort_order, name",
    "event_types": "SELECT id, code, name, sort_order FROM event_types WHERE is_active ORDER BY sort_order, name",
}

_CHANNELS_SQL = """
    SELECT c.id, c.code, c.name, c.sort_order,
           COALESCE(
               array_agg(s.id ORDER BY s.sort_order) FILTER (WHERE s.id IS NOT NULL),
               '{}'::int[]
           ) AS sub_channel_ids
    FROM channels c
    LEFT JOIN channel_sub_channels l ON l.channel_id = c.id
    LEFT JOIN sub_channels s ON s.id = l.sub_channel_id AND s.is_active
    WHERE c.is_active
    GROUP BY c.id
    ORDER BY c.sort_order, c.name
"""


async def get_reference_data() -> dict:
    cached = await master_data_cache.get(REFERENCE_DATA_CACHE_KEY)
    if cached is not None:
        return cached

    pool = get_pool()
    async with pool.acquire() as conn:
        result = {key: [dict(r) for r in await conn.fetch(sql)] for key, sql in _SIMPLE_LISTS.items()}
        result["channels"] = [
            {**dict(r), "sub_channel_ids": list(r["sub_channel_ids"])}
            for r in await conn.fetch(_CHANNELS_SQL)
        ]

    await master_data_cache.set(REFERENCE_DATA_CACHE_KEY, result, ttl_seconds=_CACHE_TTL_SECONDS)
    return result
