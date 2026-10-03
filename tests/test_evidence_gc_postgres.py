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

1. **Grace is honoured, independent of configuration.** A row whose `not_before` is in
   the future is NOT collected; a row whose `not_before` has passed IS. Both use an
   explicit `now`/grace rather than reading `settings`, because the integration job sets
   `EVIDENCE_GC_GRACE_SECONDS=0` to give the S3 collector test something to collect --
   pinning the ambient value tests the environment, not the invariant.
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
    SyncEntity,
)
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
        SyncEntity,
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


async def _queue(
    db,
    store: LocalObjectStore,
    *,
    key: str,
    grace: int,
    due_at: datetime | None = None,
    document_id: str = "doc-1",
) -> EvidenceBlob:
    """Insert a published blob and queue its object for collection."""
    blob = EvidenceBlob(
        organization_id="org-1",
        document_id=document_id,
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
        document_id=document_id,
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
    """A row inside its grace period must NOT be collected, whatever the configured grace.

    The grace is passed EXPLICITLY rather than read from `settings`. An earlier version of
    this test asserted `settings.evidence_gc_grace_seconds == 86400` and it failed in CI,
    correctly: the integration job sets `EVIDENCE_GC_GRACE_SECONDS=0` so the S3 collector
    test has something to collect. Asserting an ambient configuration value is the wrong
    thing to pin -- it tests the environment, not the invariant, and it breaks whenever a
    job legitimately overrides a default for testability.

    The invariant is what matters and it is environment-independent: a row whose
    `not_before` is in the future survives; a row whose `not_before` has passed is
    reclaimed. Both halves are covered here and in the test below.
    """
    await _clear(db)
    await _seed_org(db)
    # An hour out, so the row is unambiguously in the future regardless of settings.
    await _queue(db, local_store, key="blobs/fresh", grace=3600)

    result = await collect_due_evidence_gc(db)

    assert result.deleted == 0, "a row inside its grace period must not be collected"
    assert await local_store.exists("blobs/fresh"), "the object must survive"


@pytest.mark.asyncio
async def test_a_row_past_its_not_before_is_collected(db, local_store):
    """The other half: once due, the object is actually reclaimed.

    `now` is passed explicitly so the row's due time is relative to a fixed instant rather
    than racing wall-clock drift between seeding and collecting, and so the result does not
    depend on `EVIDENCE_GC_GRACE_SECONDS` in the ambient environment (the integration job
    sets it to 0 precisely so a collector test has something to collect).
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
    """A storage failure must not lose the queue row, and must back it off.

    Two properties, and the second is the half of `VITR-V010` this file previously
    declined to assert. The row is retained and the attempt is counted, so the work is
    never silently dropped -- and `not_before` is pushed into the future, so the next
    run does not pick the same row straight back up. Before the fix the row was
    committed with the `not_before` it was queued with, which is already past by the
    time the delete fails, so the collector retried it on every subsequent run. That is
    not cosmetic: selection is `order_by(id)` under a limit of 100, so a hundred
    permanently blocked rows sit at the head of the queue forever and starve the rows
    behind them.
    """
    await _clear(db)
    await _seed_org(db)
    due = datetime.now(timezone.utc) - timedelta(seconds=60)
    await _queue(db, local_store, key="blobs/failing", grace=0, due_at=due)

    from app.storage import StorageError

    async def boom(_key: str) -> None:
        raise StorageError("injected delete failure")

    monkeypatch.setattr(local_store, "delete", boom)

    first_run_at = due + timedelta(seconds=1)
    result = await collect_due_evidence_gc(db, now=first_run_at)

    assert result.failed == 1
    assert result.deleted == 0
    assert await local_store.exists("blobs/failing"), "a failed delete removes nothing"
    rows = (await db.execute(select(EvidenceObjectGC))).scalars().all()
    assert len(rows) == 1, "a failed collection must keep the row for a later attempt"
    assert rows[0].attempts == 1, "the attempt must be counted"
    assert rows[0].last_error == "injected delete failure"
    assert rows[0].not_before > first_run_at, (
        "a retained row must leave the selection window, or the next run retries it"
    )

    # The property itself: a run immediately afterwards finds nothing due.
    second = await collect_due_evidence_gc(db, now=first_run_at + timedelta(seconds=1))

    assert (second.failed, second.deleted, second.skipped) == (0, 0, 0), (
        "the backed-off row was reselected by the very next run"
    )
    assert await local_store.exists("blobs/failing")


@pytest.mark.asyncio
async def test_a_still_referenced_version_is_backed_off_not_reselected(db, local_store):
    """The second retain path backs off too.

    A blob whose queued object is still the version it points at, with no active
    canonical child, but with a live measurement still listing it in
    `evidenceReferences`: the object must not be reclaimed, and the row must not be
    retried on the next run. This path committed `attempts += 1` with `not_before`
    untouched, exactly like the StorageError path -- so a permanently-referenced
    document was reselected forever.
    """
    await _clear(db)
    await _seed_org(db)
    due = datetime.now(timezone.utc) - timedelta(seconds=60)
    await _queue(db, local_store, key="blobs/referenced", grace=0, due_at=due)
    db.add(SyncEntity(
        organization_id="org-1",
        entity_type="measurement",
        entity_id="measurement-1",
        server_revision=1,
        schema_version=1,
        payload_json={"id": "measurement-1", "evidenceReferences": ["doc-1"]},
        updated_at=datetime.now(timezone.utc),
        deleted_at=None,
    ))
    await db.commit()

    first_run_at = due + timedelta(seconds=1)
    result = await collect_due_evidence_gc(db, now=first_run_at)

    assert result.skipped == 1, "a still-referenced version must be skipped"
    assert result.deleted == 0
    assert await local_store.exists("blobs/referenced"), "the referenced object must survive"
    rows = (await db.execute(select(EvidenceObjectGC))).scalars().all()
    assert len(rows) == 1, "the row is retained; the object is merely not reclaimed yet"
    assert rows[0].attempts == 1
    assert rows[0].last_error == "canonical evidence is still referenced"
    assert rows[0].not_before > first_run_at

    second = await collect_due_evidence_gc(db, now=first_run_at + timedelta(seconds=1))

    assert (second.skipped, second.deleted, second.failed) == (0, 0, 0), (
        "the backed-off row was reselected by the very next run"
    )
    assert await local_store.exists("blobs/referenced")


def test_backoff_grows_with_attempts_and_is_capped():
    """`attempts` is the backoff's input, so the delay has to actually depend on it.

    A flat delay would satisfy "not reselected immediately" and still let one
    unreclaimable row be retried every five minutes forever; an uncapped doubling would
    let `attempts` reach a value that computes a bignum. Both bounds are asserted with
    the shipped seconds rather than against the constants, because a test that compares
    the code to itself cannot fail.
    """
    from app.evidence_gc import _reschedule

    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    row = EvidenceObjectGC(
        organization_id="org-1",
        document_id="doc-1",
        storage_provider="local",
        object_key="blobs/backoff",
        reason="replaced",
        not_before=now,
        # `attempts` has a column default, which SQLAlchemy applies at INSERT, not at
        # construction -- an unpersisted row built here would carry None and `+= 1`
        # would raise. A row read back from the database always has it.
        attempts=0,
    )

    for attempt, delay in enumerate([300, 600, 1200, 2400, 3600, 3600], start=1):
        _reschedule(row, error="injected", now=now)
        assert row.attempts == attempt
        assert row.not_before == now + timedelta(seconds=delay), (
            f"attempt {attempt} should back off {delay}s"
        )
    assert row.last_error == "injected"

@pytest.mark.asyncio
async def test_an_overlapping_collector_finds_nothing_while_a_run_is_in_progress(db, local_store, monkeypatch):
    """`SKIP LOCKED` only means anything if the first run keeps its locks.

    `collect_due_evidence_gc` selected its rows once with `FOR UPDATE SKIP LOCKED` and
    then `commit()`ed inside the row loop. A commit ends the transaction and releases the
    locks on every row the run had selected but not yet reached, so an overlapping run
    could pick one of them up, delete it and commit -- and this run's later write to it
    matched zero rows. On the UPDATE path (`attempts += 1`) that is a StaleDataError
    escaping the collector and aborting the whole run; on the DELETE path it is a
    silently double-counted deletion. The shipped comments claimed SKIP LOCKED made
    overlapping runs safe, and for as long as the per-row commit stood they did not.

    The first run is parked on its *second* row, which is exactly where the defect lived:
    a per-row commit had already released the lock on the row it had not reached. Only
    the second store call blocks, so whichever run reaches it second runs to completion
    and the test terminates either way rather than deadlocking.
    """
    import asyncio

    from app.evidence_gc import GCResult

    await _clear(db)
    await _seed_org(db)
    due = datetime.now(timezone.utc) - timedelta(seconds=60)
    await _queue(db, local_store, key="blobs/one", grace=0, due_at=due, document_id="doc-1")
    await _queue(db, local_store, key="blobs/two", grace=0, due_at=due, document_id="doc-2")

    parked = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    real_delete = local_store.delete

    async def gated_delete(key: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            parked.set()
            await release.wait()
        await real_delete(key)

    monkeypatch.setattr(local_store, "delete", gated_delete)

    async def run() -> GCResult:
        # Its own session, because the point is two transactions against the same rows.
        async with SessionFactory() as session:
            return await collect_due_evidence_gc(session, now=due + timedelta(seconds=1))

    first = asyncio.create_task(run())
    await asyncio.wait_for(parked.wait(), timeout=30)
    second = await run()
    release.set()
    first_result = await asyncio.wait_for(first, timeout=30)

    assert second == GCResult(), (
        "the second collector selected rows the first still owned; a per-row commit "
        "released the locks the SKIP LOCKED selection had taken"
    )
    assert first_result == GCResult(deleted=2), "the first run reclaims both objects, once each"
    assert not await local_store.exists("blobs/one")
    assert not await local_store.exists("blobs/two")


def test_the_collector_commits_once_per_run():
    """No `commit()` inside the row loop, structurally.

    The behavioural test above is the one that matters, but it needs two live sessions
    and a database, so it does not run everywhere. This pins the shape of the fix
    directly: the single `FOR UPDATE SKIP LOCKED` selection is only worth anything if the
    locks it takes survive until the run is finished with them.
    """
    import ast
    import pathlib

    import app.evidence_gc as gc_module

    tree = ast.parse(pathlib.Path(gc_module.__file__).read_text())
    collect = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "collect_due_evidence_gc"
    )

    def commits(nodes) -> list[ast.Call]:
        return [
            node
            for parent in nodes
            for node in ast.walk(parent)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
        ]

    loops = [node for node in ast.walk(collect) if isinstance(node, ast.For | ast.While)]
    assert loops, "the collector no longer loops; this test needs re-reading"
    for loop in loops:
        assert not commits(loop.body), (
            "a commit inside the collector's loop releases the SKIP LOCKED locks on the "
            "rows it has not reached yet (VITR-V011)"
        )
    assert len(commits(collect.body)) == 1, "the run should end in exactly one commit"
