from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, func, select

from app.auth import Principal
from app.db import SessionFactory
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

NOW = "2026-09-07T12:00:00Z"


def encoded(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def record(
    record_id: str,
    payload: dict,
    mutation_id: str,
    *,
    base_revision: int = 5,
    deleted_at: str | None = None,
) -> dict:
    return {
        "id": record_id,
        "entityType": "measurement",
        "entityID": "measurement-1",
        "updatedAt": NOW,
        "payload": encoded(payload),
        "baseServerRevision": base_revision,
        "clientMutationID": mutation_id,
        "deletedAt": deleted_at,
    }


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


async def clear_database(db) -> None:
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


async def seed_measurement() -> None:
    async with SessionFactory() as db:
        await clear_database(db)
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


async def push_in_fresh_session(batch: SyncBatch):
    async with SessionFactory() as db:
        return await apply_push(db, actor(), batch)


async def cleanup() -> None:
    async with SessionFactory() as db:
        await clear_database(db)


@pytest.mark.asyncio
async def test_concurrent_same_mutation_id_is_one_commit_and_two_idempotent_accepts():
    await seed_measurement()
    payload = {
        "id": "measurement-1",
        "itemID": "item-1",
        "evidenceReferences": [],
        "note": "same replay",
    }
    batch = SyncBatch.model_validate({
        "deviceID": "device-race",
        "records": [record("record-replay", payload, "mutation-replay")],
    })

    first, second = await asyncio.gather(
        push_in_fresh_session(batch),
        push_in_fresh_session(batch),
    )
    assert first.acceptedRecordIDs == ["record-replay"]
    assert second.acceptedRecordIDs == ["record-replay"]

    async with SessionFactory() as db:
        entity = await db.get(SyncEntity, ("org-1", "measurement", "measurement-1"))
        mutation_count = await db.scalar(
            select(func.count()).select_from(SyncMutation).where(
                SyncMutation.organization_id == "org-1",
                SyncMutation.client_mutation_id == "mutation-replay",
            )
        )
        change_count = await db.scalar(
            select(func.count()).select_from(SyncChangeLog).where(
                SyncChangeLog.organization_id == "org-1"
            )
        )
        assert entity is not None and entity.server_revision == 6
        assert mutation_count == 1
        assert change_count == 1
    await cleanup()


@pytest.mark.asyncio
async def test_concurrent_distinct_writers_on_same_base_have_exactly_one_winner():
    await seed_measurement()
    left = SyncBatch.model_validate({
        "deviceID": "device-left",
        "records": [record("record-left", {
            "id": "measurement-1", "itemID": "item-1",
            "evidenceReferences": [], "note": "left",
        }, "mutation-left")],
    })
    right = SyncBatch.model_validate({
        "deviceID": "device-right",
        "records": [record("record-right", {
            "id": "measurement-1", "itemID": "item-1",
            "evidenceReferences": [], "note": "right",
        }, "mutation-right")],
    })

    results = await asyncio.gather(
        push_in_fresh_session(left),
        push_in_fresh_session(right),
    )
    assert sum(bool(result.acceptedRecordIDs) for result in results) == 1
    assert sum(bool(result.rejectedRecordIDs) for result in results) == 1

    async with SessionFactory() as db:
        entity = await db.get(SyncEntity, ("org-1", "measurement", "measurement-1"))
        mutations = (
            await db.scalars(
                select(SyncMutation).where(
                    SyncMutation.organization_id == "org-1",
                    SyncMutation.client_mutation_id.in_(["mutation-left", "mutation-right"]),
                )
            )
        ).all()
        change_count = await db.scalar(
            select(func.count()).select_from(SyncChangeLog).where(
                SyncChangeLog.organization_id == "org-1"
            )
        )
        assert entity is not None and entity.server_revision == 6
        assert entity.payload_json["note"] in {"left", "right"}
        assert {mutation.result_status for mutation in mutations} == {"accepted", "rejected"}
        assert change_count == 1
    await cleanup()


@pytest.mark.asyncio
async def test_concurrent_update_and_tombstone_on_same_base_cannot_both_commit():
    await seed_measurement()
    update = SyncBatch.model_validate({
        "deviceID": "device-update",
        "records": [record("record-update", {
            "id": "measurement-1", "itemID": "item-1",
            "evidenceReferences": [], "note": "updated",
        }, "mutation-update")],
    })
    tombstone = SyncBatch.model_validate({
        "deviceID": "device-delete",
        "records": [record(
            "record-delete", {}, "mutation-delete", deleted_at=NOW
        )],
    })

    results = await asyncio.gather(
        push_in_fresh_session(update),
        push_in_fresh_session(tombstone),
    )
    assert sum(bool(result.acceptedRecordIDs) for result in results) == 1
    assert sum(bool(result.rejectedRecordIDs) for result in results) == 1

    async with SessionFactory() as db:
        entity = await db.get(SyncEntity, ("org-1", "measurement", "measurement-1"))
        child = await db.get(
            CanonicalItemChild, ("org-1", "measurement", "measurement-1")
        )
        mutations = (
            await db.scalars(
                select(SyncMutation).where(
                    SyncMutation.organization_id == "org-1",
                    SyncMutation.client_mutation_id.in_(["mutation-update", "mutation-delete"]),
                )
            )
        ).all()
        change_count = await db.scalar(
            select(func.count()).select_from(SyncChangeLog).where(
                SyncChangeLog.organization_id == "org-1"
            )
        )
        assert entity is not None and entity.server_revision == 6
        assert child is not None
        assert {mutation.result_status for mutation in mutations} == {"accepted", "rejected"}
        assert change_count == 1
        if entity.deleted_at is None:
            assert child.deleted_at is None
            assert entity.payload_json["note"] == "updated"
        else:
            assert child.deleted_at is not None
            assert entity.payload_json is None
    await cleanup()
