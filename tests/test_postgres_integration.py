import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, func, select

from app.auth import Principal, current_principal, token_hash
from app.db import SessionFactory
from app.evidence import put_blob
from app.models import (
    AuthSession,
    CanonicalCustomer,
    CanonicalItem,
    CanonicalProject,
    EvidenceBlob,
    Membership,
    Organization,
    SyncChangeLog,
    SyncEntity,
    SyncMutation,
    User,
)
from app.schemas import SyncBatch
from app.settings import settings
from app.sync_service import apply_push, pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-06T14:00:00Z"


async def clear_database(db):
    for model in (
        EvidenceBlob,
        SyncChangeLog,
        SyncMutation,
        SyncEntity,
        CanonicalItem,
        CanonicalProject,
        CanonicalCustomer,
        AuthSession,
        Membership,
        User,
        Organization,
    ):
        await db.execute(delete(model))
    await db.commit()


def encoded(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def record(
    record_id: str,
    entity_type: str,
    entity_id: str,
    payload: dict,
    mutation_id: str,
    *,
    base_revision: int | None = None,
) -> dict:
    return {
        "id": record_id,
        "entityType": entity_type,
        "entityID": entity_id,
        "updatedAt": NOW,
        "payload": encoded(payload),
        "baseServerRevision": base_revision,
        "clientMutationID": mutation_id,
    }


def principal(
    *,
    capabilities: set[str],
    customer_ids: set[str] | None = None,
    project_ids: set[str] | None = None,
    organization_id: str = "org-1",
    membership_id: str = "membership-1",
    user_id: str = "user-1",
    revision: int = 1,
) -> Principal:
    return Principal(
        user_id=user_id,
        organization_id=organization_id,
        membership_id=membership_id,
        session_id="session-1",
        authorization_revision=revision,
        capabilities=frozenset(capabilities),
        customer_ids=frozenset(customer_ids or set()),
        project_ids=frozenset(project_ids or set()),
        all_customers=False,
        all_projects=False,
    )


@pytest.mark.asyncio
async def test_session_authentication_and_revocation():
    token = "integration-access-token"
    now = datetime.now(timezone.utc)

    async with SessionFactory() as db:
        await clear_database(db)

        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=9))
        db.add(User(id="user-1", display_name="Operator", email="operator@example.invalid"))
        await db.flush()

        db.add(Membership(
            id="membership-1",
            organization_id="org-1",
            user_id="user-1",
            active=True,
            all_customers=False,
            all_projects=False,
            customer_ids=["customer-1"],
            project_ids=["project-1"],
            roles=[{"id": "operator", "displayName": "Operator"}],
            capabilities=["sync", "item.edit"],
        ))
        await db.flush()

        session = AuthSession(
            id="session-1",
            organization_id="org-1",
            membership_id="membership-1",
            user_id="user-1",
            access_token_hash=token_hash(token),
            issued_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        )
        db.add(session)
        await db.commit()

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        authenticated = await current_principal(credentials, db)
        assert authenticated.organization_id == "org-1"
        assert authenticated.authorization_revision == 9
        assert authenticated.project_ids == frozenset({"project-1"})

        session.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await current_principal(credentials, db)
        assert exc.value.status_code == 401

        await clear_database(db)


@pytest.mark.asyncio
async def test_sync_idempotency_stale_rejection_and_pull_acknowledgement():
    fixture = json.loads(
        (ROOT / "contracts/backend/v1/sync-push.request.json").read_text(encoding="utf-8")
    )
    batch = SyncBatch.model_validate(fixture)
    actor = principal(
        capabilities={"sync", "customer.edit"},
        customer_ids={"customer-001"},
        revision=4,
    )

    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=4))
        db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-001"))
        db.add(SyncEntity(
            organization_id="org-1",
            entity_type="customer",
            entity_id="customer-001",
            server_revision=7,
            schema_version=1,
            payload_json={"id": "customer-001", "name": "Before"},
            updated_at=datetime.now(timezone.utc),
            deleted_at=None,
        ))
        await db.commit()

        first = await apply_push(db, actor, batch)
        assert first.acceptedRecordIDs == ["record-001"]
        assert first.rejectedRecordIDs == []

        entity = await db.get(SyncEntity, ("org-1", "customer", "customer-001"))
        assert entity is not None
        assert entity.server_revision == 8

        replay = await apply_push(db, actor, batch)
        assert replay.acceptedRecordIDs == ["record-001"]
        change_count = await db.scalar(
            select(func.count()).select_from(SyncChangeLog).where(
                SyncChangeLog.organization_id == "org-1"
            )
        )
        assert change_count == 1

        stale_payload = fixture.copy()
        stale_payload["records"] = [dict(fixture["records"][0])]
        stale_payload["records"][0]["id"] = "record-stale"
        stale_payload["records"][0]["clientMutationID"] = "mutation-stale"
        stale_payload["records"][0]["baseServerRevision"] = 7
        stale = await apply_push(db, actor, SyncBatch.model_validate(stale_payload))
        assert stale.acceptedRecordIDs == []
        assert stale.rejectedRecordIDs == ["record-stale"]

        pulled = await pull_since(db, actor, "seq:0")
        assert len(pulled.records) == 1
        assert pulled.records[0].serverRevision == 8
        assert pulled.records[0].clientMutationID == "mutation-001"
        assert pulled.cursor and pulled.cursor.startswith("seq:")

        await clear_database(db)


