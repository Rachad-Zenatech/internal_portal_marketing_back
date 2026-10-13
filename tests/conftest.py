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


@pytest.fixture(scope="session")
def client():
    if not _database_available():
        pytest.skip("Local database not available")
    from fastapi.testclient import TestClient

    from server import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def ref():
    """Reference-data ids by code, e.g. ref['channels']['EVENTS']."""
    if not _database_available():
        pytest.skip("Local database not available")
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
    run_sql("DELETE FROM initiatives WHERE created_by = $1", user_id)
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
