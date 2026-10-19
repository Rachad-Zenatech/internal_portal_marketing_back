"""OAuth Integration Router

Manages Organization -> Company -> OAuth Connections hierarchy.
Endpoints to connect, inspect, refresh, and resolve OAuth tokens for ads & services.
"""
import base64
import json
import uuid
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.oauth_connection_model import (
    Company,
    CompanyCreate,
    OAuthAuthorizeResponse,
    OAuthCallbackRequest,
    OAuthConnectResponse,
    OAuthConnection,
    OAuthConnectionCreate,
    OAuthConnectionUpdate,
    OAuthInstantConnectRequest,
    OAuthProvider,
    Organization,
    OrganizationCreate,
    ResolvedConnection,
)
from services import oauth_connection_service as oauth_svc
from services.auth_service import get_current_user_id_dependency, require_permission
from services.audit_service import log_audit_event
from services.oauth_adapters import get_oauth_adapter

router = APIRouter(prefix="/oauth", tags=["OAuth & Multi-Entity Integrations"])

CurrentUser = Annotated[UUID, Depends(get_current_user_id_dependency)]
AdminViewUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_VIEW"))]
AdminUser = Annotated[UUID, Depends(require_permission("CONFIG_ROLES_UPDATE"))]
ResolveUser = Annotated[UUID, Depends(require_permission("INITIATIVES_VIEW"))]


# ==============================================================================
# Organizations
# ==============================================================================

@router.get("/organizations", response_model=list[Organization])
async def list_organizations(_user_id: AdminViewUser):
    """List all top-level organizations (e.g. ZenaTech Group)."""
    return await oauth_svc.list_organizations()


@router.get("/organizations/{organization_id}", response_model=Organization)
async def get_organization(organization_id: UUID, _user_id: AdminViewUser):
    org = await oauth_svc.get_organization(organization_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


@router.post("/organizations", response_model=Organization, status_code=status.HTTP_201_CREATED)
async def create_organization(data: OrganizationCreate, user_id: AdminUser):
    try:
        org = await oauth_svc.create_organization(data)
        await log_audit_event("Organization", str(org.id), "CREATE", user_id, {"name": org.name, "slug": org.slug})
        return org
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ==============================================================================
# Companies / Entities / Subsidiaries
# ==============================================================================

@router.get("/companies", response_model=list[Company])
async def list_companies(
    _user_id: AdminViewUser,
    organization_id: UUID | None = Query(None, description="Filter by parent organization"),
):
    """List companies/entities (e.g. ZenaTech USA, LE, ZD, Othership, DeskFlex, WorkAware)."""
    return await oauth_svc.list_companies(organization_id)


@router.get("/companies/{company_id}", response_model=Company)
async def get_company(company_id: UUID, _user_id: AdminViewUser):
    comp = await oauth_svc.get_company(company_id)
    if not comp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return comp


@router.post("/companies", response_model=Company, status_code=status.HTTP_201_CREATED)
async def create_company(data: CompanyCreate, user_id: AdminUser):
    try:
        comp = await oauth_svc.create_company(data)
        await log_audit_event("Company", str(comp.id), "CREATE", user_id, {"name": comp.name, "code": comp.code, "organization_id": str(comp.organization_id)})
        return comp
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ==============================================================================
# OAuth Connections
# ==============================================================================

@router.get("/connections", response_model=list[OAuthConnection])
async def list_connections(
    _user_id: AdminViewUser,
    organization_id: UUID | None = Query(None),
    company_id: UUID | None = Query(None),
    provider: OAuthProvider | None = Query(None),
):
    """List registered OAuth connections with stripped/safe token credentials (requires administrative permission)."""
    return await oauth_svc.list_connections(organization_id, company_id, provider)


@router.get("/connections/{connection_id}", response_model=OAuthConnection)
async def get_connection(connection_id: UUID, _user_id: AdminViewUser):
    conn = await oauth_svc.get_connection(connection_id)
    if not conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OAuth connection not found")
    return conn


@router.post("/connections", response_model=OAuthConnection, status_code=status.HTTP_201_CREATED)
async def store_connection(data: OAuthConnectionCreate, user_id: AdminUser):
    """Store a newly authorized OAuth connection (encrypts tokens before saving)."""
    try:
        conn = await oauth_svc.store_connection(data, user_id=user_id)
        await log_audit_event(
            "OAuthConnection",
            str(conn.id),
            "CREATE",
            user_id,
            {
                "provider": conn.provider,
                "account_name": conn.account_name,
                "provider_account_id": conn.provider_account_id,
                "scope_level": "COMPANY" if conn.company_id else "ORGANIZATION",
                "organization_id": str(conn.organization_id),
                "company_id": str(conn.company_id) if conn.company_id else None,
            },
        )
        return conn
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/connections/{connection_id}/refresh", response_model=OAuthConnection)
async def refresh_connection(connection_id: UUID, user_id: AdminUser):
    """Forces a token refresh check and returns updated connection status."""
    token = await oauth_svc.get_valid_access_token(connection_id, force_refresh=True)
    if not token:
        conn = await oauth_svc.get_connection(connection_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to refresh token: {conn.error_detail if conn else 'Unknown error'}"
        )
    await log_audit_event("OAuthConnection", str(connection_id), "TOKEN_REFRESH", user_id, {"connection_id": str(connection_id)})
    return await oauth_svc.get_connection(connection_id)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_connection(connection_id: UUID, user_id: AdminUser):
    """Disconnects and revokes an OAuth connection."""
    success = await oauth_svc.revoke_connection(connection_id)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    await log_audit_event("OAuthConnection", str(connection_id), "REVOKE", user_id, {"connection_id": str(connection_id)})
    return None


@router.get("/resolve", response_model=ResolvedConnection)
async def resolve_connection(
    _user_id: ResolveUser,
    organization_id: UUID = Query(...),
    company_id: UUID | None = Query(None, description="Selected company/entity"),
    provider: OAuthProvider = Query(..., description="Target provider, e.g. GOOGLE_ADS"),
):
    """Resolves the active OAuth connection according to the hierarchy:
    Company connection first -> Organization-wide fallback.
    Returns valid decrypted access token and resolved scope level.
    """
    resolved = await oauth_svc.resolve_oauth_connection(organization_id, company_id, provider)
    if not resolved:
        target = f"company '{company_id}' or organization '{organization_id}'"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active '{provider}' OAuth connection found for {target}."
        )
    return resolved


