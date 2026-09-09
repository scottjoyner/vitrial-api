import base64
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.auth import Principal
from app.db import SessionFactory
from app.idempotency import SyncMutationFingerprint
from app.models import (
    CanonicalCustomer,
    Organization,
    SyncChangeLog,
    SyncEntity,
    SyncMutation,
)
from app.reference_data import (
    PublicationConflict,
    ReferenceEntry,
    ReferencePublicationCreate,
    ReferencePublicationPayload,
    current_publications,
    get_publication,
    publish_reference,
)
from app.reference_models import ReferencePublication
from app.schemas import SyncBatch
from app.sync_service import apply_push
from app.sync_v2 import SyncBatchV2, apply_push_v2, pull_since_v2

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)


def principal(*, capabilities: set[str], organization_id: str = "org-scrum21") -> Principal:
    return Principal(
        user_id="user-scrum21",
        organization_id=organization_id,
        membership_id="membership-scrum21",
        session_id="session-scrum21",
        authorization_revision=1,
        capabilities=frozenset(capabilities),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )


async def clear_scrum21(db):
    for model in (
        ReferencePublication,
        SyncMutationFingerprint,
        SyncChangeLog,
        SyncMutation,
        SyncEntity,
        CanonicalCustomer,
        Organization,
    ):
        await db.execute(delete(model))
    await db.commit()


@pytest.mark.asyncio
async def test_reference_publications_are_immutable_effective_and_historical():
    actor = principal(capabilities={"catalog.manage", "pricing.manage", "sync"})
    async with SessionFactory() as db:
        await clear_scrum21(db)
        db.add(Organization(id=actor.organization_id, name="SCRUM-21", authorization_revision=1))
        await db.commit()

        baseline = await current_publications(db, actor)
        assert {p.kind for p in baseline.publications} == {
            "catalog", "compatibility_rules", "price_book"
        }
        old_catalog = next(p for p in baseline.publications if p.kind == "catalog")
        assert old_catalog.versionID == "configurator-catalog-v1"

        now = datetime.now(timezone.utc)
        request = ReferencePublicationCreate(
            publicationID="catalog-authoritative-2026-09",
            kind="catalog",
            versionID="catalog-2026.09.09",
            effectiveFrom=now - timedelta(seconds=1),
            supersedesPublicationID=old_catalog.publicationID,
            payload=ReferencePublicationPayload(
                source="operator-publication",
                authoritative=True,
                entries=[
                    ReferenceEntry(
                        id="vendor-profile-001",
                        kind="vendor_material",
                        code="PROFILE_001",
                        name="Authoritative Vendor Profile",
                        vendorSKU="SKU-001",
                        unitCostMinor=12500,
                        currency="USD",
                    )
                ],
            ),
        )
        published = await publish_reference(db, actor, request)
        assert published.versionID == request.versionID
        assert published.payload.entries[0].vendorSKU == "SKU-001"
        assert published.payload.entries[0].unitCostMinor == 12500

        current = await current_publications(db, actor)
        current_catalog = next(p for p in current.publications if p.kind == "catalog")
        assert current_catalog.publicationID == request.publicationID

        historical = await get_publication(db, actor, old_catalog.publicationID)
        assert historical.versionID == "configurator-catalog-v1"
        assert historical.contentSHA256 == old_catalog.contentSHA256

        conflicting = request.model_copy(
            update={
                "payload": ReferencePublicationPayload(
                    source="different-content",
                    authoritative=True,
                )
            }
        )
        with pytest.raises(PublicationConflict, match="immutable"):
            await publish_reference(db, actor, conflicting)

        await clear_scrum21(db)


@pytest.mark.asyncio
async def test_v2_native_json_reuses_canonical_revision_engine_and_is_replay_safe():
    actor = principal(capabilities={"sync", "customer.create", "customer.edit"})
    async with SessionFactory() as db:
        await clear_scrum21(db)
        db.add(Organization(id=actor.organization_id, name="SCRUM-21", authorization_revision=1))
        await db.commit()

        now = datetime.now(timezone.utc).isoformat()
        payload = {"id": "customer-v2-integration", "name": "Native JSON Customer", "status": "active"}
        batch = SyncBatchV2.model_validate({
            "protocolVersion": 2,
            "deviceID": "device-v2-integration",
            "cursor": "seq:0",
            "records": [{
                "id": "record-v2-integration",
                "entityType": "customer",
                "entityID": "customer-v2-integration",
                "updatedAt": now,
                "payload": payload,
                "entitySchemaVersion": 1,
                "baseServerRevision": None,
                "clientMutationID": "mutation-v2-integration",
                "deletedAt": None,
            }],
        })

        first = await apply_push_v2(db, actor, batch)
        assert first.acceptedRecordIDs == ["record-v2-integration"]
        replay = await apply_push_v2(db, actor, batch)
        assert replay.acceptedRecordIDs == ["record-v2-integration"]

        pulled = await pull_since_v2(db, actor, "seq:0")
        assert pulled.protocolVersion == 2
        assert len(pulled.records) == 1
        assert pulled.records[0].payload == payload
        assert pulled.records[0].entitySchemaVersion == 1
        assert pulled.records[0].serverRevision is not None

        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        cross_protocol = SyncBatch.model_validate({
            "deviceID": "device-v1-cross-protocol",
            "cursor": "seq:0",
            "records": [{
                "id": "record-v1-cross-protocol",
                "entityType": "customer",
                "entityID": "customer-v2-integration",
                "updatedAt": now,
                "payload": base64.b64encode(canonical).decode("ascii"),
                "baseServerRevision": None,
                "clientMutationID": "mutation-v2-integration",
                "deletedAt": None,
            }],
        })
        collision = await apply_push(db, actor, cross_protocol)
        assert collision.acceptedRecordIDs == []
        assert collision.rejectedRecordIDs == ["record-v1-cross-protocol"]

        await clear_scrum21(db)
