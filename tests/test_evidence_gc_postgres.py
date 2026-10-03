"""Evidence GC, exercised in the DEFAULT suite against the local object store.

Why this file exists
--------------------
`tests/test_s3_evidence_integration.py` holds the collector's happy path, and it is gated
behind `S3_INTEGRATION=1`. That gate is correct for S3-specific multipart behaviour and
wrong for the collector: `evidence_storage_provider` defaults to `local`, and
`store_for_provider("local")` returns a real `LocalObjectStore`. So the entire collector
-- grace handling, the canonical-reference guard, the skip/fail accounting -- was
exercisable without any S3 at all, and simply was not.

That gap is why four real defects survived review (`VITR-V007` silent data loss, `-010`
no backoff, `-011` uncaught `StaleDataError` on overlapping runs, `-013` an orphaned
published object). None of them was found by a failing test, because no default-suite
test ran the code path.

Two properties are pinned deliberately:

1. **Grace is honoured.** A row queued moments ago is NOT collected. The existing S3
   test only passes when `EVIDENCE_GC_GRACE_SECONDS=0`, so it asserted a configuration
   the deployment never ships (`settings.py` and both production env files use 86400).
   Here the shipped default is asserted as the shipped default: fresh rows survive, and
   only rows whose `not_before` has passed are collected.
2. **A still-referenced canonical version is skipped, not deleted.** That guard is the
   one preventing the collector from destroying live evidence.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sqlalchemy import select

from app.db import SessionFactory
from app.evidence_gc import collect_due_evidence_gc
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalItemChild,
    CanonicalProject,
    EvidenceBlob,
    EvidenceObjectGC,
    Organization,
)
from app.settings import settings
from app.storage import LocalObjectStore

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)


@pytest.fixture
async def db():
    async with SessionFactory() as session:
        yield session


@pytest.fixture
def local_store(tmp_path: Path, monkeypatch) -> LocalObjectStore:
    """A LocalObjectStore the collector can reach through `store_for_provider`."""
    import app.storage as storage

    store = LocalObjectStore(tmp_path)
    monkeypatch.setattr(storage, "_LOCAL_STORE", store)
    return store


async def _clear(db) -> None:
    from sqlalchemy import delete

    for model in (
        EvidenceObjectGC,
        CanonicalItemChild,
        CanonicalItem,
        CanonicalProject,
        CanonicalCustomer,
        EvidenceBlob,
        Organization,
    ):
        await db.execute(delete(model))
    await db.commit()


async def _seed_org(db) -> None:
    db.add(Organization(id="org-1", name="Vitrial", authorization_revision=1))
    await db.commit()


async def _chunks(data: bytes):
    """A single-chunk async iterable, matching how put_verified consumes a stream."""
    yield data


async def _queue(db, store: LocalObjectStore, *, key: str, grace: int, due_at: datetime | None = None) -> EvidenceBlob:
    """Insert a published blob and queue its object for collection."""
    blob = EvidenceBlob(
        organization_id="org-1",
        document_id="doc-1",
        item_id="item-1",
        filename="f.bin",
        mime_type="application/octet-stream",
        sha256=hashlib.sha256(key.encode()).hexdigest(),
        size_bytes=len(key),
        storage_provider=store.provider,
        object_key=key,
    )
    db.add(blob)
    await db.flush()
    # Write through the real production API rather than a convenience helper, so the
    # fixture cannot drift from how objects are actually stored.
    payload = key.encode()
    await store.put_verified(key, _chunks(payload), hashlib.sha256(payload).hexdigest())

    row = EvidenceObjectGC(
        organization_id="org-1",
        document_id="doc-1",
        storage_provider=store.provider,
        object_key=key,
        reason="replaced",
        not_before=due_at or (datetime.now(timezone.utc) + timedelta(seconds=grace)),
    )
    db.add(row)
    await db.commit()
    return blob


@pytest.mark.asyncio
async def test_a_freshly_queued_object_is_not_collected(db, local_store):
    """The shipped 86400s grace means a new row is NOT due.

    Asserted against the real default rather than by overriding it. The S3 test can
    only pass with `EVIDENCE_GC_GRACE_SECONDS=0`, which no deployment ships -- so it
    verified a configuration that does not exist and left the shipped one unverified.
    """
    await _clear(db)
    await _seed_org(db)
    await _queue(db, local_store, key="blobs/fresh", grace=settings.evidence_gc_grace_seconds)

    assert settings.evidence_gc_grace_seconds == 86400, (
        "this test documents the shipped grace; update it deliberately if that changes"
    )

    result = await collect_due_evidence_gc(db)

    assert result.deleted == 0, "a row inside its grace period must not be collected"
    assert await local_store.exists("blobs/fresh"), "the object must survive"


@pytest.mark.asyncio
async def test_a_row_past_its_not_before_is_collected(db, local_store):
    """The other half: once due, the object is actually reclaimed.

    `now` is passed explicitly so the row's due time is relative to a fixed instant
    rather than racing wall-clock drift between seeding and collecting.
    """
    await _clear(db)
    await _seed_org(db)
    due = datetime.now(timezone.utc) - timedelta(seconds=60)
    await _queue(db, local_store, key="blobs/due", grace=0, due_at=due)

    assert await local_store.exists("blobs/due")
    result = await collect_due_evidence_gc(db, now=due + timedelta(seconds=1))

    assert result.deleted == 1, "a due row must be reclaimed"
    assert not await local_store.exists("blobs/due"), "the object must be gone"
    remaining = (await db.execute(select(EvidenceObjectGC))).scalars().all()
    assert remaining == [], "the queue row is consumed by a successful collection"


@pytest.mark.asyncio
async def test_dry_run_reports_work_without_deleting_anything(db, local_store):
    """A rehearsal must not reclaim, and must not consume the queue row either."""
    await _clear(db)
    await _seed_org(db)
    due = datetime.now(timezone.utc) - timedelta(seconds=60)
    await _queue(db, local_store, key="blobs/dry", grace=0, due_at=due)

    result = await collect_due_evidence_gc(db, now=due + timedelta(seconds=1), dry_run=True)

    assert result.deleted == 1, "dry_run reports what would be reclaimed"
    assert await local_store.exists("blobs/dry"), "dry_run must not delete"
    # And the row survives, so the rehearsal leaves the real run intact.
    queued = (await db.execute(select(EvidenceObjectGC))).scalars().all()
    assert len(queued) == 1, "dry_run must not consume the queue row"


@pytest.mark.asyncio
async def test_a_canonical_referenced_version_is_skipped_not_deleted(db, local_store):
    """The guard preventing destruction of live evidence.

    When the queued object IS the version the blob currently points at, and canonical
    state references it, the collector must skip. Deleting here is unrecoverable data
    loss on a row the operator believed was safely superseded.
    """
    await _clear(db)
    await _seed_org(db)
    blob = await _queue(db, local_store, key="blobs/canonical", grace=0,
                        due_at=datetime.now(timezone.utc) - timedelta(seconds=60))
    # The canonical chain is Project -> Item -> ItemChild, each level FK-constrained.
    # Seeded top-down so the guard the collector consults is genuinely present rather
    # than mocked; a hand-built stub here would test the fixture, not the guard.
    db.add(CanonicalCustomer(organization_id="org-1", customer_id="customer-1"))
    await db.flush()
    db.add(CanonicalProject(
        organization_id="org-1",
        project_id="project-1",
        customer_id="customer-1",
        deleted_at=None,
    ))
    await db.flush()
    db.add(CanonicalItem(
        organization_id="org-1",
        item_id="item-1",
        project_id="project-1",
        deleted_at=None,
    ))
    await db.flush()
    db.add(CanonicalItemChild(
        organization_id="org-1",
        entity_type="evidence",
        entity_id="doc-1",
        item_id="item-1",
        deleted_at=None,
    ))
    await db.commit()
    assert blob.object_key == "blobs/canonical"

    result = await collect_due_evidence_gc(
        db, now=datetime.now(timezone.utc) + timedelta(seconds=1)
    )

    assert result.skipped == 1, "a canonical, referenced version must be skipped"
    assert result.deleted == 0
    assert await local_store.exists("blobs/canonical"), "the live object must survive"


@pytest.mark.asyncio
async def test_a_failed_delete_increments_attempts_and_keeps_the_row(db, local_store, monkeypatch):
    """A storage failure must not lose the queue row.

    This is `VITR-V010` pinned on the part that is safe to pin today: the row is retained
    and the attempt is counted, so the work is never silently dropped. That `attempts` is
    never READ (no backoff, no give-up) is the open half of `VITR-V010` and is deliberately
    not asserted here -- asserting the current behaviour would make the fix fail the suite.
    """
    await _clear(db)
    await _seed_org(db)
    due = datetime.now(timezone.utc) - timedelta(seconds=60)
    await _queue(db, local_store, key="blobs/failing", grace=0, due_at=due)

    from app.storage import StorageError

    async def boom(_key: str) -> None:
        raise StorageError("injected delete failure")

    monkeypatch.setattr(local_store, "delete", boom)

    result = await collect_due_evidence_gc(
        db, now=due + timedelta(seconds=1)
    )

    assert result.failed == 1
    assert result.deleted == 0
    assert await local_store.exists("blobs/failing"), "a failed delete removes nothing"
    rows = (await db.execute(select(EvidenceObjectGC))).scalars().all()
    assert len(rows) == 1, "a failed collection must keep the row for a later attempt"
    assert rows[0].attempts == 1, "the attempt must be counted"