"""Concurrent evidence replace: no orphan, no dangling pointer.

#28 lists evidence-replace contention as "not directly covered". VITR-V007 showed why it
matters: this exact path -- an upload streaming its object BEFORE taking the organization
lock -- once deleted an object the client had been told was stored. Its fix is tested with
stubs; this drives two real `put_blob` calls concurrently, each on its own session, against
a real database and a real (local) object store.

The invariant, whichever upload wins:

1. **The pointer names an object that exists.** The canonical EvidenceBlob row's object key
   is present in the store. Violating this is a 201 for evidence that 404s.
2. **No orphan.** Every object the two uploads wrote is either the canonical one or has a
   pending GC row. An object that is neither is storage that nothing will ever reclaim.
3. **Both callers got an answer.** Neither request is left with an unhandled error; the
   loser is superseded, not crashed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.auth import Principal
from app.db import SessionFactory
from app.evidence import put_blob
from app.models import (
    CanonicalCustomer,
    CanonicalItem,
    CanonicalProject,
    CanonicalProjectSector,
    EvidenceBlob,
    EvidenceObjectGC,
    Organization,
)
from app.storage import LocalObjectStore

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)


async def _one_chunk(data: bytes):
    yield data


@pytest.fixture
def local_store(tmp_path: Path, monkeypatch) -> LocalObjectStore:
    import app.storage as storage

    store = LocalObjectStore(tmp_path)
    monkeypatch.setattr(storage, "_LOCAL_STORE", store)
    monkeypatch.setattr("app.evidence.settings.evidence_storage_provider", "local")
    return store


async def _seed() -> tuple[Principal, str, str]:
    suffix = uuid4().hex
    org_id, item_id, document_id = f"org-race-{suffix}", f"item-race-{suffix}", f"doc-race-{suffix}"
    async with SessionFactory() as db:
        db.add(Organization(id=org_id, name="Replace contention"))
        await db.flush()
        db.add(CanonicalCustomer(organization_id=org_id, customer_id=f"cust-{suffix}"))
        await db.flush()
        db.add(CanonicalProject(
            organization_id=org_id, project_id=f"proj-{suffix}", customer_id=f"cust-{suffix}",
        ))
        await db.flush()
        db.add(CanonicalProjectSector(
            organization_id=org_id, project_sector_id=f"sect-{suffix}",
            project_id=f"proj-{suffix}", sector_id="sector-aluminum-glass-steel",
        ))
        await db.flush()
        db.add(CanonicalItem(
            organization_id=org_id, item_id=item_id, project_id=f"proj-{suffix}",
            project_sector_id=f"sect-{suffix}",
        ))
        await db.commit()
    principal = Principal(
        user_id=f"user-{suffix}", organization_id=org_id, membership_id=f"m-{suffix}",
        session_id=f"s-{suffix}", authorization_revision=1,
        capabilities=frozenset({"sync", "item.evidence.manage"}),
        customer_ids=frozenset(), project_ids=frozenset(),
        all_customers=True, all_projects=True,
    )
    return principal, item_id, document_id


async def _upload(principal: Principal, item_id: str, document_id: str, data: bytes):
    async with SessionFactory() as db:
        return await put_blob(
            db, principal, document_id, item_id, "photo.jpg", "image/jpeg",
            hashlib.sha256(data).hexdigest(), _one_chunk(data),
        )


@pytest.mark.asyncio
async def test_two_concurrent_replaces_leave_no_orphan_and_no_dangling_pointer(local_store):
    principal, item_id, document_id = await _seed()
    # An initial version, so both racers are REPLACES of an existing canonical blob.
    first = await _upload(principal, item_id, document_id, b"version-0")

    results = await asyncio.gather(
        _upload(principal, item_id, document_id, b"version-A" * 1000),
        _upload(principal, item_id, document_id, b"version-B" * 1000),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], f"a concurrent replace crashed instead of being superseded: {errors!r}"

    async with SessionFactory() as db:
        canonical = await db.get(EvidenceBlob, (principal.organization_id, document_id))
        queued = set(
            (await db.scalars(
                select(EvidenceObjectGC.object_key).where(
                    EvidenceObjectGC.organization_id == principal.organization_id,
                    EvidenceObjectGC.document_id == document_id,
                )
            )).all()
        )

    assert canonical is not None
    # 1. The pointer names an object that exists.
    assert await local_store.exists(canonical.object_key), (
        "the canonical pointer names an object that is not in the store"
    )
    # 2. No orphan: every written object is canonical or queued for collection.
    written = {first.object_key, *(r.object_key for r in results)}
    orphans = sorted(k for k in written if k != canonical.object_key and k not in queued)
    assert orphans == [], f"objects nothing will ever reclaim: {orphans}"
    # The canonical object must never be queued for deletion while canonical.
    assert canonical.object_key not in queued, "the live object is queued for deletion"