@pytest.mark.asyncio
async def test_out_of_order_create_batch_becomes_server_scope_and_bumps_revision():
    actor = principal(capabilities={
        "sync", "customer.create", "project.create", "item.create",
    })
    batch = SyncBatch.model_validate({
        "deviceID": "device-create",
        # Deliberately child-first: server must prove the hierarchy independent of client order.
        "records": [
            record(
                "record-item", "item", "item-new",
                {"id": "item-new", "projectID": "project-new", "projectSectorID": "sector-new"},
                "mutation-item",
            ),
            record(
                "record-project", "project", "project-new",
                {"id": "project-new", "customerID": "customer-new", "name": "New Project"},
                "mutation-project",
            ),
            record(
                "record-customer", "customer", "customer-new",
                {"id": "customer-new", "name": "New Customer"},
                "mutation-customer",
            ),
        ],
    })

    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=10))
        db.add(User(id="user-1", display_name="Creator", email="creator@example.invalid"))
        await db.flush()
        db.add(Membership(
            id="membership-1",
            organization_id="org-1",
            user_id="user-1",
            active=True,
            all_customers=False,
            all_projects=False,
            customer_ids=[],
            project_ids=[],
            roles=[{"id": "creator", "displayName": "Creator"}],
            capabilities=["sync", "customer.create", "project.create", "item.create"],
        ))
        await db.commit()

        result = await apply_push(db, actor, batch)
        assert result.acceptedRecordIDs == ["record-item", "record-project", "record-customer"]
        assert result.rejectedRecordIDs == []

        customer = await db.get(CanonicalCustomer, ("org-1", "customer-new"))
        project = await db.get(CanonicalProject, ("org-1", "project-new"))
        item = await db.get(CanonicalItem, ("org-1", "item-new"))
        assert customer is not None
        assert project is not None and project.customer_id == "customer-new"
        assert item is not None and item.project_id == "project-new"

        membership = await db.get(Membership, "membership-1")
        organization = await db.get(Organization, "org-1")
        assert membership is not None
        assert membership.customer_ids == ["customer-new"]
        assert membership.project_ids == ["project-new"]
        assert organization is not None and organization.authorization_revision == 12

        refreshed = principal(
            capabilities={"sync", "customer.create", "project.create", "item.create"},
            customer_ids={"customer-new"},
            project_ids={"project-new"},
            revision=12,
        )
        pulled = await pull_since(db, refreshed, "seq:0")
        assert {entry.entityID for entry in pulled.records} == {
            "customer-new", "project-new", "item-new",
        }

        await clear_database(db)


