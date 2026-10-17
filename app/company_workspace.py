"""Authenticated company workspace view; organization is taken only from the bearer principal."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal
from app.db import session_scope
from app.models import Membership, Organization, User
from app.schemas import AuthorizationRole, StrictModel


class CompanyMemberView(StrictModel):
    membershipID: str
    userID: str
    displayName: str
    roles: list[AuthorizationRole]


class CompanyWorkspaceView(StrictModel):
    organizationID: str
    organizationName: str
    seatLimit: int = Field(ge=1)
    occupiedSeats: int = Field(ge=0)
    seatsRemaining: int = Field(ge=0)
    members: list[CompanyMemberView]
    currentMemberHasCompanyCustomerAccess: bool
    currentMemberHasCompanyProjectAccess: bool


router = APIRouter(prefix="/api/v1/company", tags=["company"])


@router.get("/workspace", response_model=CompanyWorkspaceView, operation_id="companyWorkspace")
async def company_workspace(
    response: Response,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> CompanyWorkspaceView:
    response.headers["Cache-Control"] = "no-store"
    organization = await db.get(Organization, principal.organization_id)
    if organization is None:
        raise HTTPException(401, "company not found")
    result = await db.execute(
        select(Membership, User)
        .join(User, Membership.user_id == User.id)
        .where(
            Membership.organization_id == principal.organization_id,
            Membership.active.is_(True),
        )
        .order_by(User.display_name, Membership.id)
    )
    members = [
        CompanyMemberView(
            membershipID=membership.id,
            userID=user.id,
            displayName=user.display_name,
            roles=[AuthorizationRole.model_validate(role) for role in membership.roles],
        )
        for membership, user in result.all()
    ]
    occupied = len(members)
    return CompanyWorkspaceView(
        organizationID=organization.id,
        organizationName=organization.name,
        seatLimit=organization.seat_limit,
        occupiedSeats=occupied,
        seatsRemaining=max(0, organization.seat_limit - occupied),
        members=members,
        currentMemberHasCompanyCustomerAccess=principal.all_customers,
        currentMemberHasCompanyProjectAccess=principal.all_projects,
    )
