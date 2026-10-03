import base64
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.assembly_contract import AssemblyBOMSnapshot, assembly_bom_sha256
from app.auth import Principal
from app.db import SessionFactory
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalProject,
    CanonicalProjectSector,
    Membership,
    Organization,
    SyncEntity,
    User,
)
from app.reference_data import (
    ReferencePublicationCreate,
    ReferencePublicationPayload,
    current_publications,
    get_publication,
    publish_reference,
)
from app.schemas import SyncBatch
from app.sync_service import apply_push


pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ROOT = Path(__file__).resolve().parents[1]
ASSEMBLY_FIXTURE = ROOT / "contracts" / "assembly" / "aluminum-window-3panel-v1.json"


def actor(organization_id: str) -> Principal:
    return Principal(
        user_id=f"user-{organization_id}",
        organization_id=organization_id,
        membership_id=f"membership-{organization_id}",
        session_id=f"session-{organization_id}",
        authorization_revision=1,
        capabilities=frozenset({
            "sync",
            "item.configuration.manage",
            "item.configuration.finalize",
            "quotation.create",
            "catalog.manage",
            "pricing.manage",
        }),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )


async def seed_graph(
    db,
    principal: Principal,
    *,
    customer_id: str,
    project_id: str,
    sector_id: str,
    item_id: str,
) -> None:
    db.add(
        Organization(
            id=principal.organization_id,
            name="Assembly Successor Acceptance",
            authorization_revision=1,
        )
    )
    db.add(
        User(
            id=principal.user_id,
            display_name="Assembly Successor Actor",
            email=f"{principal.user_id}@example.invalid",
        )
    )
    await db.flush()
    db.add(
        Membership(
            id=principal.membership_id,
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            active=True,
            all_customers=True,
            all_projects=True,
            customer_ids=[],
            project_ids=[],
            roles=[{"id": "assembly-acceptance", "displayName": "Assembly Acceptance"}],
            capabilities=sorted(principal.capabilities),
        )
    )
    db.add(
        CanonicalCustomer(
            organization_id=principal.organization_id,
            customer_id=customer_id,
        )
    )
    await db.flush()
    db.add(
        CanonicalProject(
            organization_id=principal.organization_id,
            project_id=project_id,
            customer_id=customer_id,
        )
    )
    await db.flush()
    db.add(
        CanonicalProjectSector(
            organization_id=principal.organization_id,
            project_sector_id=sector_id,
            project_id=project_id,
            sector_id="sector-aluminum-glass-steel",
        )
    )
    await db.flush()
    db.add(
        CanonicalItem(
            organization_id=principal.organization_id,
            item_id=item_id,
            project_id=project_id,
            project_sector_id=sector_id,
        )
    )
    await db.commit()


def encoded(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")


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
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "payload": encoded(payload),
        "baseServerRevision": base_revision,
        "clientMutationID": mutation_id,
        "deletedAt": None,
    }


def pinned_reference_data(publications) -> dict:
    by_kind = {publication.kind: publication for publication in publications}
    return {
        "catalogVersionID": by_kind["catalog"].versionID,
        "catalogContentSHA256": by_kind["catalog"].contentSHA256,
        "compatibilityRulesVersionID": by_kind["compatibility_rules"].versionID,
        "compatibilityRulesContentSHA256": by_kind["compatibility_rules"].contentSHA256,
        "priceBookVersionID": by_kind["price_book"].versionID,
        "priceBookContentSHA256": by_kind["price_book"].contentSHA256,
    }


