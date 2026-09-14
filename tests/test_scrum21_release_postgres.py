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
    CompatibilityRule,
    PriceBookLine,
    ReferenceEntry,
    ReferencePublicationCreate,
    ReferencePublicationPayload,
    current_publications,
    get_publication,
    publish_reference,
)
from app.schemas import SyncBatch
from app.sync_service import apply_push, decode_payload, pull_since
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


async def seed_actor(db, actor: Principal, *, organization_name: str) -> None:
    db.add(Organization(id=actor.organization_id, name=organization_name, authorization_revision=1))
    db.add(User(id=actor.user_id, display_name="SCRUM-21 Release Actor", email=f"{actor.user_id}@example.invalid"))
    await db.flush()
    db.add(Membership(
        id=actor.membership_id,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        active=True,
        all_customers=True,
        all_projects=True,
        customer_ids=[],
        project_ids=[],
        roles=[{"id": "scrum21-release", "displayName": "SCRUM-21 Release"}],
        capabilities=sorted(actor.capabilities),
    ))
    await db.commit()


def isolated_org(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


@pytest.mark.asyncio
async def test_catalog_rules_and_price_history_remain_exactly_reproducible_after_successors():
    organization_id = isolated_org("org-scrum21-history")
    actor = principal(
        capabilities={"catalog.manage", "pricing.manage", "sync"},
        organization_id=organization_id,
    )

    async with SessionFactory() as db:
        await seed_actor(db, actor, organization_name="SCRUM-21 Historical Authority")

        historical_at = datetime.now(timezone.utc)
        baseline = await current_publications(db, actor, at=historical_at)
        baseline_by_kind = {publication.kind: publication for publication in baseline.publications}
        assert set(baseline_by_kind) == {"catalog", "compatibility_rules", "price_book"}

        successor_effective = historical_at + timedelta(seconds=1)
        suffix = uuid4().hex
        successors = [
            ReferencePublicationCreate(
                publicationID=f"catalog-release-{suffix}",
                kind="catalog",
                versionID=f"catalog-release-{suffix}",
                effectiveFrom=successor_effective,
                supersedesPublicationID=baseline_by_kind["catalog"].publicationID,
                payload=ReferencePublicationPayload(
                    source="release-acceptance",
                    authoritative=True,
                    entries=[ReferenceEntry(
                        id="release-profile-001",
                        kind="aluminum_profile",
                        code="RELEASE_PROFILE",
                        name="Release Profile",
                        vendorSKU="REL-001",
                        unitCostMinor=10000,
                        currency="USD",
                    )],
                ),
            ),
            ReferencePublicationCreate(
                publicationID=f"rules-release-{suffix}",
                kind="compatibility_rules",
                versionID=f"rules-release-{suffix}",
                effectiveFrom=successor_effective,
                supersedesPublicationID=baseline_by_kind["compatibility_rules"].publicationID,
                payload=ReferencePublicationPayload(
                    source="release-acceptance",
                    authoritative=True,
                    compatibilityRules=[CompatibilityRule(
                        id="release-window-rule",
                        itemTypeID="item-type-window",
                        availableComponents=["vanoDimensions", "structure", "aluminumProfile"],
                        requiredComponents=["vanoDimensions", "structure"],
                        allowedSectionTypes=["fixed"],
                        compatibleEntryIDs=["release-profile-001"],
                    )],
                ),
            ),
            ReferencePublicationCreate(
                publicationID=f"price-release-{suffix}",
                kind="price_book",
                versionID=f"price-release-{suffix}",
                effectiveFrom=successor_effective,
                supersedesPublicationID=baseline_by_kind["price_book"].publicationID,
                payload=ReferencePublicationPayload(
                    source="release-acceptance",
                    authoritative=True,
                    priceBookLines=[PriceBookLine(
                        id="release-window-base-price",
                        itemTypeID="item-type-window",
                        description="Window base price",
                        unitPriceMinor=250000,
                        currency="USD",
                    )],
                ),
            ),
        ]

        for successor in successors:
            await publish_reference(db, actor, successor)

        current = await current_publications(
            db,
            actor,
            at=successor_effective + timedelta(seconds=1),
        )
        current_by_kind = {publication.kind: publication for publication in current.publications}
        assert {kind: publication.versionID for kind, publication in current_by_kind.items()} == {
            successor.kind: successor.versionID for successor in successors
        }

        # Time-travel reads still resolve the exact publication set that was current before
        # the successor set became effective.
        historical_current = await current_publications(db, actor, at=historical_at)
        assert {
            publication.kind: publication.publicationID
            for publication in historical_current.publications
        } == {
            kind: publication.publicationID
            for kind, publication in baseline_by_kind.items()
        }

        # Direct historical fetch retains the exact immutable response, including digest and payload,
        # for all three authority kinds after current authority advances.
        for kind, original in baseline_by_kind.items():
            historical = await get_publication(db, actor, original.publicationID)
            assert historical.model_dump(mode="json") == original.model_dump(mode="json"), kind


@pytest.mark.asyncio
async def test_v1_create_v2_read_update_and_v1_read_share_one_canonical_state():
    organization_id = isolated_org("org-scrum21-cross-protocol")
    actor = principal(
        capabilities={"sync", "customer.create", "customer.edit"},
        organization_id=organization_id,
    )

    async with SessionFactory() as db:
        await seed_actor(db, actor, organization_name="SCRUM-21 Cross Protocol")

        suffix = uuid4().hex
        customer_id = f"customer-cross-{suffix}"
        created_at = datetime.now(timezone.utc)
        created_wire = created_at.isoformat()
        initial_payload = {
            "id": customer_id,
            "name": "Created through V1",
            "status": "active",
            "address": "",
            "email": "",
            "phone": "",
            "notes": "",
            "createdAt": created_wire,
            "updatedAt": created_wire,
        }
        initial_canonical = json.dumps(
            initial_payload,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        v1_create = SyncBatch.model_validate({
            "deviceID": f"device-v1-{suffix}",
            "cursor": "seq:0",
            "records": [{
                "id": f"record-v1-create-{suffix}",
                "entityType": "customer",
                "entityID": customer_id,
                "updatedAt": created_wire,
                "payload": base64.b64encode(initial_canonical).decode("ascii"),
                "baseServerRevision": None,
                "clientMutationID": f"mutation-v1-create-{suffix}",
                "deletedAt": None,
            }],
        })
        first = await apply_push(db, actor, v1_create)
        assert first.acceptedRecordIDs == [f"record-v1-create-{suffix}"]

        read_through_v2 = await pull_since_v2(db, actor, "seq:0")
        initial_record = next(record for record in read_through_v2.records if record.entityID == customer_id)
        assert initial_record.payload == initial_payload
        assert initial_record.entitySchemaVersion == 1
        assert initial_record.serverRevision is not None

        updated_at = created_at + timedelta(seconds=5)
        updated_payload = dict(initial_payload)
        updated_payload["name"] = "Updated through V2"
        updated_payload["notes"] = "same canonical entity, new protocol"
        updated_payload["updatedAt"] = updated_at.isoformat()
        v2_update = SyncBatchV2.model_validate({
            "protocolVersion": 2,
            "deviceID": f"device-v2-{suffix}",
            "cursor": first.nextCursor,
            "records": [{
                "id": f"record-v2-update-{suffix}",
                "entityType": "customer",
                "entityID": customer_id,
                "updatedAt": updated_at.isoformat(),
                "payload": updated_payload,
                "entitySchemaVersion": 1,
                "baseServerRevision": initial_record.serverRevision,
                "clientMutationID": f"mutation-v2-update-{suffix}",
                "deletedAt": None,
            }],
        })
        second = await apply_push_v2(db, actor, v2_update)
        assert second.acceptedRecordIDs == [f"record-v2-update-{suffix}"]

        # Pulling from the cursor returned by the V1 create observes only the V2 update through
        # the unchanged V1 base64/Data contract, and decodes to the exact same canonical object.
        read_back_through_v1 = await pull_since(db, actor, first.nextCursor)
        matching_v1 = [record for record in read_back_through_v1.records if record.entityID == customer_id]
        assert len(matching_v1) == 1
        assert decode_payload(matching_v1[0]) == updated_payload

        read_back_through_v2 = await pull_since_v2(db, actor, first.nextCursor)
        matching_v2 = [record for record in read_back_through_v2.records if record.entityID == customer_id]
        assert len(matching_v2) == 1
        assert matching_v2[0].payload == updated_payload
        assert matching_v2[0].serverRevision == matching_v1[0].serverRevision
