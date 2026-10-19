"""The VITR-V004 backfill: dry run writes nothing, execute completes, re-runs are no-ops.

Organizations created before `03e10d9` were never seeded at creation; the lazy GET seed is
the only thing giving them baselines. `scripts/backfill_baseline_publications.py` exists so
that seed can eventually be deleted. These tests pin the three properties an operator relies
on when running it against production.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, func, select

from app.db import SessionFactory
from app.models import Organization
from app.reference_data import BASELINE_PUBLICATIONS
from app.reference_models import ReferencePublication
from scripts.backfill_baseline_publications import backfill

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

LEGACY = ("org-backfill-legacy-1", "org-backfill-legacy-2")
COMPLETE = "org-backfill-complete"


async def _count(db, organization_id: str) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(ReferencePublication)
            .where(ReferencePublication.organization_id == organization_id)
        )
        or 0
    )


async def _setup(db) -> None:
    """Two legacy organizations with nothing seeded, one already complete.

    Scoped deletes only: this file never truncates shared tables, so it neither depends on
    nor disturbs other tests' rows.
    """
    owned = (*LEGACY, COMPLETE)
    await db.execute(
        delete(ReferencePublication).where(ReferencePublication.organization_id.in_(owned))
    )
    await db.execute(delete(Organization).where(Organization.id.in_(owned)))
    await db.commit()
    for organization_id in owned:
        db.add(Organization(id=organization_id, name=organization_id, authorization_revision=0))
    await db.commit()

    from app.reference_data import ensure_baseline_publications_for_organization

    await ensure_baseline_publications_for_organization(db, COMPLETE)


def _ours(result) -> dict[str, list[str]]:
    """Restrict a whole-database result to this file's organizations."""
    return {k: v for k, v in result.missing.items() if k in (*LEGACY, COMPLETE)}


@pytest.mark.asyncio
async def test_dry_run_reports_missing_baselines_and_writes_nothing():
    async with SessionFactory() as db:
        await _setup(db)
        result = await backfill(db, execute=False)
        ours = _ours(result)

        assert set(ours) == set(LEGACY), "only the unseeded organizations need a seed"
        expected = sorted(request.publicationID for request in BASELINE_PUBLICATIONS)
        for organization_id in LEGACY:
            assert sorted(ours[organization_id]) == expected
            assert await _count(db, organization_id) == 0, "a dry run must not write"
        assert result.seeded == 0


@pytest.mark.asyncio
async def test_execute_completes_every_organization_and_reports_zero_incomplete():
    async with SessionFactory() as db:
        await _setup(db)
        before_complete = await _count(db, COMPLETE)

        result = await backfill(db, execute=True)

        assert result.incomplete == 0, "after --execute every organization must be complete"
        for organization_id in LEGACY:
            assert await _count(db, organization_id) == len(BASELINE_PUBLICATIONS)
        assert await _count(db, COMPLETE) == before_complete, (
            "an already-complete organization must not gain duplicate rows"
        )


@pytest.mark.asyncio
async def test_a_second_execute_is_a_no_op():
    """Idempotence: safe to re-run, and safe to run while live traffic seeds concurrently."""
    async with SessionFactory() as db:
        await _setup(db)
        await backfill(db, execute=True)
        counts = {o: await _count(db, o) for o in (*LEGACY, COMPLETE)}

        again = await backfill(db, execute=True)

        assert _ours(again) == {}, "nothing of ours should be missing on a second run"
        assert {o: await _count(db, o) for o in (*LEGACY, COMPLETE)} == counts
