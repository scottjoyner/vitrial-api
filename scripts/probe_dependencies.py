#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.settings import settings
from app.storage import S3ObjectStore, StorageError, current_store


async def probe_database() -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def probe_storage() -> None:
    store = current_store()
    if isinstance(store, S3ObjectStore):
        try:
            await asyncio.to_thread(store.client.head_bucket, Bucket=store.bucket)
        except Exception as exc:
            raise StorageError("S3 bucket unavailable") from exc
        return

    root = Path(settings.evidence_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ".readiness"
        marker.write_bytes(b"")
        marker.unlink(missing_ok=True)
    except OSError as exc:
        raise StorageError("local evidence root unavailable") from exc


async def run_probe() -> dict[str, str]:
    result: dict[str, str] = {}
    await probe_database()
    result["database"] = "ok"
    await probe_storage()
    result["objectStorage"] = "ok"
    result["status"] = "ready"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe deployment dependencies without printing secrets")
    parser.add_argument("--quiet", action="store_true", help="emit no success output")
    args = parser.parse_args()
    try:
        result = asyncio.run(run_probe())
    except Exception as exc:
        if not args.quiet:
            print(json.dumps({"status": "not-ready", "errorType": type(exc).__name__}, sort_keys=True))
        return 1
    if not args.quiet:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