# ==============================================================================
# OAuth 2.0 Authorization & Callback Flow
# ==============================================================================

@router.get("/authorize/{provider}", response_model=OAuthAuthorizeResponse)
async def get_authorization_url(
    provider: OAuthProvider,
    _user_id: AdminUser,
    organization_id: UUID = Query(..., description="Target Organization ID"),
    company_id: UUID | None = Query(None, description="Optional target Company ID"),
    redirect_uri: str = Query("http://localhost:5180/oauth/callback", description="App callback URI"),
):
    """Generates a secure OAuth 2.0 authorization URL for the target ad or social provider."""
    try:
        adapter = get_oauth_adapter(provider)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    state_payload = {
        "org_id": str(organization_id),
        "comp_id": str(company_id) if company_id else None,
        "provider": provider,
    }
    state = base64.urlsafe_b64encode(json.dumps(state_payload).encode()).decode()
    auth_url = adapter.get_authorization_url(state=state, redirect_uri=redirect_uri)

    return OAuthAuthorizeResponse(
        provider=provider,
        authorization_url=auth_url,
        state=state,
        is_configured=adapter.is_configured,
    )


@router.post("/callback", response_model=OAuthConnectResponse)
async def handle_oauth_callback(
    data: OAuthCallbackRequest,
    user_id: AdminUser,
):
    """Handles the OAuth 2.0 callback, exchanging the code for tokens and discovering accounts."""
    try:
        adapter = get_oauth_adapter(data.provider)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    try:
        state_data = json.loads(base64.urlsafe_b64decode(data.state.encode()).decode())
        org_id = UUID(state_data["org_id"])
        comp_id = UUID(state_data["comp_id"]) if state_data.get("comp_id") else None
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state parameter")

    redirect_uri = data.redirect_uri or "http://localhost:5180/oauth/callback"
    try:
        token_info = await adapter.exchange_code(code=data.code, redirect_uri=redirect_uri)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Token exchange failed: {e}")

    # Discover accounts
    discovered = await adapter.discover_accounts(token_info["access_token"])
    primary = discovered[0] if discovered else {
        "account_id": f"acc_{uuid.uuid4().hex[:8]}",
        "account_name": f"{adapter.name} Account",
    }

    create_payload = OAuthConnectionCreate(
        organization_id=org_id,
        company_id=comp_id,
        provider=data.provider,
        provider_account_id=primary.get("account_id"),
        account_name=primary.get("account_name"),
        access_token=token_info["access_token"],
        refresh_token=token_info.get("refresh_token"),
        token_type=token_info.get("token_type", "Bearer"),
        expires_at=token_info.get("expires_at"),
        scopes=token_info.get("scopes", adapter.default_scopes),
        connection_metadata={"discovered_accounts": discovered},
    )

    conn = await oauth_svc.store_connection(create_payload, user_id=user_id)
    await log_audit_event("OAuthConnection", str(conn.id), "OAUTH_AUTHORIZE", user_id, {"provider": data.provider})

    return OAuthConnectResponse(
        connection=conn,
        discovered_accounts=discovered,
    )


@router.post("/connect-instant/{provider}", response_model=OAuthConnectResponse)
async def connect_provider_instant(
    provider: OAuthProvider,
    data: OAuthInstantConnectRequest,
    user_id: AdminUser,
):
    """Instantly connects the requested advertising or social provider using the adapter.
    Performs OAuth authorization, retrieves accounts, and encrypts credentials.
    """
    try:
        adapter = get_oauth_adapter(provider)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    token_info = await adapter.exchange_code(code="simulated_auth_grant", redirect_uri="http://localhost:5180/oauth/callback")
    discovered = await adapter.discover_accounts(token_info["access_token"])
    primary = discovered[0] if discovered else {
        "account_id": f"acc_{uuid.uuid4().hex[:8]}",
        "account_name": f"{adapter.name} Account",
    }

    account_name = data.account_name or primary.get("account_name")
    provider_account_id = data.provider_account_id or primary.get("account_id")

    create_payload = OAuthConnectionCreate(
        organization_id=data.organization_id,
        company_id=data.company_id,
        provider=provider,
        provider_account_id=provider_account_id,
        account_name=account_name,
        access_token=token_info["access_token"],
        refresh_token=token_info.get("refresh_token"),
        token_type=token_info.get("token_type", "Bearer"),
        expires_at=token_info.get("expires_at"),
        scopes=token_info.get("scopes", adapter.default_scopes),
        connection_metadata={"discovered_accounts": discovered},
    )

    conn = await oauth_svc.store_connection(create_payload, user_id=user_id)
    await log_audit_event("OAuthConnection", str(conn.id), "OAUTH_INSTANT_CONNECT", user_id, {"provider": provider})

    return OAuthConnectResponse(
        connection=conn,
        discovered_accounts=discovered,
    )
