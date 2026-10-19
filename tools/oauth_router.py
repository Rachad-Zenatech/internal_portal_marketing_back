"""OAuth Integration Router

Manages Organization -> Company -> OAuth Connections hierarchy.
Endpoints to connect, inspect, refresh, and resolve OAuth tokens for ads & services.
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.oauth_connection_model import (
    Company,
    CompanyCreate,
    OAuthConnection,
    OAuthConnectionCreate,
    OAuthProvider,
    Organization,
    OrganizationCreate,
    ResolvedConnection,
)
from services import oauth_connection_service as oauth_svc
from services.audit_service import log_audit_event
from services.auth_service import get_current_user_id_dependency, require_permission

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