def frozen_fixture(
    *,
    item_id: str,
    configuration_id: str,
    reference_data: dict,
) -> dict:
    fixture = json.loads(ASSEMBLY_FIXTURE.read_text(encoding="utf-8"))
    configuration = copy.deepcopy(fixture["configuration"])
    quotation_line = copy.deepcopy(fixture["quotationLine"])

    configuration["id"] = configuration_id
    configuration["itemID"] = item_id
    assembly = configuration["assembly"]
    assembly["referenceData"] = copy.deepcopy(reference_data)
    assembly["bom"]["configurationID"] = configuration_id
    assembly["renderDescriptor"]["configurationID"] = configuration_id

    configuration_version_id = f"{configuration_id}#v1"
    quotation_line["itemID"] = item_id
    quotation_line["configurationVersionID"] = configuration_version_id
    design = quotation_line["designSnapshot"]
    design["configurationVersionID"] = configuration_version_id
    design["referenceData"] = copy.deepcopy(reference_data)
    design["renderDescriptor"] = copy.deepcopy(assembly["renderDescriptor"])
    design["bomSHA256"] = assembly_bom_sha256(
        AssemblyBOMSnapshot.model_validate(assembly["bom"])
    )

    return {
        "configuration": configuration,
        "configurationVersion": {
            "id": configuration_version_id,
            "configuration": copy.deepcopy(configuration),
        },
        "quotationLine": quotation_line,
    }


