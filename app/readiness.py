from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.settings import settings
from app.storage import S3ObjectStore, StorageError, current_store


@dataclass(frozen=True)
class DependencyReadiness:
    database: str
    object_storage: str

    @property
    def status(self) -> str:
        if self.database == "ok" and self.object_storage == "ok":
            return "ready"
        return "not-ready"


async def probe_database() -> None:
    """Verify PostgreSQL without returning or logging connection details."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def probe_storage() -> None:
    """Verify the configured evidence store without exposing provider credentials."""
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


async def collect_dependency_readiness() -> DependencyReadiness:
    """Return only categorical dependency state suitable for an operator-facing endpoint."""
    database = "ok"
    object_storage = "ok"

    try:
        await probe_database()
    except Exception:
        database = "unavailable"

    try:
        await probe_storage()
    except Exception:
        object_storage = "unavailable"

    return DependencyReadiness(database=database, object_storage=object_storage)
