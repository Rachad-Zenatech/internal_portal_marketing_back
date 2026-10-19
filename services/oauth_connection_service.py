"""OAuth Connection Management Service

Supports hierarchical multi-entity OAuth:
Organization -> Companies/Entities -> OAuth connections
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import asyncpg
import requests

from models.oauth_connection_model import (
    Company,
    CompanyCreate,
    OAuthConnection,
    OAuthConnectionCreate,
    OAuthConnectionUpdate,
    OAuthProvider,
    Organization,
    OrganizationCreate,
    ResolvedConnection,
)
from postgresql_db.database import get_pool
from services import token_crypto_service as crypto

logger = logging.getLogger(__name__)

# Provider token refresh endpoints
PROVIDER_TOKEN_URLS = {
    # Paid Ads
    "GOOGLE_ADS": "https://oauth2.googleapis.com/token",
    "MICROSOFT_ADS": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    "META_ADS": "https://graph.facebook.com/v19.0/oauth/access_token",
    "LINKEDIN_ADS": "https://www.linkedin.com/oauth/v2/accessToken",
    # Organic Social Media
    "LINKEDIN_PAGE": "https://www.linkedin.com/oauth/v2/accessToken",
    "X_TWITTER": "https://api.twitter.com/2/oauth2/token",
    "FACEBOOK_PAGE": "https://graph.facebook.com/v19.0/oauth/access_token",
    "INSTAGRAM": "https://graph.facebook.com/v19.0/oauth/access_token",
    "YOUTUBE": "https://oauth2.googleapis.com/token",
    "TIKTOK": "https://open.tiktokapis.com/v2/oauth/token/",
}


# ==============================================================================
# Organizations & Companies CRUD
# ==============================================================================

async def list_organizations() -> list[Organization]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, slug, description, created_at, updated_at
            FROM organizations
            ORDER BY name ASC
        """)
        return [Organization(**dict(r)) for r in rows]


async def get_organization(organization_id: UUID) -> Organization | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, name, slug, description, created_at, updated_at
            FROM organizations
            WHERE id = $1
        """, organization_id)
        return Organization(**dict(row)) if row else None


async def create_organization(data: OrganizationCreate) -> Organization:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO organizations (name, slug, description)
            VALUES ($1, $2, $3)
            RETURNING id, name, slug, description, created_at, updated_at
        """, data.name, data.slug, data.description)
        return Organization(**dict(row))


async def list_companies(organization_id: UUID | None = None) -> list[Company]:
    pool = get_pool()
    query = """
        SELECT 
            c.id, c.organization_id, c.name, c.code, c.parent_company_id,
            c.brand_id, c.currency, c.is_active, c.created_at, c.updated_at,
            o.name AS organization_name,
            b.name AS brand_name
        FROM companies c
        JOIN organizations o ON o.id = c.organization_id
        LEFT JOIN brands b ON b.id = c.brand_id
    """
    args = []
    if organization_id:
        query += " WHERE c.organization_id = $1"
        args.append(organization_id)
    query += " ORDER BY c.name ASC"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
        return [Company(**dict(r)) for r in rows]


async def get_company(company_id: UUID) -> Company | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                c.id, c.organization_id, c.name, c.code, c.parent_company_id,
                c.brand_id, c.currency, c.is_active, c.created_at, c.updated_at,
                o.name AS organization_name,
                b.name AS brand_name
            FROM companies c
            JOIN organizations o ON o.id = c.organization_id
            LEFT JOIN brands b ON b.id = c.brand_id
            WHERE c.id = $1
        """, company_id)
        return Company(**dict(row)) if row else None


async def create_company(data: CompanyCreate) -> Company:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO companies (organization_id, name, code, parent_company_id, brand_id, currency, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, organization_id, name, code, parent_company_id, brand_id, currency, is_active, created_at, updated_at
        """, data.organization_id, data.name, data.code, data.parent_company_id, data.brand_id, data.currency, data.is_active)
        return await get_company(row["id"])


# ==============================================================================
# OAuth Connections
# ==============================================================================

