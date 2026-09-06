from __future__ import annotations
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.models import AuthSession, Membership, Organization, User
from app.schemas import AuthorizationProfile, AuthorizationRole

bearer = HTTPBearer(auto_error=False, scheme_name="bearerAuth")

@dataclass(frozen=True)
class Principal:
    user_id: str
    organization_id: str
    membership_id: str
    session_id: str
    authorization_revision: int
    capabilities: frozenset[str]
    customer_ids: frozenset[str]
    project_ids: frozenset[str]
    all_customers: bool
    all_projects: bool

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

async def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(session_scope),
) -> Principal:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "authentication required")

    now = datetime.now(timezone.utc)
    auth_session = await db.scalar(
        select(AuthSession).where(AuthSession.access_token_hash == token_hash(credentials.credentials))
    )
    if not auth_session or auth_session.revoked_at is not None or auth_session.expires_at <= now:
        raise HTTPException(401, "session expired or revoked")

    membership = await db.get(Membership, auth_session.membership_id)
    organization = await db.get(Organization, auth_session.organization_id)
    if not membership or not organization or not membership.active:
        raise HTTPException(401, "membership inactive")
    if (
        membership.organization_id != auth_session.organization_id
        or membership.user_id != auth_session.user_id
    ):
        raise HTTPException(401, "session membership mismatch")

    return Principal(
        user_id=auth_session.user_id,
        organization_id=auth_session.organization_id,
        membership_id=auth_session.membership_id,
        session_id=auth_session.id,
        authorization_revision=organization.authorization_revision,
        capabilities=frozenset(membership.capabilities),
        customer_ids=frozenset(membership.customer_ids),
        project_ids=frozenset(membership.project_ids),
        all_customers=membership.all_customers,
        all_projects=membership.all_projects,
    )

async def profile_for(principal: Principal, db: AsyncSession) -> AuthorizationProfile:
    user = await db.get(User, principal.user_id)
    membership = await db.get(Membership, principal.membership_id)
    auth_session = await db.get(AuthSession, principal.session_id)
    if not user or not membership or not auth_session:
        raise HTTPException(401, "session identity incomplete")

    return AuthorizationProfile(
        principalID=user.id,
        displayName=user.display_name,
        email=user.email,
        customerIDs=sorted(principal.customer_ids),
        allCustomers=principal.all_customers,
        capabilities=sorted(principal.capabilities),
        issuedAt=auth_session.issued_at,
        expiresAt=auth_session.expires_at,
        organizationID=principal.organization_id,
        membershipID=principal.membership_id,
        sessionID=principal.session_id,
        authorizationRevision=principal.authorization_revision,
        projectIDs=sorted(principal.project_ids),
        allProjects=principal.all_projects,
        roles=[AuthorizationRole.model_validate(r) for r in membership.roles],
    )