@pytest.mark.asyncio
async def test_sibling_project_reparent_and_cross_tenant_mutations_fail_closed():
    actor = principal(
        capabilities={"sync", "item.edit", "item.create"},
        customer_ids={"customer-1"},
        project_ids={"project-1"},
    )

    async with SessionFactory() as db:
        await clear_database(db)
        db.add_all([
            Organization(id="org-1", name="Vitrial", authorization_revision=1),
            Organization(id="org-2", name="Other Tenant", authorization_revision=1),
        ])
        await db.flush()
        db.add_all([
            CanonicalCustomer(organization_id="org-1", customer_id="customer-1"),
            CanonicalCustomer(organization_id="org-2", customer_id="customer-2"),
        ])
        await db.flush()
        db.add_all([
            CanonicalProject(organization_id="org-1", project_id="project-1", customer_id="customer-1"),
            CanonicalProject(organization_id="org-1", project_id="project-2", customer_id="customer-1"),
            CanonicalProject(organization_id="org-2", project_id="project-other", customer_id="customer-2"),
        ])
        await db.flush()
        db.add_all([
            CanonicalItem(organization_id="org-1", item_id="item-1", project_id="project-1"),
            CanonicalItem(organization_id="org-1", item_id="item-2", project_id="project-2"),
            CanonicalItem(organization_id="org-2", item_id="item-other", project_id="project-other"),
            SyncEntity(
                organization_id="org-1", entity_type="item", entity_id="item-1",
                server_revision=3, schema_version=1,
                payload_json={"id": "item-1", "projectID": "project-1", "name": "One"},
                updated_at=datetime.now(timezone.utc), deleted_at=None,
            ),
            SyncEntity(
                organization_id="org-1", entity_type="item", entity_id="item-2",
                server_revision=4, schema_version=1,
                payload_json={"id": "item-2", "projectID": "project-2", "name": "Two"},
                updated_at=datetime.now(timezone.utc), deleted_at=None,
            ),
        ])
        await db.flush()
        db.add_all([
            SyncChangeLog(
                organization_id="org-1", entity_type="item", entity_id="item-1",
                server_revision=3, client_mutation_id="seed-1", operation="upsert",
            ),
            SyncChangeLog(
                organization_id="org-1", entity_type="item", entity_id="item-2",
                server_revision=4, client_mutation_id="seed-2", operation="upsert",
            ),
        ])
        await db.commit()

        batch = SyncBatch.model_validate({
            "deviceID": "device-authz",
            "records": [
                record(
                    "record-sibling", "item", "item-2",
                    {"id": "item-2", "projectID": "project-2", "name": "Denied sibling"},
                    "mutation-sibling", base_revision=4,
                ),
                record(
                    "record-reparent", "item", "item-1",
                    {"id": "item-1", "projectID": "project-2", "name": "Illegal move"},
                    "mutation-reparent", base_revision=3,
                ),
                record(
                    "record-cross-tenant", "item", "item-other",
                    {"id": "item-other", "projectID": "project-other", "name": "Steal"},
                    "mutation-cross-tenant",
                ),
                record(
                    "record-allowed", "item", "item-1",
                    {"id": "item-1", "projectID": "project-1", "name": "Allowed"},
                    "mutation-allowed", base_revision=3,
                ),
            ],
        })
        result = await apply_push(db, actor, batch)
        assert result.acceptedRecordIDs == ["record-allowed"]
        assert result.rejectedRecordIDs == [
            "record-sibling", "record-reparent", "record-cross-tenant",
        ]

        item = await db.get(CanonicalItem, ("org-1", "item-1"))
        assert item is not None and item.project_id == "project-1"
        assert await db.get(SyncEntity, ("org-1", "item-other")) is None

        pulled = await pull_since(db, actor, "seq:0")
        assert "item-2" not in {entry.entityID for entry in pulled.records}
        assert "item-1" in {entry.entityID for entry in pulled.records}
        max_sequence = await db.scalar(select(func.max(SyncChangeLog.sequence)))
        assert pulled.cursor == f"seq:{max_sequence}"

        await clear_database(db)


@pytest.mark.asyncio
async def test_unimplemented_child_entity_cannot_use_sync_as_blanket_write_authority():
    actor = principal(
        capabilities={"sync", "item.measurements.manage"},
        customer_ids={"customer-1"},
        project_ids={"project-1"},
    )
    batch = SyncBatch.model_validate({
        "deviceID": "device-child",
        "records": [record(
            "record-measurement", "measurement", "measurement-1",
            {"id": "measurement-1", "itemID": "item-1"},
            "mutation-measurement",
        )],
    })

    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=1))
        db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-1"))
        await db.flush()
        db.add(CanonicalProject(
            organization_id="org-1", project_id="project-1", customer_id="customer-1"
        ))
        await db.flush()
        db.add(CanonicalItem(
            organization_id="org-1", item_id="item-1", project_id="project-1"
        ))
        await db.commit()

        result = await apply_push(db, actor, batch)
        assert result.acceptedRecordIDs == []
        assert result.rejectedRecordIDs == ["record-measurement"]
        assert await db.get(SyncEntity, ("org-1", "measurement", "measurement-1")) is None

        await clear_database(db)


@pytest.mark.asyncio
async def test_evidence_upload_uses_canonical_item_scope_and_capability(tmp_path, monkeypatch):
    allowed = principal(
        capabilities={"sync", "item.evidence.manage"},
        customer_ids={"customer-1"},
        project_ids={"project-1"},
    )
    denied = principal(
        capabilities={"sync", "item.evidence.manage"},
        customer_ids={"customer-1"},
        project_ids={"project-2"},
    )
    monkeypatch.setattr(settings, "evidence_root", tmp_path)
    body = b"canonical evidence"
    digest = hashlib.sha256(body).hexdigest()

    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=1))
        db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-1"))
        await db.flush()
        db.add_all([
            CanonicalProject(
                organization_id="org-1", project_id="project-1", customer_id="customer-1"
            ),
            CanonicalProject(
                organization_id="org-1", project_id="project-2", customer_id="customer-1"
            ),
        ])
        await db.flush()
        db.add(CanonicalItem(
            organization_id="org-1", item_id="item-1", project_id="project-1"
        ))
        await db.commit()

        stored = await put_blob(
            db, allowed, "doc-1", "item-1", "photo.jpg", "image/jpeg", digest, body
        )
        assert stored.item_id == "item-1"

        with pytest.raises(HTTPException) as exc:
            await put_blob(
                db, denied, "doc-2", "item-1", "photo.jpg", "image/jpeg", digest, body
            )
        assert exc.value.status_code == 403
        assert await db.get(EvidenceBlob, ("org-1", "doc-2")) is None

        await clear_database(db)
