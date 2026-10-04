"""Baseline publications are seeded at organization creation, not by the first GET.

`VITR-V004`: four GET helpers each call `ensure_baseline_publications`, so every read
route took a WRITE transaction and committed. `bootstrap_identity` now seeds instead.

The lazy seed is deliberately KEPT for organizations that already exist, because removing
it would change what a GET returns for them and that is client-visible on a pinned
contract. So the property worth pinning is narrower and more useful:

**a newly bootstrapped organization has its baselines before any GET has run.**

That is what makes the read-path seed a no-op instead of a mutation, and it is the whole
of what this change claims. Both halves are asserted — the rows exist, AND the
`VITR-V008` retry path was not needed to get them there.
"""

from __future__ import annotations

import os

import pytest

from app.admin import AdminBootstrapRequest, bootstrap_identity
from app.db import SessionFactory
from app.models import User
from app.reference_models import ReferencePublication

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)


@pytest.fixture
async def db():
    async with SessionFactory() as session:
        yield session


def _request(**overrides) -> AdminBootstrapRequest:
    base = {
        "organizationID": "org-boot-1",
        "organizationName": "Bootstrap Seed Org",
        "userID": "user-boot-1",
        "email": "bootstrap-seed@example.invalid",
        "displayName": "Seed Operator",
        "membershipID": "membership-boot-1",
        "active": True,
        "allCustomers": True,
        "allProjects": True,
        "customerIDs": [],
        "projectIDs": [],
        "roles": [],
        "capabilities": ["sync"],
        "sessionTTLSeconds": 3600,
    }
    base.update(overrides)
    return AdminBootstrapRequest(**base)


# Only these organizations are ever created by this file. Cleanup is scoped to them.
_OWNED_ORGANIZATIONS = ("org-boot-1", "org-boot-2")


async def _clear(db) -> None:
    """Delete only this file's rows.

    An earlier version truncated `organizations` outright. That passes in isolation and
    fails in the full suite, because other tests leave `canonical_projects` rows whose FK
    chain reaches `organizations`, and the truncate trips
    `fk_canonical_project_customer`. A test that clears global tables is asserting on the
    absence of other tests' state as much as on its own.
    """
    from sqlalchemy import delete

    from app.models import AuthSession, Membership, Organization

    for organization_id in _OWNED_ORGANIZATIONS:
        await db.execute(
            delete(ReferencePublication).where(
                ReferencePublication.organization_id == organization_id
            )
        )
        await db.execute(
            delete(AuthSession).where(AuthSession.organization_id == organization_id)
        )
        await db.execute(
            delete(Membership).where(Membership.organization_id == organization_id)
        )
        await db.execute(delete(User).where(User.id.like("user-boot-%")))
        await db.execute(
            delete(Organization).where(Organization.id.in_(_OWNED_ORGANIZATIONS))
        )
    await db.commit()


async def _publication_count(db, organization_id: str) -> int:
    from sqlalchemy import func, select

    return int(
        await db.scalar(
            select(func.count())
            .select_from(ReferencePublication)
            .where(ReferencePublication.organization_id == organization_id)
        )
        or 0
    )


@pytest.mark.asyncio
async def test_bootstrap_seeds_baselines_without_any_read_having_run(db):
    """The whole claim: creation writes them, so the read path has nothing to insert."""
    await _clear(db)
    assert await _publication_count(db, "org-boot-1") == 0, "precondition: nothing seeded"

    await bootstrap_identity(db, _request())

    assert await _publication_count(db, "org-boot-1") > 0, (
        "bootstrap must seed the bundled baselines; otherwise the first GET is still "
        "the first writer, which is VITR-V004"
    )


@pytest.mark.asyncio
async def test_rebootstrapping_is_idempotent_and_does_not_duplicate(db):
    """Bootstrap is re-runnable, so the seed must be too.

    A duplicate insert would raise the very `IntegrityError` that `VITR-V008` was written
    to tolerate, turning an idempotent admin call into a 500.
    """
    await _clear(db)
    await bootstrap_identity(db, _request())
    first = await _publication_count(db, "org-boot-1")

    # Re-bootstrap the SAME principal. The membership id must be reused: bootstrap
    # deliberately rejects a second membership for an existing org/user pair with a 409,
    # so varying it would be testing that guard rather than seed idempotence. Only the
    # session id changes, which is what makes this a second bootstrap at all.
    await bootstrap_identity(db, _request(sessionID="session-boot-2"))
    second = await _publication_count(db, "org-boot-1")

    assert first == second, "re-bootstrapping must not duplicate baseline publications"
    assert first > 0


@pytest.mark.asyncio
async def test_seeding_is_scoped_to_the_organization_being_bootstrapped(db):
    """A second organization's bootstrap must not touch the first one's rows.

    The seed takes an organization id rather than reading it off a principal, so this
    pins that the id is actually threaded through rather than defaulted anywhere.
    """
    await _clear(db)
    await bootstrap_identity(db, _request())
    first_org_count = await _publication_count(db, "org-boot-1")
    assert first_org_count > 0

    await bootstrap_identity(
        db,
        _request(
            organizationID="org-boot-2",
            organizationName="Second Org",
            userID="user-boot-2",
            email="second@example.invalid",
            membershipID="membership-boot-3",
            sessionID="session-boot-3",
        ),
    )

    assert await _publication_count(db, "org-boot-2") > 0
    assert await _publication_count(db, "org-boot-1") == first_org_count, (
        "bootstrapping a second organization must not alter the first one's publications"
    )


@pytest.mark.asyncio
async def test_the_bootstrap_session_is_still_issued(db):
    """Guard against the seeding change disturbing the rest of bootstrap.

    The seed runs in the same transaction as the session insert, so a regression that
    committed early or rolled back would silently break session issuance — which is the
    thing `bootstrap_identity` actually exists to do.
    """
    from app.models import AuthSession

    await _clear(db)
    response = await bootstrap_identity(db, _request())

    assert response.accessToken, "bootstrap must still return an access token"
    stored = await db.get(AuthSession, response.sessionID)
    assert stored is not None, "the session row must be committed with the seeded rows"