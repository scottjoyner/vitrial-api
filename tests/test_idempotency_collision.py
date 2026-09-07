from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, func, select

from app.auth import Principal
from app.db import SessionFactory
from app.idempotency import SyncMutationFingerprint
from app.models import (
    AuthSession,
    CanonicalCustomer,
    CanonicalItem,
    CanonicalItemChild,
    CanonicalProject,
    CanonicalProjectChild,
    CanonicalProjectSector,
    EvidenceBlob,
    Membership,
    Organization,
    SyncChangeLog,
    SyncEntity,
    SyncMutation,
    User,
)
from app.schemas import SyncBatch
from app.sync_service import apply_push

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

NOW = "2026-09-07T12:30:00Z"


def encoded(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def actor() -> Principal:
    return Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=1,
        capabilities=frozenset({"sync", "item.measurements.manage"}),
        customer_ids=frozenset({"customer-1"}),
        project_ids=frozenset({"project-1"}),
        all_customers=False,
        all_projects=False,
    )


def mutation(record_id: str, note: str, *, mutation_id: str, base_revision: int = 5) -> dict:
    return {
        "id": record_id,
        "entityType": "measurement",
        "entityID": "measurement-1",
        "updatedAt": NOW,
        "payload": encoded({
            "id": "measurement-1",
            "itemID": "item-1",
            "evidenceReferences": [],
            "note": note,
        }),
        "baseServerRevision": base_revision,
        "clientMutationID": mutation_id,
        "deletedAt": None,
    }


async def clear_database(db) -> None:
    # Fingerprints are deleted explicitly here so the test also proves migration/model access.
    await db.execute(delete(SyncMutationFingerprint))
    for model in (
        EvidenceBlob,
        SyncChangeLog,
        SyncMutation,
        SyncEntity,
        CanonicalItemChild,
        CanonicalProjectChild,
        CanonicalItem,
        CanonicalProjectSector,
        CanonicalProject,
        CanonicalCustomer,
        AuthSession,
        Membership,
        User,
        Organization,
    ):
        await db.execute(delete(model))
    await db.commit()


async def seed_measurement(db) -> None:
    db.add(Organization(id="org-1", name="Vitrial", authorization_revision=1))
    await db.flush()
    db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-1"))
    await db.flush()
    db.add(CanonicalProject(
        organization_id="org-1", project_id="project-1", customer_id="customer-1"
    ))
    await db.flush()
    db.add(CanonicalProjectSector(
        organization_id="org-1",
        project_sector_id="project-sector-1",
        project_id="project-1",
        sector_id="sector-aluminum-glass-steel",
    ))
    await db.flush()
    db.add(CanonicalItem(
        organization_id="org-1",
        item_id="item-1",
        project_id="project-1",
        project_sector_id="project-sector-1",
    ))
    db.add(CanonicalItemChild(
        organization_id="org-1",
        entity_type="measurement",
        entity_id="measurement-1",
        item_id="item-1",
    ))
    db.add(SyncEntity(
        organization_id="org-1",
        entity_type="measurement",
        entity_id="measurement-1",
        server_revision=5,
        schema_version=1,
        payload_json={
            "id": "measurement-1",
            "itemID": "item-1",
            "evidenceReferences": [],
            "note": "baseline",
        },
        updated_at=datetime.now(timezone.utc),
        deleted_at=None,
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_duplicate_mutation_id_inside_batch_rejects_different_request_bytes():
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_measurement(db)
        batch = SyncBatch.model_validate({
            "deviceID": "device-collision",
            "records": [
                mutation("record-first", "first", mutation_id="mutation-same"),
                mutation("record-second", "second", mutation_id="mutation-same"),
            ],
        })
        result = await apply_push(db, actor(), batch)
        assert result.acceptedRecordIDs == ["record-first"]
        assert result.rejectedRecordIDs == ["record-second"]

        entity = await db.get(SyncEntity, ("org-1", "measurement", "measurement-1"))
        mutation_count = await db.scalar(
            select(func.count()).select_from(SyncMutation).where(
                SyncMutation.organization_id == "org-1",
                SyncMutation.client_mutation_id == "mutation-same",
            )
        )
        fingerprint_count = await db.scalar(
            select(func.count()).select_from(SyncMutationFingerprint).where(
                SyncMutationFingerprint.organization_id == "org-1",
                SyncMutationFingerprint.client_mutation_id == "mutation-same",
            )
        )
        assert entity is not None and entity.server_revision == 6
        assert entity.payload_json["note"] == "first"
        assert mutation_count == 1
        assert fingerprint_count == 1
        await clear_database(db)


@pytest.mark.asyncio
async def test_committed_mutation_id_replays_exact_request_but_rejects_changed_payload():
    async with SessionFactory() as db:
        await clear_database(db)
        await seed_measurement(db)
        exact = SyncBatch.model_validate({
            "deviceID": "device-original",
            "records": [mutation("record-original", "original", mutation_id="mutation-fixed")],
        })
        first = await apply_push(db, actor(), exact)
        assert first.acceptedRecordIDs == ["record-original"]

        replay = await apply_push(db, actor(), exact)
        assert replay.acceptedRecordIDs == ["record-original"]

        collision = SyncBatch.model_validate({
            "deviceID": "device-other",
            "records": [mutation("record-collision", "different", mutation_id="mutation-fixed")],
        })
        rejected = await apply_push(db, actor(), collision)
        assert rejected.rejectedRecordIDs == ["record-collision"]

        entity = await db.get(SyncEntity, ("org-1", "measurement", "measurement-1"))
        change_count = await db.scalar(
            select(func.count()).select_from(SyncChangeLog).where(
                SyncChangeLog.organization_id == "org-1"
            )
        )
        assert entity is not None and entity.server_revision == 6
        assert entity.payload_json["note"] == "original"
        assert change_count == 1
        await clear_database(db)
