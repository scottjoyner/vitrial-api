#!/usr/bin/env python3
"""Seed the bundled baseline publications for every existing organization.

The operator step that lets `VITR-V004` close.

Four reference-data GET helpers call `ensure_baseline_publications`, so a read can insert
and commit. Since `03e10d9`, `bootstrap_identity` seeds at organization creation, so every
NEW organization is complete before its first read. The lazy GET seed was deliberately
kept for organizations created BEFORE that change: removing it would change what a GET
returns for them, which is client-visible on a pinned contract.

This script removes that last reason. Run it once against production; when it reports
`incomplete=0`, every organization has its baselines, the lazy GET seed is a no-op for all
of them, and it can be deleted -- turning the four read routes back into reads.

Safe by construction:
- **Dry run by default.** It reports which organizations are missing which baselines and
  writes nothing unless `--execute` is given.
- **Idempotent.** It inserts only what is missing, through the same race-tolerant
  `ensure_baseline_publications_for_organization` the write path uses, so re-running it, or
  running it while live traffic seeds concurrently, is harmless (`VITR-V008` handles the
  race).
- **Exit status is the answer.** 0 means every organization is complete after the run
  (or, in a dry run, already was); 1 means something is still missing, so it can gate a
  deploy step.

Usage:
    python scripts/backfill_baseline_publications.py            # report only
    python scripts/backfill_baseline_publications.py --execute  # seed what is missing
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.models import Organization
from app.reference_data import (
    BASELINE_PUBLICATIONS,
    ensure_baseline_publications_for_organization,
)
from app.reference_models import ReferencePublication


@dataclass
class BackfillResult:
    organizations: int = 0
    # organization id -> baseline publication ids it was missing BEFORE this run
    missing: dict[str, list[str]] = field(default_factory=dict)
    seeded: int = 0
    # organizations still missing a baseline AFTER this run (dry run: same as `missing`)
    incomplete: int = 0


async def missing_baselines(db: AsyncSession, organization_id: str) -> list[str]:
    missing = []
    for request in BASELINE_PUBLICATIONS:
        if await db.get(ReferencePublication, (organization_id, request.publicationID)) is None:
            missing.append(request.publicationID)
    return missing


async def backfill(db: AsyncSession, *, execute: bool) -> BackfillResult:
    result = BackfillResult()
    organization_ids = list(
        (await db.scalars(select(Organization.id).order_by(Organization.id))).all()
    )
    result.organizations = len(organization_ids)

    for organization_id in organization_ids:
        missing = await missing_baselines(db, organization_id)
        if not missing:
            continue
        result.missing[organization_id] = missing
        if execute:
            await ensure_baseline_publications_for_organization(db, organization_id)
            result.seeded += len(missing)

    if execute:
        # Re-measured against committed state rather than inferred from what this run did:
        # a concurrent seed, a race the retry absorbed, or a partial failure all show up
        # here as the truth, not as an optimistic count.
        # An explicit loop, not sum(... if await ...): an `await` inside a generator
        # expression makes it an ASYNC generator, which sum() cannot iterate -- the first
        # version crashed with "'async_generator' object is not iterable" on --execute.
        for organization_id in organization_ids:
            if await missing_baselines(db, organization_id):
                result.incomplete += 1
    else:
        result.incomplete = len(result.missing)
    return result


async def run(execute: bool) -> int:
    async with SessionFactory() as db:
        result = await backfill(db, execute=execute)
    mode = "executed" if execute else "dry-run"
    for organization_id, missing in sorted(result.missing.items()):
        print(f"  {organization_id}: missing {', '.join(missing)}")
    print(
        f"baseline backfill {mode}: organizations={result.organizations} "
        f"needing_seed={len(result.missing)} seeded={result.seeded} "
        f"incomplete={result.incomplete}"
    )
    return 1 if result.incomplete else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed bundled baseline publications for every existing organization."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually insert missing baselines; default is a dry run",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.execute))


if __name__ == "__main__":
    raise SystemExit(main())
