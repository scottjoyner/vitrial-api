"""Session lifecycle: what actually revokes access, pinned rather than assumed.

`app/auth.py::current_principal` is the single gate every authenticated route passes
through, and it has four rejection branches (auth.py:41, 48, 53-59). Before this file
**none of the four was exercised by name, and two were never exercised at all**:

- **Expiry** was untested. Every session in the suite was created with `expires_at` in the
  FUTURE. `auth_session.expires_at <= now` therefore never fired, so inverting it to
  `>=`, or dropping the clause, would have left the whole suite green while every
  expired bearer token kept working. An unbounded session is not a hypothetical
  consequence: it is exactly what "fixed bearer TTL" is supposed to prevent.
- **Membership deactivation** was untested. Revoking a session is one lever; setting
  `Membership.active = False` is the other, and it is the one an operator reaches for
  when someone leaves. If it did not revoke, a deactivated user kept full authority until
  their token expired on its own.
- **Membership mismatch** (a session pointing at a membership belonging to a different
  org/user) was untested.
- The **expiry boundary** is asserted **structurally**, not behaviourally. An earlier
  version of that test set `expires_at = now` and expected rejection, which passes under
  `<=` *and* under `<` -- `current_principal` computes its own `now` microseconds later,
  so the two are indistinguishable from outside. Planting `<` left the test green. The
  comparison is now pinned with an AST assertion, which is honest about what a black-box
  test can and cannot see.

Revocation *was* covered (`test_postgres_integration.py`), by status code. It is repeated
here only so this file stands alone as the lifecycle gate's specification; the two
assertions are about different things and neither is redundant in a file meant to be read
as the answer to "what revokes access".
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.auth import current_principal, token_hash
from app.db import SessionFactory
from app.models import AuthSession, Membership, Organization, User

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

TOKEN = "session-lifecycle-token-value-that-is-long-enough"


@pytest.fixture
async def db():
    async with SessionFactory() as session:
        yield session


async def _clear(db) -> None:
    """Scoped to the ids below only.

    Truncating `organizations` passes in isolation and fails in the full suite: other
    tests leave `canonical_projects` rows whose FK chain reaches `organizations`, and the
    truncate trips `fk_canonical_project_customer`. Cleanup here is a prefix delete on
    this file's own ids, so it neither depends on nor destroys anyone else's state.
    """
    from sqlalchemy import delete

    await db.execute(
        delete(AuthSession).where(AuthSession.id.like("sess-lifecycle-%"))
    )
    await db.execute(
        delete(Membership).where(Membership.id.like("membership-lifecycle-%"))
    )
    await db.execute(delete(User).where(User.id.like("user-lifecycle-%")))
    await db.execute(delete(Organization).where(Organization.id.like("org-lifecycle-%")))
    await db.commit()


def _credentials() -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=TOKEN)


async def _seed(
    db,
    *,
    issued: datetime,
    expires: datetime,
    revoked_at: datetime | None = None,
    membership_active: bool = True,
    membership_org: str | None = None,
    membership_user: str | None = None,
) -> None:
    # Both organizations and both users always exist. The mismatch tests need the
    # membership to point at a REAL row in the wrong place -- pointing it at a
    # nonexistent id would fail the membership's own foreign key first and never reach
    # the mismatch branch under test.
    db.add(Organization(id="org-lifecycle-1", name="Lifecycle", authorization_revision=1))
    db.add(Organization(id="org-lifecycle-2", name="Other", authorization_revision=1))
    db.add(User(id="user-lifecycle-1", display_name="Operator", email="op@example.invalid"))
    db.add(User(id="user-lifecycle-2", display_name="Other", email="other@example.invalid"))
    await db.flush()
    db.add(
        Membership(
            id="membership-lifecycle-1",
            organization_id=membership_org or "org-lifecycle-1",
            user_id=membership_user or "user-lifecycle-1",
            active=membership_active,
            all_customers=True,
            all_projects=True,
            capabilities=["sync"],
        )
    )
    await db.flush()
    db.add(
        AuthSession(
            id="sess-lifecycle-1",
            organization_id="org-lifecycle-1",
            membership_id="membership-lifecycle-1",
            user_id="user-lifecycle-1",
            access_token_hash=token_hash(TOKEN),
            issued_at=issued,
            expires_at=expires,
            revoked_at=revoked_at,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_a_valid_session_is_accepted(db):
    """The control. Without it, every other test here would pass on a gate that refuses
    everything -- which is the failure mode a rejection-only suite cannot detect."""
    now = datetime.now(timezone.utc)
    await _clear(db)
    await _seed(db, issued=now - timedelta(minutes=1), expires=now + timedelta(hours=1))

    principal = await current_principal(_credentials(), db)
    assert principal.organization_id == "org-lifecycle-1"
    assert principal.session_id == "sess-lifecycle-1"


@pytest.mark.asyncio
async def test_an_expired_session_is_rejected(db):
    """The gap this file exists to close.

    Every session the suite previously created expired in the future, so this branch
    never ran. A regression here is not subtle -- it is every bearer token outliving its
    TTL -- and it would have been invisible.
    """
    now = datetime.now(timezone.utc)
    await _clear(db)
    await _seed(
        db,
        issued=now - timedelta(hours=2),
        expires=now - timedelta(seconds=1),
    )

    with pytest.raises(HTTPException) as exc:
        await current_principal(_credentials(), db)

    assert exc.value.status_code == 401
    assert "expired or revoked" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_session_is_rejected_once_its_ttl_has_elapsed(db):
    """The instant at and after expiry.

    The first version of this test claimed to pin the `<=` boundary and could not. It set
    `expires_at = now` and expected rejection, which passes under `<=` AND under `<`,
    because `current_principal` computes its own `now` microseconds later -- so the two
    forms are indistinguishable from outside the function. Verified by planting `<` and
    watching the test stay green.

    So the boundary is asserted structurally below, where it is actually observable, and
    this test asserts only the half a behavioural test can reach.
    """
    now = datetime.now(timezone.utc)
    await _clear(db)
    await _seed(db, issued=now - timedelta(hours=1), expires=now)

    with pytest.raises(HTTPException) as exc:
        await current_principal(_credentials(), db)

    assert exc.value.status_code == 401, (
        "a session whose expires_at has been reached must be rejected"
    )


def test_the_expiry_comparison_is_inclusive():
    """`expires_at <= now`, pinned structurally because it is not observable otherwise.

    A session whose TTL has just elapsed must not be honoured for one more request.
    `<=` and `<` are equally plausible in review and differ by an instant, and no
    behavioural test can tell them apart (see the docstring above). So the shape is
    asserted directly, the same way the GC collector pins "no commit inside the loop".
    """
    import ast
    import pathlib

    import app.auth as auth_module

    source = pathlib.Path(auth_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    comparisons = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "expires_at"
    ]
    assert comparisons, "current_principal no longer compares expires_at; update this"
    operators = [type(node.ops[0]).__name__ for node in comparisons]
    assert operators == ["LtE"], (
        f"expiry must be compared with <= (a session is expired the instant its TTL "
        f"elapses), found {operators}"
    )


@pytest.mark.asyncio
async def test_a_revoked_session_is_rejected(db):
    """Revocation is the explicit lever. Re-asserted here only so this file stands alone
    as the lifecycle specification; `test_postgres_integration.py` covers it too, by
    status code only."""
    now = datetime.now(timezone.utc)
    await _clear(db)
    await _seed(
        db,
        issued=now - timedelta(minutes=1),
        expires=now + timedelta(hours=1),
        revoked_at=now,
    )

    with pytest.raises(HTTPException) as exc:
        await current_principal(_credentials(), db)
    assert exc.value.status_code == 401
    assert "expired or revoked" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_deactivating_the_membership_revokes_a_live_session(db):
    """The other lever, and the one an operator actually reaches for.

    Revoking the session is explicit; setting `Membership.active = False` is what happens
    when someone leaves. If it did not revoke, a deactivated user kept full authority
    until their own token happened to expire.
    """
    now = datetime.now(timezone.utc)
    await _clear(db)
    await _seed(
        db,
        issued=now - timedelta(minutes=1),
        expires=now + timedelta(hours=1),
        membership_active=False,
    )

    with pytest.raises(HTTPException) as exc:
        await current_principal(_credentials(), db)

    assert exc.value.status_code == 401
    assert "membership inactive" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_deactivation_after_issue_revokes_an_already_valid_session(db):
    """Ordering matters: deactivation must take effect on the NEXT request.

    Separate from the test above because it exercises the state transition rather than
    the initial condition -- the session is proved working first, then revoked. A gate
    that cached the membership at issue time would pass the test above and fail this one.
    """
    now = datetime.now(timezone.utc)
    await _clear(db)
    await _seed(db, issued=now - timedelta(minutes=1), expires=now + timedelta(hours=1))

    # Proved working before the change.
    await current_principal(_credentials(), db)

    membership = await db.get(Membership, "membership-lifecycle-1")
    membership.active = False
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await current_principal(_credentials(), db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_a_session_pointing_at_another_organizations_membership_is_rejected(db):
    """Cross-organization mismatch must fail closed.

    `auth_session` is looked up by token hash alone, so nothing upstream of this check
    guarantees the membership it names belongs to the same organization. That
    correspondence is asserted here and nowhere else.
    """
    now = datetime.now(timezone.utc)
    await _clear(db)
    await _seed(
        db,
        issued=now - timedelta(minutes=1),
        expires=now + timedelta(hours=1),
        membership_org="org-lifecycle-2",
    )

    with pytest.raises(HTTPException) as exc:
        await current_principal(_credentials(), db)

    assert exc.value.status_code == 401
    assert "membership mismatch" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_session_naming_a_different_user_is_rejected(db):
    now = datetime.now(timezone.utc)
    await _clear(db)
    await _seed(
        db,
        issued=now - timedelta(minutes=1),
        expires=now + timedelta(hours=1),
        membership_user="user-lifecycle-2",
    )

    with pytest.raises(HTTPException) as exc:
        await current_principal(_credentials(), db)

    assert exc.value.status_code == 401
    assert "membership mismatch" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_missing_credentials_and_wrong_scheme_are_rejected(db):
    """Both halves of the first branch, which no test previously exercised.

    A DB stub is unnecessary: the credential check happens before any query, so passing
    `db=None` is not a shortcut -- it is what proves the ordering.
    """
    with pytest.raises(HTTPException) as exc:
        await current_principal(None, None)
    assert exc.value.status_code == 401
    assert "authentication required" in str(exc.value.detail)

    with pytest.raises(HTTPException) as exc:
        await current_principal(
            HTTPAuthorizationCredentials(scheme="Basic", credentials=TOKEN), None
        )
    assert exc.value.status_code == 401
    assert "authentication required" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_an_unknown_token_is_rejected_without_touching_membership(db):
    """An unknown token must fail at the session lookup.

    Asserted that it yields the same 401 as an expired one: a distinct error for
    "no such token" would be an account-existence oracle.
    """
    await _clear(db)
    stranger = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-real-token")

    with pytest.raises(HTTPException) as exc:
        await current_principal(stranger, db)

    assert exc.value.status_code == 401
    assert "expired or revoked" in str(exc.value.detail)