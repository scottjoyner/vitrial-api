from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.models import CanonicalCustomer, CanonicalItem, CanonicalProject, Membership, Organization


class AuthorizationRejected(Exception):
    """A record could not be authorized from canonical server state."""


@dataclass
class EffectiveScope:
    all_customers: bool
    all_projects: bool
    customer_ids: set[str] = field(default_factory=set)
    project_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_principal(cls, principal: Principal) -> "EffectiveScope":
        return cls(
            all_customers=principal.all_customers,
            all_projects=principal.all_projects,
            customer_ids=set(principal.customer_ids),
            project_ids=set(principal.project_ids),
        )

    def can_access_customer(self, customer_id: str) -> bool:
        return self.all_customers or customer_id in self.customer_ids

    def can_access_project(self, project_id: str, customer_id: str) -> bool:
        return self.can_access_customer(customer_id) and (
            self.all_projects or project_id in self.project_ids
        )


@dataclass(frozen=True)
class OwnershipPlan:
    entity_type: str
    entity_id: str
    is_create: bool
    deleted_at: datetime | None
    customer_id: str | None = None
    project_id: str | None = None


ENTITY_ORDER = {
    "customer": 0,
    "project": 1,
    "item": 2,
}


def mutation_sort_key(entity_type: str, original_index: int) -> tuple[int, int]:
    # Parent creates are evaluated before children so one atomic batch can contain a newly-created
    # Customer, Project and Item in any client order. Unsupported children remain fail-closed.
    return ENTITY_ORDER.get(entity_type, 100), original_index


