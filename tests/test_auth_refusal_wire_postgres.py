"""The exact HTTP response of every authentication refusal, pinned at the wire.

`test_session_lifecycle_postgres.py` pins `current_principal` IN-FUNCTION: which branch
raises, and that the status is 401. That is not what a client sees. A client sees status
plus body bytes, after FastAPI serialises the exception -- and nothing pinned those bytes
on either estate.

Why that gap mattered (VITR-V016): the BEAM port renders refusals with
`detail(reason) when is_atom(reason)`, producing `{"detail":"session_expired_or_revoked"}`
where this service sends `{"detail":"session expired or revoked"}`. Same status, different
body, and no test on either side could see it. This file is the reference answer the BEAM
port is meant to reproduce byte-for-byte; it is asserted with `==` on the whole body, not
a substring, so a drift in punctuation, casing or extra keys is caught too.

Two refusals deliberately share a body:

- **An unknown token answers exactly like an expired or revoked one.** `auth.py:48` folds
  "no such session" into the same branch. A distinct answer would let a caller learn
  whether a token was ever issued -- after a leak, whether a stolen-then-revoked token was
  real. The BEAM port currently answers `authentication required` here; that is the
  divergence this test exists to make visible.
- **A missing organization answers like an inactive membership** (`auth.py:53`).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth import token_hash
from app.db import SessionFactory
from app.main import app
from app.models import AuthSession, Membership, Organization, User

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

TOKEN = "wire-refusal-token-long-enough-to-be-realistic-0123456789"
ROUTE = "/api/v1/auth/me"

# The reference vocabulary. If one of these changes, the BEAM port's `detail/1` must
# change with it -- that coupling is the reason the strings are spelled out here.
UNAUTHENTICATED = {"detail": "authentication required"}
EXPIRED_OR_REVOKED = {"detail": "session expired or revoked"}
MEMBERSHIP_INACTIVE = {"detail": "membership inactive"}
MEMBERSHIP_MISMATCH = {"detail": "session membership mismatch"}


async def _reset() -> None:
    """Scoped to this file's ids; never truncates shared tables (see the GC tests)."""
    from sqlalchemy import delete

    async with SessionFactory() as db:
        await db.execute(delete(AuthSession).where(AuthSession.id.like("sess-wire-%")))
        await db.execute(delete(Membership).where(Membership.id.like("membership-wire-%")))
        await db.execute(delete(User).where(User.id.like("user-wire-%")))
        await db.execute(delete(Organization).where(Organization.id.like("org-wire-%")))
        await db.commit()


async def _seed(
    *,
    expires_in: timedelta = timedelta(hours=1),
    revoked: bool = False,
    active: bool = True,
    membership_org: str = "org-wire-1",
) -> None:
    now = datetime.now(timezone.utc)
    async with SessionFactory() as db:
        db.add(Organization(id="org-wire-1", name="Wire", authorization_revision=1))
        db.add(Organization(id="org-wire-2", name="Elsewhere", authorization_revision=1))
        db.add(User(id="user-wire-1", display_name="Wire", email="wire@example.invalid"))
        await db.flush()
        db.add(
            Membership(
                id="membership-wire-1",
                organization_id=membership_org,
                user_id="user-wire-1",
                active=active,
                all_customers=True,
                all_projects=True,
                capabilities=["sync"],
            )
        )
        await db.flush()
        db.add(
            AuthSession(
                id="sess-wire-1",
                organization_id="org-wire-1",
                membership_id="membership-wire-1",
                user_id="user-wire-1",
                access_token_hash=token_hash(TOKEN),
                issued_at=now - timedelta(hours=2),
                expires_at=now + expires_in,
                revoked_at=now if revoked else None,
            )
        )
        await db.commit()


def _get(headers: dict[str, str] | None = None):
    with TestClient(app) as client:
        return client.get(ROUTE, headers=headers or {})


def _assert_refusal(response, expected: dict) -> None:
    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == expected, (
        f"refusal body drifted from the reference: {response.text}"
    )


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_a_valid_session_reaches_the_route():
    """Control: without it, a route that refused everything would pass every test below."""
    await _reset()
    await _seed()
    assert _get(_bearer()).status_code == 200


@pytest.mark.asyncio
async def test_missing_header_and_non_bearer_scheme():
    _assert_refusal(_get(), UNAUTHENTICATED)
    _assert_refusal(_get({"Authorization": f"Basic {TOKEN}"}), UNAUTHENTICATED)


@pytest.mark.asyncio
async def test_expired_session():
    await _reset()
    await _seed(expires_in=-timedelta(seconds=1))
    _assert_refusal(_get(_bearer()), EXPIRED_OR_REVOKED)


@pytest.mark.asyncio
async def test_revoked_session():
    await _reset()
    await _seed(revoked=True)
    _assert_refusal(_get(_bearer()), EXPIRED_OR_REVOKED)


@pytest.mark.asyncio
async def test_unknown_token_is_indistinguishable_from_expired():
    """The token-existence oracle the BEAM port currently opens (VITR-V016)."""
    await _reset()
    unknown = _get({"Authorization": "Bearer never-issued-token-value"})
    await _seed(expires_in=-timedelta(seconds=1))
    expired = _get(_bearer())

    _assert_refusal(unknown, EXPIRED_OR_REVOKED)
    assert unknown.status_code == expired.status_code
    assert unknown.json() == expired.json(), (
        "an unknown token must not be distinguishable from an expired one"
    )


@pytest.mark.asyncio
async def test_inactive_membership():
    await _reset()
    await _seed(active=False)
    _assert_refusal(_get(_bearer()), MEMBERSHIP_INACTIVE)


@pytest.mark.asyncio
async def test_membership_in_another_organization():
    await _reset()
    await _seed(membership_org="org-wire-2")
    _assert_refusal(_get(_bearer()), MEMBERSHIP_MISMATCH)
