"""Shared fixtures for API tests.

These tests need the local Postgres (docker compose up -d db) with migrations applied.
When no database is reachable — e.g. the CI lint job — they are skipped, not failed.
"""
import asyncio
import os
import sys
import uuid

import asyncpg
import pytest
from dotenv import load_dotenv

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(ROOT, ".env.local"), override=True)
# Dev sign-in is only available outside production
os.environ.setdefault("APP_ENV", "development")
# Every test signs in fresh users from one address, which would trip the per-minute sign-in limit.
# Raise it for the test run only (read when server.py is imported); real limits are unchanged.
for _limit in ("RATE_LIMIT_AUTH_PER_WINDOW", "RATE_LIMIT_REQUESTS_PER_WINDOW", "RATE_LIMIT_IP_CEILING_PER_WINDOW"):
    os.environ.setdefault(_limit, "1000000")
# database.py reloads .env and .env.local (with override) when first imported, so import it now and force the
# test-only settings after it: tests never connect to brokers or call a real travel system from the background loop
import postgresql_db.database  # noqa: E402,F401

os.environ["MQTT_PRESENCE_ENABLED"] = "false"
os.environ["RABBITMQ_ENABLED"] = "false"
os.environ["TRAVEL_COST_SYNC_ENABLED"] = "false"

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def run_sql(sql: str, *args):
    """Run one statement on its own connection and return the rows."""

    async def _run():
        conn = await asyncpg.connect(DATABASE_URL, timeout=5)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(_run())


def _database_available() -> bool:
    if not DATABASE_URL:
        return False
    try:
        run_sql("SELECT 1 FROM initiatives LIMIT 1")
        return True
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


def _skip_without_database() -> None:
    if _database_available():
        return
    # CI sets this so a broken database fails the build instead of silently skipping every test
    if os.environ.get("REQUIRE_TEST_DATABASE") == "1":
        pytest.fail("Test database not available but REQUIRE_TEST_DATABASE=1")
    pytest.skip("Local database not available")


@pytest.fixture(scope="session")
def client():
    _skip_without_database()
    from fastapi.testclient import TestClient

    from server import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def ref():
    """Reference-data ids by code, e.g. ref['channels']['EVENTS']."""
    _skip_without_database()
    tables = ["brands", "channels", "sub_channels", "funnel_stages", "goals", "primary_kpis"]
    return {t: {r["code"]: r["id"] for r in run_sql(f"SELECT id, code FROM {t}")} for t in tables}


def _make_user(*, super_admin: bool) -> uuid.UUID:
    email = f"pytest.{uuid.uuid4().hex[:10]}@zenatech.com"
    rows = run_sql(
        "INSERT INTO users (email, full_name, is_active, is_super_admin) VALUES ($1, $2, true, $3) RETURNING id",
        email,
        "Pytest User",
        super_admin,
    )
    user_id = rows[0]["id"]
    if not super_admin:
        # A regular role with no Marketing permissions
        run_sql(
            "INSERT INTO user_roles (user_id, role_id, is_active) SELECT $1, id, true FROM roles WHERE code = 'USER'",
            user_id,
        )
    return user_id


def _token_for(client, user_id: uuid.UUID) -> dict:
    email = run_sql("SELECT email FROM users WHERE id = $1", user_id)[0]["email"]
    response = client.post("/api/auth/dev-login", json={"email": email})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _delete_user(user_id: uuid.UUID) -> None:
    # Everything the test user did, so local audit logs don't fill up with test entries
    run_sql("DELETE FROM audit_logs WHERE actor_user_id = $1 OR entity_id = $2", user_id, str(user_id))
    run_sql("DELETE FROM login_activity_logs WHERE user_id = $1", user_id)
    run_sql("DELETE FROM import_batches WHERE created_by = $1", user_id)
    run_sql("DELETE FROM initiatives WHERE created_by = $1", user_id)
    # Temporary roles a test made for this user (code PYTEST_…); their permission rows go with them
    run_sql(
        "DELETE FROM roles WHERE code LIKE 'PYTEST\\_%' AND id IN (SELECT role_id FROM user_roles WHERE user_id = $1)",
        user_id,
    )
    run_sql("DELETE FROM user_roles WHERE user_id = $1", user_id)
    run_sql("DELETE FROM users WHERE id = $1", user_id)


@pytest.fixture
def admin(client):
    """Headers for a temporary super admin; it and anything it created are removed afterwards."""
    user_id = _make_user(super_admin=True)
    try:
        yield {"id": user_id, "headers": _token_for(client, user_id)}
    finally:
        _delete_user(user_id)


@pytest.fixture
def viewer(client):
    """Headers for a temporary regular user without Marketing permissions."""
    user_id = _make_user(super_admin=False)
    try:
        yield {"id": user_id, "headers": _token_for(client, user_id)}
    finally:
        _delete_user(user_id)
