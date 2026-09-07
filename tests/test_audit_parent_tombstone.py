import base64
import json
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.auth import Principal
from app.db import SessionFactory
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalItemChild,
    CanonicalProject,
    CanonicalProjectSector,
    Organization,
    SyncEntity,
    SyncMutation,
)
from app.schemas import SyncBatch
from app.sync_service import apply_push


pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)


@pytest.mark.asyncio
async def test_item_tombstone_is_rejected_while_immutable_audit_history_exists():
    organization_id = "org-audit-parent-guard"
    customer_id = "customer-audit-parent-guard"
    project_id = "project-audit-parent-guard"
    project_sector_id = "project-sector-audit-parent-guard"
    item_id = "item-audit-parent-guard"
    audit_id = "audit-audit-parent-guard"
    mutation_id = "mutation-delete-audit-parent-guard"

    principal = Principal(
        user_id="user-audit-parent-guard",
        organization_id=organization_id,
        membership_id="membership-audit-parent-guard",
        session_id="session-audit-parent-guard",
        authorization_revision=1,
        capabilities=frozenset({"sync", "item.edit"}),
        customer_ids=frozenset({customer_id}),
        project_ids=frozenset({project_id}),
        all_customers=False,
        all_projects=False,
    )

    async with SessionFactory() as db:
        # Keep this regression isolated from the broader integration fixture cleanup.
        await db.execute(delete(SyncMutation).where(SyncMutation.organization_id == organization_id))
        await db.execute(delete(SyncEntity).where(SyncEntity.organization_id == organization_id))
        await db.execute(delete(CanonicalItemChild).where(CanonicalItemChild.organization_id == organization_id))
        await db.execute(delete(CanonicalItem).where(CanonicalItem.organization_id == organization_id))
        await db.execute(delete(CanonicalProjectSector).where(CanonicalProjectSector.organization_id == organization_id))
        await db.execute(delete(CanonicalProject).where(CanonicalProject.organization_id == organization_id))
        await db.execute(delete(CanonicalCustomer).where(CanonicalCustomer.organization_id == organization_id))
        await db.execute(delete(Organization).where(Organization.id == organization_id))
        await db.commit()

        db.add(Organization(id=organization_id, name="Audit Guard", authorization_revision=1))
        await db.flush()
        db.add(CanonicalCustomer(organization_id=organization_id, customer_id=customer_id))
        await db.flush()
        db.add(CanonicalProject(
            organization_id=organization_id,
            project_id=project_id,
            customer_id=customer_id,
        ))
        await db.flush()
        db.add(CanonicalProjectSector(
            organization_id=organization_id,
            project_sector_id=project_sector_id,
            project_id=project_id,
            sector_id="sector-aluminum",
        ))
        await db.flush()
        db.add(CanonicalItem(
            organization_id=organization_id,
            item_id=item_id,
            project_id=project_id,
            project_sector_id=project_sector_id,
        ))
        db.add(SyncEntity(
            organization_id=organization_id,
            entity_type="item",
            entity_id=item_id,
            server_revision=5,
            schema_version=1,
            payload_json={
                "id": item_id,
                "projectID": project_id,
                "projectSectorID": project_sector_id,
            },
            updated_at=datetime.now(timezone.utc),
            deleted_at=None,
        ))
        await db.flush()
        db.add(CanonicalItemChild(
            organization_id=organization_id,
            entity_type="item_audit_event",
            entity_id=audit_id,
            item_id=item_id,
            deleted_at=None,
        ))
        await db.commit()

        payload = base64.b64encode(json.dumps({
            "id": item_id,
            "projectID": project_id,
            "projectSectorID": project_sector_id,
        }).encode()).decode()
        batch = SyncBatch.model_validate({
            "deviceID": "device-audit-parent-guard",
            "records": [{
                "id": "record-delete-audit-parent-guard",
                "entityType": "item",
                "entityID": item_id,
                "updatedAt": "2026-09-07T11:00:00Z",
                "payload": payload,
                "baseServerRevision": 5,
                "clientMutationID": mutation_id,
                "deletedAt": "2026-09-07T11:00:00Z",
            }],
        })

        result = await apply_push(db, principal, batch)
        assert result.acceptedRecordIDs == []
        assert result.rejectedRecordIDs == ["record-delete-audit-parent-guard"]

        canonical_item = await db.get(CanonicalItem, (organization_id, item_id))
        sync_item = await db.get(SyncEntity, (organization_id, "item", item_id))
        mutation = await db.scalar(select(SyncMutation).where(
            SyncMutation.organization_id == organization_id,
            SyncMutation.client_mutation_id == mutation_id,
        ))

        assert canonical_item is not None and canonical_item.deleted_at is None
        assert sync_item is not None and sync_item.deleted_at is None
        assert sync_item.server_revision == 5
        assert mutation is not None and mutation.result_status == "rejected"
        assert mutation.result_server_revision == 5

        await db.execute(delete(SyncMutation).where(SyncMutation.organization_id == organization_id))
        await db.execute(delete(SyncEntity).where(SyncEntity.organization_id == organization_id))
        await db.execute(delete(CanonicalItemChild).where(CanonicalItemChild.organization_id == organization_id))
        await db.execute(delete(CanonicalItem).where(CanonicalItem.organization_id == organization_id))
        await db.execute(delete(CanonicalProjectSector).where(CanonicalProjectSector.organization_id == organization_id))
        await db.execute(delete(CanonicalProject).where(CanonicalProject.organization_id == organization_id))
        await db.execute(delete(CanonicalCustomer).where(CanonicalCustomer.organization_id == organization_id))
        await db.execute(delete(Organization).where(Organization.id == organization_id))
        await db.commit()