def _row_to_connection(r: dict) -> OAuthConnection:
    now = datetime.now(timezone.utc)
    expires_at = r.get("expires_at")
    is_expired = bool(expires_at and expires_at < now)
    meta = r.get("connection_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    return OAuthConnection(
        id=r["id"],
        organization_id=r["organization_id"],
        organization_name=r.get("organization_name"),
        company_id=r.get("company_id"),
        company_name=r.get("company_name"),
        company_code=r.get("company_code"),
        provider=r["provider"],
        provider_account_id=r.get("provider_account_id"),
        account_name=r.get("account_name"),
        token_type=r.get("token_type") or "Bearer",
        expires_at=expires_at,
        is_expired=is_expired,
        scopes=list(r.get("scopes") or []),
        status=r["status"],
        error_detail=r.get("error_detail"),
        connection_metadata=meta,
        has_refresh_token=bool(r.get("refresh_token_encrypted")),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


async def list_connections(
    organization_id: UUID | None = None,
    company_id: UUID | None = None,
    provider: str | None = None,
) -> list[OAuthConnection]:
    pool = get_pool()
    query = """
        SELECT 
            c.id, c.organization_id, c.company_id, c.provider, c.provider_account_id,
            c.account_name, c.token_type, c.expires_at, c.scopes, c.status,
            c.error_detail, c.connection_metadata, c.refresh_token_encrypted,
            c.created_at, c.updated_at,
            o.name AS organization_name,
            comp.name AS company_name,
            comp.code AS company_code
        FROM oauth_connections c
        JOIN organizations o ON o.id = c.organization_id
        LEFT JOIN companies comp ON comp.id = c.company_id
        WHERE 1=1
    """
    args = []
    idx = 1
    if organization_id:
        query += f" AND c.organization_id = ${idx}"
        args.append(organization_id)
        idx += 1
    if company_id:
        query += f" AND c.company_id = ${idx}"
        args.append(company_id)
        idx += 1
    if provider:
        query += f" AND c.provider = ${idx}"
        args.append(provider)
        idx += 1

    query += " ORDER BY o.name ASC, comp.name ASC NULLS FIRST, c.provider ASC"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
        return [_row_to_connection(dict(r)) for r in rows]


async def get_connection(connection_id: UUID) -> OAuthConnection | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                c.id, c.organization_id, c.company_id, c.provider, c.provider_account_id,
                c.account_name, c.token_type, c.expires_at, c.scopes, c.status,
                c.error_detail, c.connection_metadata, c.refresh_token_encrypted,
                c.created_at, c.updated_at,
                o.name AS organization_name,
                comp.name AS company_name,
                comp.code AS company_code
            FROM oauth_connections c
            JOIN organizations o ON o.id = c.organization_id
            LEFT JOIN companies comp ON comp.id = c.company_id
            WHERE c.id = $1
        """, connection_id)
        return _row_to_connection(dict(row)) if row else None


async def store_connection(
    data: OAuthConnectionCreate,
    user_id: UUID | None = None,
) -> OAuthConnection:
    """Stores or updates an OAuth connection, encrypting access and refresh tokens."""
    pool = get_pool()
    enc_access = crypto.encrypt_token(data.access_token)
    enc_refresh = crypto.encrypt_token(data.refresh_token)

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO oauth_connections (
                organization_id, company_id, provider, provider_account_id, account_name,
                access_token_encrypted, refresh_token_encrypted, token_type, expires_at,
                scopes, status, connection_metadata, created_by, updated_by
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'CONNECTED', $11, $12, $12)
            RETURNING id
        """,
            data.organization_id,
            data.company_id,
            data.provider,
            data.provider_account_id,
            data.account_name,
            enc_access,
            enc_refresh,
            data.token_type,
            data.expires_at,
            data.scopes,
            json.dumps(data.connection_metadata),
            user_id,
        )
        return await get_connection(row["id"])


async def update_connection(
    connection_id: UUID,
    data: OAuthConnectionUpdate,
    user_id: UUID | None = None,
) -> OAuthConnection | None:
    pool = get_pool()
    updates = []
    args = [connection_id]
    idx = 2

    if data.account_name is not None:
        updates.append(f"account_name = ${idx}")
        args.append(data.account_name)
        idx += 1
    if data.access_token is not None:
        updates.append(f"access_token_encrypted = ${idx}")
        args.append(crypto.encrypt_token(data.access_token))
        idx += 1
    if data.refresh_token is not None:
        updates.append(f"refresh_token_encrypted = ${idx}")
        args.append(crypto.encrypt_token(data.refresh_token))
        idx += 1
    if data.expires_at is not None:
        updates.append(f"expires_at = ${idx}")
        args.append(data.expires_at)
        idx += 1
    if data.scopes is not None:
        updates.append(f"scopes = ${idx}")
        args.append(data.scopes)
        idx += 1
    if data.status is not None:
        updates.append(f"status = ${idx}")
        args.append(data.status)
        idx += 1
    if data.error_detail is not None:
        updates.append(f"error_detail = ${idx}")
        args.append(data.error_detail)
        idx += 1
    if data.connection_metadata is not None:
        updates.append(f"connection_metadata = ${idx}")
        args.append(json.dumps(data.connection_metadata))
        idx += 1

    if user_id:
        updates.append(f"updated_by = ${idx}")
        args.append(user_id)
        idx += 1

    updates.append("updated_at = now()")

    if not updates:
        return await get_connection(connection_id)

    query = f"UPDATE oauth_connections SET {', '.join(updates)} WHERE id = $1 RETURNING id"
    async with pool.acquire() as conn:
        res = await conn.fetchrow(query, *args)
        return await get_connection(res["id"]) if res else None


