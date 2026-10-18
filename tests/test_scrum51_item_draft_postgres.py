"""Real synthetic PostgreSQL V2 sync tests for job-specific Item draft CRUD."""
import base64
import copy
import json
import hashlib
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db import SessionFactory
from app.idempotency import SyncMutationFingerprint
from app.main import app
from app.models import (
    AuthSession, CanonicalCustomer, CanonicalItem, CanonicalProject,
    CanonicalProjectSector, Membership, Organization, SyncChangeLog,
    SyncEntity, SyncMutation, User,
)
from app.settings import settings
from test_scrum51_finished_item_draft import draft

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="disposable PostgreSQL required",
)
ADMIN = "synthetic-item-draft-admin-key"


async def cleanup(org_ids):
    async with SessionFactory() as db:
        users = (await db.scalars(select(Membership.user_id).where(
            Membership.organization_id.in_(org_ids)))).all()
        for cls in (SyncChangeLog, SyncEntity, SyncMutation, SyncMutationFingerprint,
                    CanonicalItem, CanonicalProjectSector, CanonicalProject,
                    CanonicalCustomer, AuthSession, Membership):
            await db.execute(delete(cls).where(cls.organization_id.in_(org_ids)))
        await db.flush()
        if users:
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))
        await db.commit()


@pytest.mark.asyncio
async def test_item_draft_create_edit_and_company_separation(monkeypatch):
    nonce = uuid4().hex[:14]
    org_a = "synthetic-item-A-" + nonce
    org_b = "synthetic-item-B-" + nonce
    customer = "synthetic-customer-" + nonce
    project = "synthetic-project-" + nonce
    sector = "synthetic-sector-" + nonce
    item_id = "synthetic-item-" + nonce
    orgs = (org_a, org_b)
    monkeypatch.setattr(
        settings, "admin_api_key_hash",
        hashlib.sha256(ADMIN.encode()).hexdigest()
    )
    await cleanup(orgs)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            async def admission(org, label):
                data = {
                    "organizationID": org,
                    "organizationName": "Synthetic Draft Company",
                    "userID": "user-" + label + nonce,
                    "displayName": "Synthetic " + label,
                    "email": label + nonce + "@example.invalid",
                    "membershipID": "member-" + label + nonce,
                    "active": True,
                    "sharedCompanyAccess": True,
                    "allCustomers": True, "allProjects": True,
                    "customerIDs": [], "projectIDs": [],
                    "roles": [],
                    "capabilities": ["sync", "item.create", "item.edit"],
                    "pilotSeatLimit": 3,
                    "sessionID": "session-" + uuid4().hex,
                    "sessionTTLSeconds": 3600,
                }
                r = await client.post("/internal/admin/v1/bootstrap",
                    headers={"X-Vitrial-Admin-Key": ADMIN}, json=data)
                assert r.status_code == 200, r.text
                return {"Authorization": "Bearer " + r.json()["accessToken"]}

            actor_a = await admission(org_a, "a")
            actor_b = await admission(org_b, "b")
            async with SessionFactory() as db:
                for org in orgs:
                    db.add(CanonicalCustomer(
                        organization_id=org, customer_id=customer))
                    await db.flush()
                    db.add(CanonicalProject(
                        organization_id=org, customer_id=customer,
                        project_id=project))
                    await db.flush()
                    db.add(CanonicalProjectSector(
                        organization_id=org, project_id=project,
                        project_sector_id=sector,
                        sector_id="sector-aluminum-glass-steel"))
                await db.commit()
            def payload(marker):
                d = draft()
                d["id"] = item_id
                d["projectID"] = project
                d["projectSectorID"] = sector
                d["jobItemDraft"]["itemID"] = item_id
                d["jobItemDraft"]["projectID"] = project
                d["jobItemDraft"]["projectSectorID"] = sector
                d["jobItemDraft"]["nameEsCO"] = "Synthetic " + marker
                return d
            async def push(headers, data, suffix, base_revision=None):
                record = {
                    "id": "record-" + suffix + nonce,
                    "clientMutationID": "mutation-" + suffix + nonce,
                    "entityID": item_id, "entityType": "item",
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                    "payload": data,
                }
                if base_revision is not None:
                    record["baseServerRevision"] = base_revision
                return await client.post("/api/v2/sync/push", headers=headers,
                    json={"protocolVersion": 2, "deviceID": "synthetic-device",
                          "records": [record]})
            good_a = payload("A")
            bad = copy.deepcopy(good_a)
            bad["jobItemDraft"]["components"][0]["vendorSKU"] = "fabricated"
            denied = await push(actor_a, bad, "bad")
            assert denied.status_code == 200
            assert denied.json()["rejectedRecordIDs"] == ["record-bad" + nonce]
            # Legacy V1 base64 wire reaches the SAME fail-closed Item draft
            # validator. An invalid supplier SKU cannot bypass it.
            v1_record = {
                "id": "v1-invalid-" + nonce,
                "entityType": "item", "entityID": item_id,
                "clientMutationID": "v1-invalid-" + nonce,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "payload": base64.b64encode(json.dumps(bad).encode()).decode(),
            }
            v1_invalid = await client.post("/api/v1/sync/push",
                headers=actor_a,
                json={"deviceID": "synthetic-old-device", "records": [v1_record]})
            assert v1_invalid.status_code == 200
            assert v1_invalid.json()["rejectedRecordIDs"] == ["v1-invalid-" + nonce]
            accepted = await push(actor_a, good_a, "a")
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["acceptedRecordIDs"] == ["record-a" + nonce]
            # Identical item IDs are isolated by the authenticated tenant.
            good_b = payload("B")
            accepted_b = await push(actor_b, good_b, "b")
            assert accepted_b.status_code == 200
            assert accepted_b.json()["acceptedRecordIDs"] == ["record-b" + nonce]
            async with SessionFactory() as db:
                a = await db.get(SyncEntity, (org_a, "item", item_id))
                b = await db.get(SyncEntity, (org_b, "item", item_id))
                assert a is not None and b is not None
                assert a.payload_json["jobItemDraft"]["nameEsCO"] == "Synthetic A"
                assert b.payload_json["jobItemDraft"]["nameEsCO"] == "Synthetic B"
                revision = a.server_revision
            update = copy.deepcopy(good_a)
            update["jobItemDraft"]["components"].append({
                "id": "new-hinge", "kind": "hardware", "name": "Bisagra",
                "quantity": "2", "unit": "each",
            })
            edited = await push(actor_a, update, "edit", revision)
            assert edited.json()["acceptedRecordIDs"] == ["record-edit" + nonce]
            stale = await push(actor_a, good_a, "stale", revision)
            assert stale.json()["rejectedRecordIDs"] == ["record-stale" + nonce]
            for actor, expected in ((actor_a, "Synthetic A"),
                                    (actor_b, "Synthetic B")):
                r = await client.get("/api/v2/sync/pull?cursor=seq:0",
                                     headers=actor)
                assert r.status_code == 200
                rows = [x for x in r.json()["records"]
                        if x["entityType"] == "item" and x["entityID"] == item_id]
                # The sync protocol may deliver multiple retained change-log
                # revisions for the same Item, not one collapsed snapshot.
                assert len(rows) == (2 if expected == "Synthetic A" else 1)
                assert all(row["payload"]["jobItemDraft"]["nameEsCO"] == expected
                           for row in rows)
                if expected == "Synthetic A":
                    assert any(any(part["id"] == "new-hinge"
                                   for part in row["payload"]["jobItemDraft"]["components"])
                               for row in rows)
            assert (await client.get("/api/v2/sync/pull")).status_code == 401
    finally:
        await cleanup(orgs)
