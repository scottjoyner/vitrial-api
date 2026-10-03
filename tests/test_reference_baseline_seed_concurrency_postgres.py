"""The reference-data baseline seed under a concurrent first read (VITR-V008).

All four reference-data GET routes call `ensure_baseline_publications`, which
checks for the bundled baseline rows and inserts the missing ones. The check is a
plain SELECT and the engine runs READ COMMITTED (`create_async_engine` with no
`isolation_level`, `app/db.py:61`), so two concurrent first reads of a
freshly-onboarded organization both see the rows missing and both insert them. The
loser's INSERT violates the `reference_publications` primary key at the flush in
`_insert_publication`, and nothing in `app/` catches `IntegrityError` -- so the
request became a 500 on a condition that resolves itself.

`tests/test_sync_concurrency.py` pins the same class of property on the push path.
Nothing covered this one: the four GET handlers are only reachable through an
authenticated request, so a check-then-insert in their helper had no test at all.

The race is made deterministic rather than hoped for. `_insert_publication` is
wrapped so that every caller's *first* insert parks until all four readers have
arrived -- which can only happen if all four finished their check and decided the
rows were missing. Without that gate the window between SELECT and flush is a few
microseconds and the test would pass against the broken code most of the time.

Requires PostgreSQL; run with `DATABASE_NULL_POOL=true POSTGRES_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import delete, func, select

from app.auth import Principal
from app.db import SessionFactory
from app.models import Organization
from app import reference_data
from app.reference_data import (
    BASELINE_PUBLICATIONS,
    current_publications,
    get_publication,
    list_publications,
    publication_manifest,
    publication_sha256,
)
from app.reference_models import ReferencePublication

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ORG = "org-baseline-seed-race"
FIRST_PUBLICATION = "catalog-bundled-v1"


def actor() -> Principal:
    return Principal(
        user_id="user-baseline-seed",
        organization_id=ORG,
        membership_id="membership-baseline-seed",
        session_id="session-baseline-seed",
        authorization_revision=1,
        capabilities=frozenset({"sync", "catalog.manage"}),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=False,
        all_projects=False,
    )


async def clear_organization() -> None:
    async with SessionFactory() as db:
        await db.execute(delete(Organization).where(Organization.id == ORG))
        await db.commit()


async def seed_organization() -> None:
    async with SessionFactory() as db:
        await db.execute(delete(Organization).where(Organization.id == ORG))
        db.add(Organization(id=ORG, name="Baseline seed race", authorization_revision=1))
        await db.commit()


class FirstInsertBarrier:
    """Parks every reader's first baseline insert until all of them have decided.

    Without this the four sessions race through check-then-insert on their own and
    the loser only sometimes loses -- which is the difference between a test that
    catches the defect and a test that agrees with whatever the code does.
    """

    def __init__(self, expected: int, insert) -> None:
        self.expected = expected
        self.arrived = 0
        self.released = asyncio.Event()
        # Bound before monkeypatching, so the wrapper delegates to the real insert
        # rather than to itself.
        self._insert = insert

    async def __call__(self, db, organization_id, request, *, published_by_user_id):
        self.arrived += 1
        if self.arrived >= self.expected:
            self.released.set()
        await self.released.wait()
        return await self._insert(
            db, organization_id, request, published_by_user_id=published_by_user_id
        )


@pytest.fixture
def gated_seed(monkeypatch):
    barrier = FirstInsertBarrier(
        expected=4, insert=reference_data._insert_publication
    )
    monkeypatch.setattr(reference_data, "_insert_publication", barrier)
    return barrier


async def _read_list_publications():
    async with SessionFactory() as db:
        return await list_publications(db, actor(), kind=None)


async def _read_current_publications():
    async with SessionFactory() as db:
        return await current_publications(db, actor())


async def _read_manifest():
    async with SessionFactory() as db:
        return await publication_manifest(db, actor())


async def _read_one_publication():
    async with SessionFactory() as db:
        return await get_publication(db, actor(), FIRST_PUBLICATION)


@pytest.mark.asyncio
async def test_concurrent_first_reads_seed_once_and_all_succeed(gated_seed):
    """Four GET routes, first read each, no 500 and no duplicate baseline rows."""
    await clear_organization()
    await seed_organization()
    try:
        results = await asyncio.gather(
            _read_list_publications(),
            _read_current_publications(),
            _read_manifest(),
            _read_one_publication(),
        )
    finally:
        await clear_organization()

    # All four reached the insert decision, so the race was actually exercised.
    assert gated_seed.arrived >= 4

    listed, current, manifest, one = results
    baseline_ids = {request.publicationID for request in BASELINE_PUBLICATIONS}
    assert one.publicationID == FIRST_PUBLICATION
    assert {p.publicationID for p in listed.publications} == baseline_ids
    assert {p.publicationID for p in current.publications} <= baseline_ids
    assert {p.publicationID for p in manifest.available} == baseline_ids
    assert {p.publicationID for p in manifest.current} <= baseline_ids


@pytest.mark.asyncio
async def test_baseline_rows_are_written_once_by_the_winner(gated_seed):
    """The surviving rows are the winner's, exactly one per bundled publication."""
    await clear_organization()
    await seed_organization()
    try:
        await asyncio.gather(
            _read_list_publications(),
            _read_current_publications(),
            _read_manifest(),
            _read_one_publication(),
        )

        async with SessionFactory() as db:
            total = await db.scalar(
                select(func.count()).select_from(ReferencePublication).where(
                    ReferencePublication.organization_id == ORG
                )
            )
            rows = (
                await db.scalars(
                    select(ReferencePublication).where(
                        ReferencePublication.organization_id == ORG
                    )
                )
            ).all()
    finally:
        await clear_organization()

    assert total == len(BASELINE_PUBLICATIONS)
    by_id = {row.publication_id: row for row in rows}
    for request in BASELINE_PUBLICATIONS:
        row = by_id[request.publicationID]
        assert row.content_sha256 == publication_sha256(request)
        assert row.published_by_user_id == "system:baseline"