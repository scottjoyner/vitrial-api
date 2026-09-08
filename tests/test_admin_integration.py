import hashlib
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.auth import token_hash
from app.db import SessionFactory
from app.main import app
from app.models import AuthSession, Membership, Organization, User
from app.settings import settings

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ORG_ID = "org-admin-provisioning-test"
USER_ID = "user-admin-provisioning-test"
MEMBERSHIP_ID = "membership-admin-provisioning-test"
ADMIN_KEY = "integration-admin-key-not-a-user-bearer"


def admin_key_hash() -> str:
    return hashlib.sha256(ADMIN_KEY.encode("utf-8")).hexdigest()


async def clear_fixture() -> None:
    async with SessionFactory() as db:
        await db.execute(delete(AuthSession).where(AuthSession.organization_id == ORG_ID))
        await db.execute(delete(Membership).where(Membership.organization_id == ORG_ID))
        await db.execute(delete(User).where(User.id == USER_ID))
        await db.execute(delete(Organization).where(Organization.id == ORG_ID))
        await db.commit()


def bootstrap_payload(*, session_id: str, expanded_authority: bool = False) -> dict:
    capabilities = ["sync", "item.edit"]
    if expanded_authority:
        capabilities.append("item.create")
    return {
        "organizationID": ORG_ID,
        "organizationName": "Provisioning Integration Org",
        "userID": USER_ID,
        "displayName": "Provisioned Operator",
        "email": "provisioning-operator@example.invalid",
        "membershipID": MEMBERSHIP_ID,
        "active": True,
        "allCustomers": expanded_authority,
        "allProjects": False,
        "customerIDs": [] if expanded_authority else ["customer-admin-test"],
        "projectIDs": ["project-admin-test"],
        "roles": [{"id": "operator", "displayName": "Operator"}],
        "capabilities": capabilities,
        "sessionID": session_id,
        "sessionTTLSeconds": 3600,
    }


@pytest.mark.asyncio
async def test_admin_boundary_bootstrap_authority_refresh_and_revocation(monkeypatch):
    await clear_fixture()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(settings, "admin_api_key_hash", None)
        disabled = await client.post(
            "/internal/admin/v1/bootstrap",
            json=bootstrap_payload(session_id="session-admin-disabled"),
            headers={"X-Vitrial-Admin-Key": ADMIN_KEY},
        )
        assert disabled.status_code == 404

        monkeypatch.setattr(settings, "admin_api_key_hash", admin_key_hash())
        wrong_key = await client.post(
            "/internal/admin/v1/bootstrap",
            json=bootstrap_payload(session_id="session-admin-wrong"),
            headers={"X-Vitrial-Admin-Key": "wrong-admin-key"},
        )
        assert wrong_key.status_code == 401

        first = await client.post(
            "/internal/admin/v1/bootstrap",
            json=bootstrap_payload(session_id="session-admin-1"),
            headers={"X-Vitrial-Admin-Key": ADMIN_KEY, "X-Request-ID": "admin-bootstrap-0001"},
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        first_token = first_body["accessToken"]
        assert first_body["authorizationRevision"] == 1
        assert first_body["sessionID"] == "session-admin-1"
        assert first.headers["X-Request-ID"] == "admin-bootstrap-0001"
        assert first_token != ADMIN_KEY

        async with SessionFactory() as db:
            stored_session = await db.get(AuthSession, "session-admin-1")
            assert stored_session is not None
            assert stored_session.access_token_hash == token_hash(first_token)
            assert first_token not in stored_session.access_token_hash

        profile = await client.get(
            "/api/v1/auth/me",
            headers={
                "Authorization": f"Bearer {first_token}",
                "X-Request-ID": "auth-profile-0001",
            },
        )
        assert profile.status_code == 200, profile.text
        profile_body = profile.json()
        assert profile_body["organizationID"] == ORG_ID
        assert profile_body["principalID"] == USER_ID
        assert profile_body["membershipID"] == MEMBERSHIP_ID
        assert profile_body["authorizationRevision"] == 1
        assert set(profile_body["capabilities"]) == {"sync", "item.edit"}

        second = await client.post(
            "/internal/admin/v1/bootstrap",
            json=bootstrap_payload(session_id="session-admin-2", expanded_authority=True),
            headers={"X-Vitrial-Admin-Key": ADMIN_KEY},
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        second_token = second_body["accessToken"]
        assert second_body["authorizationRevision"] == 2

        refreshed = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert refreshed.status_code == 200
        refreshed_body = refreshed.json()
        assert refreshed_body["authorizationRevision"] == 2
        assert refreshed_body["allCustomers"] is True
        assert set(refreshed_body["capabilities"]) == {"sync", "item.edit", "item.create"}

        revoked = await client.post(
            "/internal/admin/v1/sessions/session-admin-1/revoke",
            json={"organizationID": ORG_ID},
            headers={"X-Vitrial-Admin-Key": ADMIN_KEY},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["sessionID"] == "session-admin-1"

        revoked_again = await client.post(
            "/internal/admin/v1/sessions/session-admin-1/revoke",
            json={"organizationID": ORG_ID},
            headers={"X-Vitrial-Admin-Key": ADMIN_KEY},
        )
        assert revoked_again.status_code == 200
        assert revoked_again.json()["revokedAt"] == revoked.json()["revokedAt"]

        rejected_profile = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert rejected_profile.status_code == 401

        active_profile = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {second_token}"},
        )
        assert active_profile.status_code == 200
        assert active_profile.json()["sessionID"] == "session-admin-2"

    await clear_fixture()
