from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CanonicalItemChild, EvidenceBlob, EvidenceObjectGC, SyncEntity
from app.settings import settings
from app.storage import StorageError, store_for_provider


@dataclass(frozen=True)
class GCResult:
    deleted: int = 0
    skipped: int = 0
    failed: int = 0


async def queue_blob_gc(
    db: AsyncSession,
    blob: EvidenceBlob,
    *,
    reason: str,
    grace_seconds: int | None = None,
) -> EvidenceObjectGC:
    grace = settings.evidence_gc_grace_seconds if grace_seconds is None else grace_seconds
    row = EvidenceObjectGC(
        organization_id=blob.organization_id,
        document_id=blob.document_id,
        storage_provider=blob.storage_provider,
        object_key=blob.object_key,
        reason=reason,
        not_before=datetime.now(timezone.utc) + timedelta(seconds=max(0, grace)),
    )
    db.add(row)
    return row


async def _is_referenced(db: AsyncSession, organization_id: str, document_id: str) -> bool:
    entities = (
        await db.scalars(
            select(SyncEntity).where(
                SyncEntity.organization_id == organization_id,
                SyncEntity.entity_type.in_(["measurement", "customer_requirement"]),
                SyncEntity.deleted_at.is_(None),
            )
        )
    ).all()
    for entity in entities:
        payload = entity.payload_json or {}
        key = "evidenceReferences" if entity.entity_type == "measurement" else "evidenceReferenceIDs"
        references = payload.get(key)
        if isinstance(references, list) and document_id in references:
            return True
    return False


async def collect_due_evidence_gc(
    db: AsyncSession,
    *,
    limit: int = 100,
    dry_run: bool = False,
    now: datetime | None = None,
) -> GCResult:
    cutoff = now or datetime.now(timezone.utc)
    rows = (
        await db.scalars(
            select(EvidenceObjectGC)
            .where(EvidenceObjectGC.not_before <= cutoff)
            .order_by(EvidenceObjectGC.id.asc())
            .limit(max(1, min(limit, 1000)))
            .with_for_update(skip_locked=True)
        )
    ).all()

    deleted = skipped = failed = 0
    for row in rows:
        current = await db.get(EvidenceBlob, (row.organization_id, row.document_id))
        current_points_to_candidate = (
            current is not None
            and current.storage_provider == row.storage_provider
            and current.object_key == row.object_key
        )

        if current_points_to_candidate:
            canonical = await db.get(
                CanonicalItemChild,
                (row.organization_id, "evidence", row.document_id),
            )
            if canonical is not None and canonical.deleted_at is None:
                # An active canonical document still points at these bytes. The queue entry is stale
                # and must never be allowed to turn a live document into a dangling pointer.
                skipped += 1
                if not dry_run:
                    await db.delete(row)
                    await db.commit()
                continue
            if await _is_referenced(db, row.organization_id, row.document_id):
                skipped += 1
                if not dry_run:
                    row.attempts += 1
                    row.last_error = "canonical evidence is still referenced"
                    await db.commit()
                continue

        if dry_run:
            deleted += 1
            continue

        try:
            await store_for_provider(row.storage_provider).delete(row.object_key)
            if current_points_to_candidate and current is not None:
                await db.delete(current)
            await db.delete(row)
            await db.commit()
            deleted += 1
        except StorageError as exc:
            await db.rollback()
            retry = await db.get(EvidenceObjectGC, row.id)
            if retry is not None:
                retry.attempts += 1
                retry.last_error = str(exc)[:1024]
                await db.commit()
            failed += 1

    return GCResult(deleted=deleted, skipped=skipped, failed=failed)
