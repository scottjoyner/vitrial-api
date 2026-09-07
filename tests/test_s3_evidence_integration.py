import hashlib
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.auth import Principal
from app.db import SessionFactory
from app.evidence import put_blob
from app.evidence_gc import collect_due_evidence_gc, queue_blob_gc
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalItemChild,
    CanonicalProject,
    CanonicalProjectSector,
    EvidenceBlob,
    EvidenceObjectGC,
    Organization,
)
from app.storage import store_for_provider

pytestmark = pytest.mark.skipif(
    os.environ.get("S3_INTEGRATION") != "1",
    reason="requires live S3-compatible object storage",
)


async def chunked(data: bytes, size: int = 1024 * 1024):
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


@pytest.mark.asyncio
async def test_s3_stream_replace_and_canonical_gc():
    suffix = uuid4().hex
    org_id = f"org-s3-{suffix}"
    customer_id = f"customer-s3-{suffix}"
    project_id = f"project-s3-{suffix}"
    sector_id = f"project-sector-s3-{suffix}"
    item_id = f"item-s3-{suffix}"
    document_id = f"document-s3-{suffix}"
    principal = Principal(
        user_id=f"user-{suffix}",
        organization_id=org_id,
        membership_id=f"membership-{suffix}",
        session_id=f"session-{suffix}",
        authorization_revision=1,
        capabilities=frozenset({"sync", "item.evidence.manage"}),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )

    async with SessionFactory() as db:
        db.add(Organization(id=org_id, name="S3 integration"))
        await db.flush()
        db.add(CanonicalCustomer(organization_id=org_id, customer_id=customer_id))
        await db.flush()
        db.add(CanonicalProject(
            organization_id=org_id,
            project_id=project_id,
            customer_id=customer_id,
        ))
        await db.flush()
        db.add(CanonicalProjectSector(
            organization_id=org_id,
            project_sector_id=sector_id,
            project_id=project_id,
            sector_id="sector-aluminum-glass-steel",
        ))
        await db.flush()
        db.add(CanonicalItem(
            organization_id=org_id,
            item_id=item_id,
            project_id=project_id,
            project_sector_id=sector_id,
        ))
        await db.commit()

        original = (b"A" * (5 * 1024 * 1024)) + (b"B" * (1024 * 1024 + 17))
        original_digest = hashlib.sha256(original).hexdigest()
        blob = await put_blob(
            db,
            principal,
            document_id,
            item_id,
            "original.bin",
            "application/octet-stream",
            original_digest,
            chunked(original),
        )
        assert blob.storage_provider == "s3"
        original_key = blob.object_key
        store = store_for_provider("s3")
        assert await store.exists(original_key)
        assert b"".join([part async for part in store.stream(original_key)]) == original

        db.add(CanonicalItemChild(
            organization_id=org_id,
            entity_type="evidence",
            entity_id=document_id,
            item_id=item_id,
        ))
        await db.commit()

        replacement = (b"C" * (5 * 1024 * 1024)) + b"replacement-tail"
        replacement_digest = hashlib.sha256(replacement).hexdigest()
        blob = await put_blob(
            db,
            principal,
            document_id,
            item_id,
            "replacement.bin",
            "application/octet-stream",
            replacement_digest,
            chunked(replacement),
        )
        replacement_key = blob.object_key
        assert replacement_key != original_key
        assert await store.exists(original_key)
        assert await store.exists(replacement_key)
        queued = (await db.scalars(
            select(EvidenceObjectGC).where(
                EvidenceObjectGC.organization_id == org_id,
                EvidenceObjectGC.object_key == original_key,
            )
        )).all()
        assert len(queued) == 1

        result = await collect_due_evidence_gc(db, limit=20)
        assert result.deleted >= 1
        assert not await store.exists(original_key)
        assert await store.exists(replacement_key)
        current = await db.get(EvidenceBlob, (org_id, document_id))
        assert current is not None and current.object_key == replacement_key

        canonical = await db.get(CanonicalItemChild, (org_id, "evidence", document_id))
        canonical.deleted_at = datetime.now(timezone.utc)
        await queue_blob_gc(db, current, reason="metadata_tombstone", grace_seconds=0)
        await db.commit()

        result = await collect_due_evidence_gc(db, limit=20)
        assert result.deleted >= 1
        assert not await store.exists(replacement_key)
        assert await db.get(EvidenceBlob, (org_id, document_id)) is None
