"""Pure policy for company-shared pilot seats; no database or HTTP side effects."""
from __future__ import annotations

PILOT_DEFAULT_SEAT_LIMIT = 4
PILOT_MAX_SEAT_LIMIT = 10000


def effective_member_scope(
    *,
    shared_company_access: bool,
    all_customers: bool,
    all_projects: bool,
    customer_ids: list[str],
    project_ids: list[str],
) -> tuple[bool, bool]:
    """Opt-in sales-seat company visibility without changing legacy scoped memberships.

    An explicit shared-company member must not carry arbitrary per-record grants
    masquerading as permission for future customers and projects.
    """
    if shared_company_access:
        if customer_ids or project_ids:
            raise ValueError("company shared access may not include per-record grants")
        return True, True
    return all_customers, all_projects


def ensure_seat_limit(*, requested_limit: int, active_members: int, additional_active: bool) -> None:
    """Fail closed on seat over-allocation, including limit reductions.

    Caller serializes this decision with SELECT ... FOR UPDATE on the organization.
    """
    if not 1 <= requested_limit <= PILOT_MAX_SEAT_LIMIT:
        raise ValueError("seat limit outside supported range")
    if active_members < 0:
        raise ValueError("active membership count invalid")
    if active_members + int(additional_active) > requested_limit:
        raise ValueError("organization seat capacity exceeded")
