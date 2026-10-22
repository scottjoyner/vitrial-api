"""Disposable-PostgreSQL HTTP acceptance: real paired sessions, not a Demo store.

No network endpoint is contacted. ASGITransport exercises FastAPI directly
against the job's migrated ephemeral PostgreSQL. A separate DB session performs
an independent readback. The current iOS binary is NOT exercised by this test.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.db import SessionFactory
from app.main import app
from app.models import (
    AuthSession, CanonicalCustomer, CanonicalItem, CanonicalItemChild,
    CanonicalProject, CanonicalProjectChild, CanonicalProjectSector,
    Membership, Organization, SyncChangeLog, SyncEntity, SyncMutation, User,
)
from app.pairing import PairingGrant
from app.settings import settings

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated disposable PostgreSQL integration database",
)

ORG = "org-connected-http-proof"
USERS = ("user-connected-http-a", "user-connected-http-b")
MEMBERS = ("membership-connected-http-a", "membership-connected-http-b")
ADMIN = "ephemeral-ci-only-admin-key"
NOW = "2026-10-14T18:00:00Z"


def _record(kind: str, entity_id: str, payload: dict) -> dict:
    return {
        "id": "record-http-" + kind,
        "entityType": kind,
        "entityID": entity_id,
        "updatedAt": NOW,
        "payload": base64.b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).decode(),
        "baseServerRevision": None,
        "clientMutationID": "mutation-http-" + kind,
    }


async def _clear_synthetic_org() -> None:
    async with SessionFactory() as db:
        for model in (
            SyncChangeLog, SyncMutation, SyncEntity,
            CanonicalItemChild, CanonicalProjectChild, CanonicalItem,
            CanonicalProjectSector, CanonicalProject, CanonicalCustomer,
            PairingGrant, AuthSession, Membership,
        ):
            await db.execute(delete(model).where(model.organization_id == ORG))
        for user in USERS:
            await db.execute(delete(User).where(User.id == user))
        await db.execute(delete(Organization).where(Organization.id == ORG))
        await db.commit()


async def _pair(client: AsyncClient, admin_headers: dict, member: str) -> str:
    grant = await client.post(
        "/internal/admin/v1/pairing-grants", headers=admin_headers,
        json={"membershipID": member, "grantTTLSeconds": 300,
              "sessionTTLSeconds": 3600},
    )
    assert grant.status_code == 200, grant.text
    code = grant.json()["pairingCode"]
    paired = await client.post("/api/v1/auth/pair", json={"pairingCode": code})
    assert paired.status_code == 200, paired.text
    assert paired.headers.get("Cache-Control") == "no-store"
    assert set(("accessToken", "expiresAt", "refreshToken",
                "refreshTokenExpiresAt")).issubset(paired.json())
    rejected = await client.post("/api/v1/auth/pair", json={"pairingCode": code})
    assert rejected.status_code == 401, "the same one-time grant must not pair twice"
    return paired.json()["accessToken"]


@pytest.mark.asyncio
async def test_two_real_pairings_create_http_graph_with_pg_readback_replay_and_revocation(
    monkeypatch,
):
    await _clear_synthetic_org()
    monkeypatch.setattr(
        settings, "admin_api_key_hash", hashlib.sha256(ADMIN.encode()).hexdigest()
    )
    admin_headers = {"X-Vitrial-Admin-Key": ADMIN}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://disposable-test"
        ) as client:
            for label, user, membership in zip(("A", "B"), USERS, MEMBERS):
                response = await client.post(
                    "/internal/admin/v1/bootstrap", headers=admin_headers,
                    json={
                        "organizationID": ORG,
                        "organizationName": "Disposable Connected Test",
                        "userID": user, "displayName": "Operator " + label,
                        "membershipID": membership, "active": True,
                        "allCustomers": True, "allProjects": True,
                        "customerIDs": [], "projectIDs": [],
                        "roles": [{"id": "operator", "displayName": "Operator"}],
                        "capabilities": [
                            "sync", "customer.create", "project.create",
                            "project.edit", "item.create",
                        ],
                        "sessionID": "bootstrap-connected-http-" + label,
                        "sessionTTLSeconds": 3600,
                    },
                )
                assert response.status_code == 200, response.text

            token_a = await _pair(client, admin_headers, MEMBERS[0])
            token_b = await _pair(client, admin_headers, MEMBERS[1])
            headers_a = {"Authorization": f"Bearer {token_a}"}
            headers_b = {"Authorization": f"Bearer {token_b}"}

            for header, user in ((headers_a, USERS[0]), (headers_b, USERS[1])):
                me = await client.get("/api/v1/auth/me", headers=header)
                assert me.status_code == 200, me.text
                profile = me.json()
                assert profile["principalID"] == user
                assert profile["organizationID"] == ORG
                assert profile["allCustomers"] is True
                assert profile["allProjects"] is True
                assert "sync" in profile["capabilities"]

            # Deliberate out-of-order graph, as the real offline app can
            # enqueue parent and child edits in different local orders.
            records = [
                _record("item", "item-connected-http", {
                    "id": "item-connected-http",
                    "projectID": "project-connected-http",
                    "projectSectorID": "sector-connected-http",
                }),
                _record("project_sector", "sector-connected-http", {
                    "id": "sector-connected-http",
                    "projectID": "project-connected-http",
                    "sectorID": "sector-aluminum-glass-steel",
                }),
                _record("project", "project-connected-http", {
                    "id": "project-connected-http",
                    "customerID": "customer-connected-http",
                    "name": "Real Project Test",
                }),
                _record("customer", "customer-connected-http", {
                    "id": "customer-connected-http",
                    "name": "Real Customer Test",
                }),
            ]
            payload = {"deviceID": "device-a-ci", "records": records}
            pushed = await client.post("/api/v1/sync/push", headers=headers_a, json=payload)
            assert pushed.status_code == 200, pushed.text
            response_data = pushed.json()
            assert set(response_data["acceptedRecordIDs"]) == {r["id"] for r in records}
            assert response_data["rejectedRecordIDs"] == []

            # A separate SQL connection verifies actual durable rows, not an
            # ASGI stub or echoed HTTP response.
            async with SessionFactory() as db:
                customer = await db.get(
                    CanonicalCustomer, (ORG, "customer-connected-http")
                )
                project = await db.get(
                    CanonicalProject, (ORG, "project-connected-http")
                )
                item = await db.get(CanonicalItem, (ORG, "item-connected-http"))
                assert customer is not None
                assert project is not None and project.customer_id == customer.customer_id
                assert item is not None and item.project_id == project.project_id
                assert item.project_sector_id == "sector-connected-http"
                changes_before = await db.scalar(
                    select(func.count()).select_from(SyncChangeLog)
                    .where(SyncChangeLog.organization_id == ORG)
                )
                assert changes_before == 4

            pulled = await client.get(
                "/api/v1/sync/pull", headers=headers_b, params={"cursor": "seq:0"}
            )
            assert pulled.status_code == 200, pulled.text
            received = pulled.json()["records"]
            assert {r["entityID"] for r in received} == {
                "item-connected-http", "sector-connected-http",
                "project-connected-http", "customer-connected-http",
            }
            assert {r["clientMutationID"] for r in received} == {
                "mutation-http-item", "mutation-http-project_sector",
                "mutation-http-project", "mutation-http-customer",
            }
            assert all(r["serverRevision"] >= 1 for r in received)

            # An uncertain HTTP acknowledgment must be idempotent on replay,
            # not create a second business event.
            replay = await client.post("/api/v1/sync/push", headers=headers_a, json=payload)
            assert replay.status_code == 200, replay.text
            assert set(replay.json()["acceptedRecordIDs"]) == {r["id"] for r in records}
            async with SessionFactory() as db:
                changes_after = await db.scalar(
                    select(func.count()).select_from(SyncChangeLog)
                    .where(SyncChangeLog.organization_id == ORG)
                )
                assert changes_after == changes_before

            logout = await client.post("/api/v1/auth/logout", headers=headers_a)
            assert logout.status_code == 204, logout.text
            revoked = await client.get("/api/v1/auth/me", headers=headers_a)
            assert revoked.status_code == 401
            still_valid = await client.get("/api/v1/auth/me", headers=headers_b)
            assert still_valid.status_code == 200
    finally:
        await _clear_synthetic_org()