def _value(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuthorizationRejected(f"{key} is required for canonical ownership")
    return value.strip()


def _require_identity(payload: dict, entity_id: str, *, deleting: bool) -> None:
    value = payload.get("id")
    if deleting and value is None:
        return
    if not isinstance(value, str) or value.strip() != entity_id:
        raise AuthorizationRejected("payload identity does not match canonical entityID")


def _require_capability(principal: Principal, capability: str) -> None:
    if capability not in principal.capabilities:
        raise AuthorizationRejected(f"{capability} capability required")


async def authorize_record(
    db: AsyncSession,
    principal: Principal,
    scope: EffectiveScope,
    *,
    entity_type: str,
    entity_id: str,
    payload: dict,
    deleted_at: datetime | None,
    generic_entity_exists: bool,
) -> OwnershipPlan:
    deleting = deleted_at is not None

    if entity_type == "customer":
        canonical = await db.get(CanonicalCustomer, (principal.organization_id, entity_id))
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing Customer lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown Customer")
            _require_capability(principal, "customer.create")
            _require_identity(payload, entity_id, deleting=False)
            return OwnershipPlan("customer", entity_id, True, deleted_at, customer_id=entity_id)

        _require_capability(principal, "customer.edit")
        if not scope.can_access_customer(canonical.customer_id):
            raise AuthorizationRejected("Customer is outside authorized scope")
        _require_identity(payload, entity_id, deleting=deleting)
        return OwnershipPlan("customer", entity_id, False, deleted_at, customer_id=canonical.customer_id)

    if entity_type == "project":
        canonical = await db.get(CanonicalProject, (principal.organization_id, entity_id))
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing Project lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown Project")
            _require_capability(principal, "project.create")
            _require_identity(payload, entity_id, deleting=False)
            customer_id = _value(payload, "customerID")
            customer = await db.get(CanonicalCustomer, (principal.organization_id, customer_id))
            if customer is None or customer.deleted_at is not None:
                raise AuthorizationRejected("Project parent Customer is not canonical and active")
            if not scope.can_access_customer(customer_id):
                raise AuthorizationRejected("Project parent Customer is outside authorized scope")
            return OwnershipPlan(
                "project", entity_id, True, deleted_at,
                customer_id=customer_id, project_id=entity_id,
            )

        _require_capability(principal, "project.edit")
        if not scope.can_access_project(canonical.project_id, canonical.customer_id):
            raise AuthorizationRejected("Project is outside authorized scope")
        _require_identity(payload, entity_id, deleting=deleting)
        supplied_customer = payload.get("customerID")
        if supplied_customer is not None and supplied_customer != canonical.customer_id:
            raise AuthorizationRejected("Project Customer ownership is immutable")
        return OwnershipPlan(
            "project", entity_id, False, deleted_at,
            customer_id=canonical.customer_id, project_id=canonical.project_id,
        )

    if entity_type == "item":
        canonical = await db.get(CanonicalItem, (principal.organization_id, entity_id))
        if canonical is None:
            if generic_entity_exists:
                raise AuthorizationRejected("existing Item lacks canonical ownership")
            if deleting:
                raise AuthorizationRejected("cannot tombstone an unknown Item")
            _require_capability(principal, "item.create")
            _require_identity(payload, entity_id, deleting=False)
            project_id = _value(payload, "projectID")
            project = await db.get(CanonicalProject, (principal.organization_id, project_id))
            if project is None or project.deleted_at is not None:
                raise AuthorizationRejected("Item parent Project is not canonical and active")
            if not scope.can_access_project(project.project_id, project.customer_id):
                raise AuthorizationRejected("Item parent Project is outside authorized scope")
            return OwnershipPlan(
                "item", entity_id, True, deleted_at,
                customer_id=project.customer_id, project_id=project.project_id,
            )

        _require_capability(principal, "item.edit")
        project = await db.get(CanonicalProject, (principal.organization_id, canonical.project_id))
        if project is None:
            raise AuthorizationRejected("Item canonical Project is missing")
        if not scope.can_access_project(project.project_id, project.customer_id):
            raise AuthorizationRejected("Item is outside authorized Project scope")
        _require_identity(payload, entity_id, deleting=deleting)
        supplied_project = payload.get("projectID")
        if supplied_project is not None and supplied_project != canonical.project_id:
            raise AuthorizationRejected("Item Project ownership is immutable")
        return OwnershipPlan(
            "item", entity_id, False, deleted_at,
            customer_id=project.customer_id, project_id=canonical.project_id,
        )

    # A global sync capability is transport authority, not domain mutation authority. Until a child
    # type has an explicit canonical-parent + capability policy, rejecting it is safer than silently
    # accepting a payload whose parent relationship came from the client itself.
    raise AuthorizationRejected(f"canonical authorization is not implemented for {entity_type}")


async def apply_ownership_plan(
    db: AsyncSession,
    principal: Principal,
    scope: EffectiveScope,
    plan: OwnershipPlan,
) -> None:
    if plan.entity_type == "customer":
        model = await db.get(CanonicalCustomer, (principal.organization_id, plan.entity_id))
        if model is None:
            model = CanonicalCustomer(
                organization_id=principal.organization_id,
                customer_id=plan.entity_id,
                deleted_at=plan.deleted_at,
            )
            db.add(model)
            scope.customer_ids.add(plan.entity_id)
            await _grant_created_scope(db, principal, customer_id=plan.entity_id)
        else:
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    if plan.entity_type == "project":
        assert plan.customer_id is not None
        model = await db.get(CanonicalProject, (principal.organization_id, plan.entity_id))
        if model is None:
            model = CanonicalProject(
                organization_id=principal.organization_id,
                project_id=plan.entity_id,
                customer_id=plan.customer_id,
                deleted_at=plan.deleted_at,
            )
            db.add(model)
            scope.project_ids.add(plan.entity_id)
            await _grant_created_scope(db, principal, project_id=plan.entity_id)
        else:
            if model.customer_id != plan.customer_id:
                raise AuthorizationRejected("Project Customer ownership is immutable")
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    if plan.entity_type == "item":
        assert plan.project_id is not None
        model = await db.get(CanonicalItem, (principal.organization_id, plan.entity_id))
        if model is None:
            db.add(CanonicalItem(
                organization_id=principal.organization_id,
                item_id=plan.entity_id,
                project_id=plan.project_id,
                deleted_at=plan.deleted_at,
            ))
        else:
            if model.project_id != plan.project_id:
                raise AuthorizationRejected("Item Project ownership is immutable")
            model.deleted_at = plan.deleted_at
        await db.flush()
        return

    raise AuthorizationRejected(f"unsupported ownership plan {plan.entity_type}")


async def _grant_created_scope(
    db: AsyncSession,
    principal: Principal,
    *,
    customer_id: str | None = None,
    project_id: str | None = None,
) -> None:
    membership = await db.get(Membership, principal.membership_id)
    organization = await db.get(Organization, principal.organization_id)
    if (
        membership is None
        or organization is None
        or not membership.active
        or membership.organization_id != principal.organization_id
        or membership.user_id != principal.user_id
    ):
        raise AuthorizationRejected("creator membership is not canonical and active")

    changed = False
    if customer_id is not None and not membership.all_customers and customer_id not in membership.customer_ids:
        membership.customer_ids = [*membership.customer_ids, customer_id]
        changed = True
    if project_id is not None and not membership.all_projects and project_id not in membership.project_ids:
        membership.project_ids = [*membership.project_ids, project_id]
        changed = True
    if changed:
        organization.authorization_revision += 1
        await db.flush()


async def require_item_access(
    db: AsyncSession,
    principal: Principal,
    item_id: str,
    *,
    capability: str | None = None,
) -> tuple[CanonicalItem, CanonicalProject]:
    if capability is not None:
        _require_capability(principal, capability)
    item = await db.get(CanonicalItem, (principal.organization_id, item_id))
    if item is None or item.deleted_at is not None:
        raise AuthorizationRejected("Item is not canonical and active")
    project = await db.get(CanonicalProject, (principal.organization_id, item.project_id))
    if project is None or project.deleted_at is not None:
        raise AuthorizationRejected("Item Project is not canonical and active")
    scope = EffectiveScope.from_principal(principal)
    if not scope.can_access_project(project.project_id, project.customer_id):
        raise AuthorizationRejected("Item is outside authorized Project scope")
    return item, project


async def record_is_visible(
    db: AsyncSession,
    principal: Principal,
    *,
    entity_type: str,
    entity_id: str,
) -> bool:
    scope = EffectiveScope.from_principal(principal)
    if entity_type == "customer":
        customer = await db.get(CanonicalCustomer, (principal.organization_id, entity_id))
        return customer is not None and scope.can_access_customer(customer.customer_id)
    if entity_type == "project":
        project = await db.get(CanonicalProject, (principal.organization_id, entity_id))
        return project is not None and scope.can_access_project(project.project_id, project.customer_id)
    if entity_type == "item":
        item = await db.get(CanonicalItem, (principal.organization_id, entity_id))
        if item is None:
            return False
        project = await db.get(CanonicalProject, (principal.organization_id, item.project_id))
        return project is not None and scope.can_access_project(project.project_id, project.customer_id)
    return False
