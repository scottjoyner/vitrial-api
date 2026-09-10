import base64
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.auth import Principal
from app.db import SessionFactory
from app.models import Membership, Organization, User
from app.reference_data import (
    PublicationConflict,
    ReferenceEntry,
    ReferencePublicationCreate,
    ReferencePublicationPayload,
    current_publications,
    get_publication,
    publish_reference,
)
from app.schemas import SyncBatch
from app.sync_service import apply_push
from app.sync_v2 import SyncBatchV2, apply_push_v2, pull_since_v2

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)


def principal(*, capabilities: set[str], organization_id: str) -> Principal:
    return Principal(
        user_id=f"user-{organization_id}",
        organization_id=organization_id,
        membership_id=f"membership-{organization_id}",
        session_id=f"session-{organization_id}",
        authorization_revision=1,
        capabilities=frozenset(capabilities),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )


def isolated_org(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


async def seed_actor(db, actor: Principal, *, organization_name: str) -> None:
    db.add(Organization(id=actor.organization_id, name=organization_name, authorization_revision=1))
    db.add(User(id=actor.user_id, display_name="SCRUM-21 Test Actor", email=f"{actor.user_id}@example.invalid"))
    # These fixture models intentionally have no ORM relationships, so flush the
    # FK parents before inserting the Membership instead of relying on unit-of-work ordering.
    await db.flush()
    db.add(Membership(
        id=actor.membership_id,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        active=True,
        all_customers=actor.all_customers,
        all_projects=actor.all_projects,
        customer_ids=list(actor.customer_ids),
        project_ids=list(actor.project_ids),
        roles=[{"id": "scrum21-test", "displayName": "SCRUM-21 Test"}],
        capabilities=sorted(actor.capabilities),
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_reference_publications_are_immutable_effective_and_historical():
    organization_id = isolated_org("org-scrum21-reference")
    actor = principal(
        capabilities={"catalog.manage", "pricing.manage", "sync"},
        organization_id=organization_id,
    )
    async with SessionFactory() as db:
        await seed_actor(db, actor, organization_name="SCRUM-21 Reference")

        baseline = await current_publications(db, actor)
        assert {p.kind for p in baseline.publications} == {
            "catalog", "compatibility_rules", "price_book"
        }
        current_baseline_catalog = next(p for p in baseline.publications if p.kind == "catalog")
        assert current_baseline_catalog.versionID == "configurator-catalog-v2"
        assert current_baseline_catalog.supersedesPublicationID == "catalog-bundled-v1"

        sector = next(
            entry for entry in current_baseline_catalog.payload.entries
            if entry.id == "sector-aluminum-glass-steel"
        )
        assert sector.kind == "sector"
        assert sector.code == "ALUMINUM_GLASS_STEEL"

        item_types = [
            entry for entry in current_baseline_catalog.payload.entries
            if entry.kind == "item_type"
        ]
        assert len(item_types) == 7
        assert {entry.attributes["sectorID"] for entry in item_types} == {
            "sector-aluminum-glass-steel"
        }
        assert {entry.code for entry in item_types} == {
            "WINDOW", "DOOR", "BATHROOM_DIVISION", "OFFICE_DIVISION",
            "FACADE", "BALCONY_RAILING", "OTHER",
        }

        original_v1 = await get_publication(db, actor, "catalog-bundled-v1")
        assert original_v1.versionID == "configurator-catalog-v1"
        assert not any(entry.kind in {"sector", "item_type"} for entry in original_v1.payload.entries)

        now = datetime.now(timezone.utc)
        suffix = uuid4().hex
        request = ReferencePublicationCreate(
            publicationID=f"catalog-authoritative-{suffix}",
            kind="catalog",
            versionID=f"catalog-{suffix}",
            effectiveFrom=now - timedelta(seconds=1),
            supersedesPublicationID=current_baseline_catalog.publicationID,
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

        historical = await get_publication(db, actor, current_baseline_catalog.publicationID)
        assert historical.versionID == "configurator-catalog-v2"
        assert historical.contentSHA256 == current_baseline_catalog.contentSHA256

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


@pytest.mark.asyncio
async def test_v2_native_json_reuses_canonical_revision_engine_and_is_replay_safe():
    organization_id = isolated_org("org-scrum21-v2")
    actor = principal(
        capabilities={"sync", "customer.create", "customer.edit"},
        organization_id=organization_id,
    )
    async with SessionFactory() as db:
        await seed_actor(db, actor, organization_name="SCRUM-21 V2")

        suffix = uuid4().hex
        customer_id = f"customer-v2-{suffix}"
        mutation_id = f"mutation-v2-{suffix}"
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "id": customer_id,
            "name": "Native JSON Customer",
            "status": "active",
            "address": "",
            "email": "",
            "phone": "",
            "notes": "",
            "createdAt": now,
            "updatedAt": now,
        }
        batch = SyncBatchV2.model_validate({
            "protocolVersion": 2,
            "deviceID": f"device-v2-{suffix}",
            "cursor": "seq:0",
            "records": [{
                "id": f"record-v2-{suffix}",
                "entityType": "customer",
                "entityID": customer_id,
                "updatedAt": now,
                "payload": payload,
                "entitySchemaVersion": 1,
                "baseServerRevision": None,
                "clientMutationID": mutation_id,
                "deletedAt": None,
            }],
        })

        first = await apply_push_v2(db, actor, batch)
        assert first.acceptedRecordIDs == [f"record-v2-{suffix}"]
        replay = await apply_push_v2(db, actor, batch)
        assert replay.acceptedRecordIDs == [f"record-v2-{suffix}"]

        pulled = await pull_since_v2(db, actor, "seq:0")
        matching = [record for record in pulled.records if record.entityID == customer_id]
        assert pulled.protocolVersion == 2
        assert len(matching) == 1
        assert matching[0].payload == payload
        assert matching[0].entitySchemaVersion == 1
        assert matching[0].serverRevision is not None

        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        cross_protocol = SyncBatch.model_validate({
            "deviceID": f"device-v1-cross-protocol-{suffix}",
            "cursor": "seq:0",
            "records": [{
                "id": f"record-v1-cross-protocol-{suffix}",
                "entityType": "customer",
                "entityID": customer_id,
                "updatedAt": now,
                "payload": base64.b64encode(canonical).decode("ascii"),
                "baseServerRevision": None,
                "clientMutationID": mutation_id,
                "deletedAt": None,
            }],
        })
        collision = await apply_push(db, actor, cross_protocol)
        assert collision.acceptedRecordIDs == []
        assert collision.rejectedRecordIDs == [f"record-v1-cross-protocol-{suffix}"]
