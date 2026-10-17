"""Pure company-seat rules: no live database or issued login sessions."""
import pytest
from app.company_policy import effective_member_scope, ensure_seat_limit


def test_explicit_shared_company_member_gets_future_record_visibility():
    assert effective_member_scope(
        shared_company_access=True, all_customers=False, all_projects=False,
        customer_ids=[], project_ids=[],
    ) == (True, True)


@pytest.mark.parametrize("customer_ids,project_ids", [
    (["customer-a"], []), ([], ["project-a"]), (["a"], ["b"]),
])
def test_cannot_look_shared_with_only_fixture_specific_grants(customer_ids, project_ids):
    with pytest.raises(ValueError, match="per-record grants"):
        effective_member_scope(
            shared_company_access=True, all_customers=False, all_projects=False,
            customer_ids=customer_ids, project_ids=project_ids,
        )


def test_legacy_scoped_members_are_preserved():
    assert effective_member_scope(
        shared_company_access=False, all_customers=False, all_projects=False,
        customer_ids=["customer-a"], project_ids=["project-a"],
    ) == (False, False)


def test_explicit_legacy_all_customers_does_not_force_projects():
    assert effective_member_scope(
        shared_company_access=False, all_customers=True, all_projects=False,
        customer_ids=[], project_ids=[],
    ) == (True, False)


@pytest.mark.parametrize("active,extra,allowed", [
    (0, True, 4), (3, True, 4), (4, False, 4), (2, False, 3), (3, False, 3),
])
def test_seats_accept_valid_counts(active, extra, allowed):
    ensure_seat_limit(requested_limit=allowed, active_members=active, additional_active=extra)


@pytest.mark.parametrize("active,extra,allowed", [
    (4, True, 4), (5, False, 4), (3, True, 3), (4, False, 3),
])
def test_seats_reject_excess(active, extra, allowed):
    with pytest.raises(ValueError, match="capacity exceeded"):
        ensure_seat_limit(requested_limit=allowed, active_members=active, additional_active=extra)


@pytest.mark.parametrize("limit", [0, -1, 10001])
def test_invalid_seat_allowance_rejected(limit):
    with pytest.raises(ValueError, match="supported range"):
        ensure_seat_limit(requested_limit=limit, active_members=0, additional_active=False)


def test_shared_member_bootstrap_requires_sync_capability():
    from pydantic import ValidationError
    from app.admin import AdminBootstrapRequest
    values=dict(organizationID="org",organizationName="Org",userID="user",
        displayName="User",membershipID="membership",sharedCompanyAccess=True,
        capabilities=["customer.create"])
    with pytest.raises(ValidationError, match="requires sync capability"):
        AdminBootstrapRequest.model_validate(values)
    values["capabilities"].append("sync")
    assert AdminBootstrapRequest.model_validate(values).sharedCompanyAccess is True