def successors_for(baseline_by_kind, effective_from: datetime, suffix: str):
    return [
        ReferencePublicationCreate(
            publicationID=f"assembly-catalog-successor-{suffix}",
            kind="catalog",
            versionID=f"assembly-catalog-successor-{suffix}",
            effectiveFrom=effective_from,
            supersedesPublicationID=baseline_by_kind["catalog"].publicationID,
            payload=ReferencePublicationPayload(
                source="assembly-successor-acceptance",
                authoritative=True,
                metadata={"generation": "successor", "kind": "catalog"},
            ),
        ),
        ReferencePublicationCreate(
            publicationID=f"assembly-rules-successor-{suffix}",
            kind="compatibility_rules",
            versionID=f"assembly-rules-successor-{suffix}",
            effectiveFrom=effective_from,
            supersedesPublicationID=baseline_by_kind["compatibility_rules"].publicationID,
            payload=ReferencePublicationPayload(
                source="assembly-successor-acceptance",
                authoritative=True,
                metadata={"generation": "successor", "kind": "compatibility_rules"},
            ),
        ),
        ReferencePublicationCreate(
            publicationID=f"assembly-price-successor-{suffix}",
            kind="price_book",
            versionID=f"assembly-price-successor-{suffix}",
            effectiveFrom=effective_from,
            supersedesPublicationID=baseline_by_kind["price_book"].publicationID,
            payload=ReferencePublicationPayload(
                source="assembly-successor-acceptance",
                authoritative=True,
                metadata={"generation": "successor", "kind": "price_book"},
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_frozen_quote_remains_accepted_after_reference_successors_become_current():
    suffix = uuid4().hex
    organization_id = f"org-assembly-successor-{suffix}"
    customer_id = f"customer-{suffix}"
    project_id = f"project-{suffix}"
    sector_id = f"project-sector-{suffix}"
    item_id = f"item-{suffix}"
    configuration_id = f"configuration-{suffix}"
    quotation_id = f"quotation-{suffix}"
    principal = actor(organization_id)

    async with SessionFactory() as db:
        await seed_graph(
            db,
            principal,
            customer_id=customer_id,
            project_id=project_id,
            sector_id=sector_id,
            item_id=item_id,
        )

        historical_at = datetime.now(timezone.utc)
        baseline = await current_publications(db, principal, at=historical_at)
        baseline_by_kind = {
            publication.kind: publication
            for publication in baseline.publications
        }
        assert set(baseline_by_kind) == {
            "catalog",
            "compatibility_rules",
            "price_book",
        }

        pins = pinned_reference_data(baseline.publications)
        frozen = frozen_fixture(
            item_id=item_id,
            configuration_id=configuration_id,
            reference_data=pins,
        )
        quotation = {
            "id": quotation_id,
            "projectID": project_id,
            "customerID": customer_id,
            "status": "draft",
            "lines": [frozen["quotationLine"]],
        }

        initial = SyncBatch.model_validate({
            "deviceID": f"device-{suffix}",
            "records": [
                record(
                    f"record-config-{suffix}",
                    "configuration",
                    configuration_id,
                    frozen["configuration"],
                    f"mutation-config-{suffix}",
                ),
                record(
                    f"record-config-version-{suffix}",
                    "configuration_version",
                    frozen["configurationVersion"]["id"],
                    frozen["configurationVersion"],
                    f"mutation-config-version-{suffix}",
                ),
                record(
                    f"record-quote-{suffix}",
                    "quotation",
                    quotation_id,
                    quotation,
                    f"mutation-quote-{suffix}",
                ),
            ],
        })
        initial_result = await apply_push(db, principal, initial)
        assert initial_result.rejectedRecordIDs == []
        assert initial_result.acceptedRecordIDs == [
            record.id for record in initial.records
        ]

        canonical_version_before = await db.get(
            SyncEntity,
            (
                organization_id,
                "configuration_version",
                frozen["configurationVersion"]["id"],
            ),
        )
        canonical_quote_before = await db.get(
            SyncEntity,
            (organization_id, "quotation", quotation_id),
        )
        assert canonical_version_before is not None
        assert canonical_quote_before is not None
        frozen_version_payload = copy.deepcopy(
            canonical_version_before.payload_json
        )
        frozen_design_snapshot = copy.deepcopy(
            canonical_quote_before.payload_json["lines"][0]["designSnapshot"]
        )

        successor_effective = historical_at + timedelta(seconds=1)
        successors = successors_for(
            baseline_by_kind,
            successor_effective,
            suffix,
        )
        for successor in successors:
            await publish_reference(db, principal, successor)

        current_after = await current_publications(
            db,
            principal,
            at=successor_effective + timedelta(seconds=1),
        )
        assert {
            publication.kind: publication.versionID
            for publication in current_after.publications
        } == {
            successor.kind: successor.versionID
            for successor in successors
        }

        # Re-authorize/update the old draft after successors are current. The quote still
        # points at the historical ConfigurationVersion and must remain valid against those
        # exact immutable publication pins rather than today's current publications.
        quotation_after = copy.deepcopy(canonical_quote_before.payload_json)
        quotation_after["notes"] = "Historical assembly pins remain valid after successors"
        revalidated = await apply_push(
            db,
            principal,
            SyncBatch.model_validate({
                "deviceID": f"device-{suffix}",
                "records": [
                    record(
                        f"record-quote-revalidate-{suffix}",
                        "quotation",
                        quotation_id,
                        quotation_after,
                        f"mutation-quote-revalidate-{suffix}",
                        base_revision=canonical_quote_before.server_revision,
                    )
                ],
            }),
        )
        assert revalidated.rejectedRecordIDs == []
        assert revalidated.acceptedRecordIDs == [
            f"record-quote-revalidate-{suffix}"
        ]

        canonical_version_after = await db.get(
            SyncEntity,
            (
                organization_id,
                "configuration_version",
                frozen["configurationVersion"]["id"],
            ),
        )
        canonical_quote_after = await db.get(
            SyncEntity,
            (organization_id, "quotation", quotation_id),
        )
        assert canonical_version_after is not None
        assert canonical_quote_after is not None
        assert canonical_version_after.payload_json == frozen_version_payload
        assert (
            canonical_quote_after.payload_json["lines"][0]["designSnapshot"]
            == frozen_design_snapshot
        )

        # The old immutable publications are still directly retrievable byte-for-byte at the
        # response-model level even after successor authority advances.
        for kind, original in baseline_by_kind.items():
            historical = await get_publication(
                db,
                principal,
                original.publicationID,
            )
            assert historical.model_dump(mode="json") == original.model_dump(
                mode="json"
            ), kind
