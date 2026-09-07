#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio

from app.db import SessionFactory
from app.evidence_gc import collect_due_evidence_gc


async def run(limit: int, execute: bool) -> int:
    async with SessionFactory() as db:
        result = await collect_due_evidence_gc(db, limit=limit, dry_run=not execute)
    mode = "executed" if execute else "dry-run"
    print(
        f"evidence gc {mode}: deletable={result.deleted} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    return 1 if result.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely collect non-canonical evidence objects.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually delete due objects; default is a dry run",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.limit, args.execute))


if __name__ == "__main__":
    raise SystemExit(main())
