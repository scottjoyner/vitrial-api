from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.metrics import evidence_gc_total
from app.models import CanonicalItemChild, EvidenceBlob, EvidenceObjectGC, Organization, SyncEntity
from app.settings import settings
from app.storage import StorageError, store_for_provider


@dataclass(frozen=True)
class GCResult:
    deleted: int = 0
    skipped: int = 0
    failed: int = 0


# Backoff for a row the collector could not reclaim.
#
# `not_before` is the collector's only selection predicate (`not_before <= cutoff`), so
# it is the only thing that can keep a failing row from being picked again immediately.
# Both retain paths -- the still-referenced skip and the StorageError handler -- used to
# commit the row with the `not_before` it was queued with, which is already in the past
# by the time anything failed. Every such row was therefore reselected on the very next
# run, in id order, and with the shipped limit of 100 (`scripts/gc_evidence.py`) ~100
# permanently blocked rows permanently occupied the head of the queue.
#
# The delay grows with `attempts`, so `attempts` is finally read by something: it is the
# backoff's input rather than a counter nothing consumes. The cap bounds the damage from
# a row that can never succeed (an object store that is permanently broken) -- without
# it, one such row is retried every run forever.
GC_BACKOFF_BASE_SECONDS = 300
GC_BACKOFF_MAX_SECONDS = 3600


def _reschedule(row: EvidenceObjectGC, *, error: str, now: datetime) -> None:
    """Record a failed attempt and push the row out of the selection window.

    `attempts` is incremented first so the first failure gets the base delay, not zero.
    """
    row.attempts += 1
    row.last_error = error
    # The exponent is bounded as well as the result: `attempts` is a smallint column that
    # increments forever, and `2 ** attempts` on a large value is a needless bignum.
    delay = min(
        GC_BACKOFF_BASE_SECONDS * 2 ** min(row.attempts - 1, 16),
        GC_BACKOFF_MAX_SECONDS,
    )
    row.not_before = now + timedelta(seconds=delay)


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
    """Whether any live measurement/requirement still lists this evidence document.

    Same containment predicate as `ownership._ensure_evidence_delete_safe`: the filter
    runs in PostgreSQL so the collector does not materialize every measurement payload
    in the process. See the ownership helper for why this is an engine-side scan and
    not an indexed lookup.
    """
    from app.ownership import _evidence_reference_filter

    referenced = await db.scalar(
        select(SyncEntity.entity_id)
        .where(
            SyncEntity.organization_id == organization_id,
            SyncEntity.entity_type.in_(["measurement", "customer_requirement"]),
            SyncEntity.deleted_at.is_(None),
            _evidence_reference_filter(document_id),
        )
        .limit(1)
    )
    return referenced is not None


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
        # Read the primary key BEFORE any rollback below. A rollback -- including the
        # savepoint rollback the `async with` performs -- expires every instance in that
        # scope, so touching `row.id` afterwards makes SQLAlchemy refresh the row, which
        # means lazy IO from a synchronous context and raises MissingGreenlet under
        # asyncpg. That turned the StorageError handler into the thing it was written to
        # prevent: a failing object store aborted the whole collection run with a
        # traceback instead of recording the failure. See VITR-V014.
        row_id = row.id

        # One transaction for the whole run, one SAVEPOINT per row.
        #
        # The run used to `commit()` inside this loop. A commit ends the transaction and
        # releases the `FOR UPDATE` locks taken by the single `SKIP LOCKED` selection
        # above -- including the locks on rows this run has not reached yet. An
        # overlapping run could then select, delete and commit one of those rows, and
        # this run's `row.attempts += 1` would match zero rows: `EvidenceObjectGC` has no
        # `version_id_col`, so the UPDATE path's rowcount check raises StaleDataError
        # unconditionally, and with no handler it escaped `collect_due_evidence_gc` and
        # aborted the entire collection. The two shipped comments that claimed
        # "overlapping runs are safe" were describing locks this ordering did not hold.
        #
        # The SAVEPOINT is what keeps the StorageError path recoverable without giving
        # that up: a failing object store rolls back only its own row, and the locks,
        # and the work of every row already processed, all survive.
        try:
            async with db.begin_nested():
                # Share the same organization lock used by sync publication and evidence pointer swaps.
                #
                # What this lock actually buys, stated precisely because the earlier comment here
                # claimed more than it delivered: it serializes this DELETE against an upload's
                # pointer SWITCH. It does NOT protect an upload's WRITE, because `put_blob`
                # streams the object to storage BEFORE it takes this lock -- deliberately, so a
                # 100 MiB upload does not hold a transaction and a connection for its duration.
                #
                # So there is a real window: an upload writes key K, and a collector holding this
                # lock can delete K before the upload acquires it. Reachable because keys are
                # content-addressed -- re-uploading byte-identical content reuses the key a
                # superseded version used, which may still carry a pending GC row.
                #
                # `put_blob` closes it from the other side: under this same lock it cancels any
                # pending GC row for the key it is about to publish, then verifies the object
                # still exists before committing the pointer. The two locks are what make each
                # side safe, which is why neither half is sufficient alone. See VITR-V007.
                #
                # The lock is now held for the whole run rather than per row. Anything that
                # takes organization locks in a different order in one transaction would
                # deadlock against this; nothing in the service does -- sync push and
                # `put_blob` each take the one organization they are working on.
                await db.scalar(
                    select(Organization)
                    .where(Organization.id == row.organization_id)
                    .with_for_update()
                )
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
                        skipped += 1
                        if not dry_run:
                            await db.delete(row)
                        continue
                    if await _is_referenced(db, row.organization_id, row.document_id):
                        skipped += 1
                        if not dry_run:
                            _reschedule(
                                row,
                                error="canonical evidence is still referenced",
                                now=cutoff,
                            )
                        continue

                if dry_run:
                    deleted += 1
                    continue

                await store_for_provider(row.storage_provider).delete(row.object_key)
                if current_points_to_candidate and current is not None:
                    await db.delete(current)
                await db.delete(row)
                deleted += 1
        except StorageError as exc:
            retry = await db.get(EvidenceObjectGC, row_id)
            if retry is not None:
                _reschedule(retry, error=str(exc)[:1024], now=cutoff)
            failed += 1

    # One commit for the run, so the `SKIP LOCKED` selection above stays locked for as
    # long as this run is working through it. Nothing else in the file commits.
    if not dry_run:
        await db.commit()

    # Counted once per run from the returned totals rather than at each of the three
    # increment sites. Incrementing inline would be the obvious approach and it is
    # wrong in a way that only shows up under failure: a new branch added later, or an
    # exception path that skips a site, leaves the counter describing a run that did
    # not happen. The totals here are the same numbers the caller already receives, so
    # the metric cannot disagree with the return value.
    #
    # A `dry_run` reports what WOULD be deleted and increments the same `deleted`
    # counter, so it is labelled separately rather than merged. Folding it in would
    # inflate the reclamation rate -- which is the number an operator alerts on --
    # purely by someone rehearsing a collection.
    if deleted:
        evidence_gc_total.inc(amount=deleted, result="dry_run" if dry_run else "deleted")
    if skipped:
        evidence_gc_total.inc(amount=skipped, result="skipped")
    if failed:
        evidence_gc_total.inc(amount=failed, result="failed")

    return GCResult(deleted=deleted, skipped=skipped, failed=failed)