async def revoke_connection(connection_id: UUID) -> bool:
    pool = get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute("""
            UPDATE oauth_connections
            SET status = 'REVOKED', updated_at = now()
            WHERE id = $1
        """, connection_id)
        return res.endswith("1")


# ==============================================================================
# Hierarchical Connection Resolution & Token Management
# ==============================================================================

async def resolve_oauth_connection(
    organization_id: UUID,
    company_id: UUID | None,
    provider: OAuthProvider,
) -> ResolvedConnection | None:
    """Resolves an OAuth connection using the hierarchical fallback pattern:
    1. Check for connection specific to company_id.
    2. If not found, fall back to organization-wide connection (company_id IS NULL).
    3. Ensure access token is valid and refreshed if needed.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Step 1: Look for company-specific connection
        row = None
        scope_level = "COMPANY"
        if company_id:
            row = await conn.fetchrow("""
                SELECT * FROM oauth_connections
                WHERE organization_id = $1 
                  AND company_id = $2 
                  AND provider = $3
                  AND status = 'CONNECTED'
                ORDER BY updated_at DESC
                LIMIT 1
            """, organization_id, company_id, provider)

        # Step 2: Fallback to organization-level connection
        if not row:
            scope_level = "ORGANIZATION"
            row = await conn.fetchrow("""
                SELECT * FROM oauth_connections
                WHERE organization_id = $1 
                  AND company_id IS NULL 
                  AND provider = $2
                  AND status = 'CONNECTED'
                ORDER BY updated_at DESC
                LIMIT 1
            """, organization_id, provider)

        if not row:
            return None

        connection_id = row["id"]

    # Step 3: Get valid / refreshed access token
    access_token = await get_valid_access_token(connection_id)
    if not access_token:
        return None

    full_conn = await get_connection(connection_id)
    return ResolvedConnection(
        connection=full_conn,
        scope_level=scope_level,
        access_token=access_token,
        provider=provider,
    )


async def get_valid_access_token(
    connection_id: UUID,
    force_refresh: bool = False,
) -> str | None:
    """Retrieves the decrypted access token. If expired or close to expiry (within 5 minutes),
    refreshes it using the provider's OAuth token endpoint, encrypts the new tokens,
    and updates the database.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                provider, access_token_encrypted, refresh_token_encrypted,
                expires_at, connection_metadata, status
            FROM oauth_connections
            WHERE id = $1
        """, connection_id)
        if not row:
            return None

    if row["status"] in ("REVOKED", "ERROR"):
        logger.warning(f"Cannot use OAuth connection {connection_id} with status {row['status']}")
        return None

    expires_at = row["expires_at"]
    now = datetime.now(timezone.utc)
    threshold = now + timedelta(minutes=5)
    is_expiring = bool(expires_at and expires_at <= threshold)

    current_access_token = crypto.decrypt_token(row["access_token_encrypted"])
    refresh_token = crypto.decrypt_token(row["refresh_token_encrypted"])

    if not force_refresh and not is_expiring and current_access_token:
        return current_access_token

    # Token needs refresh
    if not refresh_token:
        if is_expiring:
            logger.error(f"OAuth connection {connection_id} expired and has no refresh token")
            await update_connection(connection_id, OAuthConnectionUpdate(status="EXPIRED", error_detail="Token expired with no refresh token"))
            return None
        return current_access_token

    # Refresh via provider
    provider = row["provider"]
    meta = row.get("connection_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    refreshed_data = await _execute_provider_refresh(provider, refresh_token, meta)
    if not refreshed_data:
        await update_connection(
            connection_id,
            OAuthConnectionUpdate(status="NEEDS_REAUTH", error_detail="Failed to refresh OAuth token with provider")
        )
        return None

    new_access_token = refreshed_data.get("access_token")
    new_refresh_token = refreshed_data.get("refresh_token") or refresh_token
    expires_in_seconds = refreshed_data.get("expires_in", 3600)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in_seconds))

    await update_connection(
        connection_id,
        OAuthConnectionUpdate(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            expires_at=new_expires_at,
            status="CONNECTED",
            error_detail=None,
        )
    )
    return new_access_token


async def _execute_provider_refresh(
    provider: str,
    refresh_token: str,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Calls provider token endpoint to refresh OAuth tokens."""
    token_url = PROVIDER_TOKEN_URLS.get(provider)
    if not token_url:
        logger.error(f"Unknown provider for OAuth refresh: {provider}")
        return None

    client_id = metadata.get("client_id") or ""
    client_secret = metadata.get("client_secret") or ""

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        def _call_refresh():
            return requests.post(token_url, data=payload, timeout=15.0)

        resp = await asyncio.to_thread(_call_refresh)
        if resp.status_code == 200:
            return resp.json()
        logger.error(f"OAuth refresh failed for {provider}: {resp.status_code} {resp.text}")
        return None
    except Exception as exc:
        logger.exception(f"Exception during OAuth refresh for {provider}: {exc}")
        return None
