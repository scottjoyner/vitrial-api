from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Header, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import token_hash
from app.models import AuthSession, Membership, Organization, User, uid
from app.observability import correlation_ref, log_event
from app.schemas import AuthorizationRole, Capability, StrictModel
from app.settings import settings


class AdminBootstrapRequest(StrictModel):
    organizationID: str = Field(min_length=1, max_length=128)
    organizationName: str = Field(min_length=1, max_length=256)
    userID: str = Field(min_length=1, max_length=128)
    displayName: str = Field(min_length=1, max_length=256)
    email: str | None = Field(default=None, max_length=320)
    membershipID: str = Field(min_length=1, max_length=128)
    active: bool = True
    allCustomers: bool = False
    allProjects: bool = False
    customerIDs: list[str] = Field(default_factory=list)
    projectIDs: list[str] = Field(default_factory=list)
    roles: list[AuthorizationRole] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    sessionID: str | None = Field(default=None, min_length=1, max_length=128)
    sessionTTLSeconds: int = Field(default=8 * 60 * 60, ge=300, le=30 * 24 * 60 * 60)


class AdminBootstrapResponse(StrictModel):
    organizationID: str
    userID: str
    membershipID: str
    sessionID: str
    authorizationRevision: int
    issuedAt: datetime
    expiresAt: datetime
    accessToken: str


class AdminSessionRevokeRequest(StrictModel):
    organizationID: str = Field(min_length=1, max_length=128)


class AdminSessionRevokeResponse(StrictModel):
    organizationID: str
    sessionID: str
    revokedAt: datetime


async def require_admin_key(
    x_vitrial_admin_key: Annotated[str | None, Header(alias="X-Vitrial-Admin-Key")] = None,
) -> None:
    configured_hash = settings.admin_api_key_hash
    if not configured_hash:
        raise HTTPException(404, "admin API disabled")
    if not x_vitrial_admin_key:
        log_event("admin.authentication_failed", reason="missing_key")
        raise HTTPException(401, "admin authentication required")
    supplied_hash = hashlib.sha256(x_vitrial_admin_key.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(supplied_hash, configured_hash.lower()):
        log_event("admin.authentication_failed", reason="invalid_key")
        raise HTTPException(401, "admin authentication failed")


def _membership_authority(membership: Membership) -> tuple:
    return (
        membership.active,
        membership.all_customers,
        membership.all_projects,
        tuple(sorted(membership.customer_ids)),
        tuple(sorted(membership.project_ids)),
        tuple(sorted((role.get("id", ""), role.get("displayName", "")) for role in membership.roles)),
        tuple(sorted(membership.capabilities)),
    )


def _requested_authority(request: AdminBootstrapRequest) -> tuple:
    return (
        request.active,
        request.allCustomers,
        request.allProjects,
        tuple(sorted(set(request.customerIDs))),
        tuple(sorted(set(request.projectIDs))),
        tuple(sorted((role.id, role.displayName) for role in request.roles)),
        tuple(sorted(set(request.capabilities))),
    )


async def bootstrap_identity(
    db: AsyncSession,
    request: AdminBootstrapRequest,
) -> AdminBootstrapResponse:
    organization = await db.get(Organization, request.organizationID)
    if organization is None:
        organization = Organization(
            id=request.organizationID,
            name=request.organizationName,
            authorization_revision=0,
        )
        db.add(organization)
        await db.flush()
    else:
        organization.name = request.organizationName

    user = await db.get(User, request.userID)
    if request.email:
        email_owner = await db.scalar(select(User).where(User.email == request.email))
        if email_owner is not None and email_owner.id != request.userID:
            raise HTTPException(409, "email already belongs to another user")
    if user is None:
        user = User(
            id=request.userID,
            display_name=request.displayName,
            email=request.email,
        )
        db.add(user)
        await db.flush()
    else:
        user.display_name = request.displayName
        user.email = request.email

    membership_by_id = await db.get(Membership, request.membershipID)
    membership_for_pair = await db.scalar(
        select(Membership).where(
            Membership.organization_id == request.organizationID,
            Membership.user_id == request.userID,
        )
    )
    if membership_by_id is not None and (
        membership_by_id.organization_id != request.organizationID
        or membership_by_id.user_id != request.userID
    ):
        raise HTTPException(409, "membership ID belongs to another principal")
    if membership_for_pair is not None and membership_for_pair.id != request.membershipID:
        raise HTTPException(409, "organization/user already has another membership ID")

    membership = membership_by_id or membership_for_pair
    requested_authority = _requested_authority(request)
    authority_changed = membership is None
    if membership is None:
        membership = Membership(
            id=request.membershipID,
            organization_id=request.organizationID,
            user_id=request.userID,
        )
        db.add(membership)
    else:
        authority_changed = _membership_authority(membership) != requested_authority

    membership.active = request.active
    membership.all_customers = request.allCustomers
    membership.all_projects = request.allProjects
    membership.customer_ids = sorted(set(request.customerIDs))
    membership.project_ids = sorted(set(request.projectIDs))
    membership.roles = [role.model_dump() for role in request.roles]
    membership.capabilities = sorted(set(request.capabilities))
    if authority_changed:
        organization.authorization_revision += 1
    await db.flush()

    session_id = request.sessionID or uid()
    if await db.get(AuthSession, session_id) is not None:
        raise HTTPException(409, "session ID already exists")

    now = datetime.now(timezone.utc)
    access_token = secrets.token_urlsafe(48)
    auth_session = AuthSession(
        id=session_id,
        organization_id=request.organizationID,
        membership_id=membership.id,
        user_id=user.id,
        access_token_hash=token_hash(access_token),
        issued_at=now,
        expires_at=now + timedelta(seconds=request.sessionTTLSeconds),
    )
    db.add(auth_session)
    await db.commit()

    log_event(
        "admin.identity_bootstrapped",
        organizationRef=correlation_ref(organization.id),
        actorRef=correlation_ref(user.id),
        membershipRef=correlation_ref(membership.id),
        issuedSessionRef=correlation_ref(auth_session.id),
        authorizationRevision=organization.authorization_revision,
        authorityChanged=authority_changed,
    )
    return AdminBootstrapResponse(
        organizationID=organization.id,
        userID=user.id,
        membershipID=membership.id,
        sessionID=auth_session.id,
        authorizationRevision=organization.authorization_revision,
        issuedAt=auth_session.issued_at,
        expiresAt=auth_session.expires_at,
        accessToken=access_token,
    )


async def revoke_session(
    db: AsyncSession,
    session_id: str,
    request: AdminSessionRevokeRequest,
) -> AdminSessionRevokeResponse:
    auth_session = await db.get(AuthSession, session_id)
    if auth_session is None or auth_session.organization_id != request.organizationID:
        raise HTTPException(404, "session not found")
    if auth_session.revoked_at is None:
        auth_session.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    log_event(
        "admin.session_revoked",
        organizationRef=correlation_ref(auth_session.organization_id),
        revokedSessionRef=correlation_ref(auth_session.id),
    )
    return AdminSessionRevokeResponse(
        organizationID=auth_session.organization_id,
        sessionID=auth_session.id,
        revokedAt=auth_session.revoked_at,
    )
