from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.evidence_gc import queue_blob_gc
from app.models import EvidenceBlob
from app.ownership import AuthorizationRejected, require_item_access
from app.storage import (
    StorageDigestMismatch,
    StorageError,
    current_store,
    object_key_for,
)


async def bytes_chunks(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def put_blob(
    db: AsyncSession,
    principal: Principal,
    document_id: str,
    item_id: str,
    filename: str,
    mime_type: str,
    expected_sha256: str,
    chunks: AsyncIterable[bytes] | bytes,
) -> EvidenceBlob:
    model = await db.get(EvidenceBlob, (principal.organization_id, document_id))
    if model is not None and model.item_id != item_id:
        # Reject re-parenting before touching any object-store bytes.
        raise HTTPException(409, "evidence ownership mismatch")

    canonical_item_id = model.item_id if model is not None else item_id
    try:
        await require_item_access(
            db,
            principal,
            canonical_item_id,
            capability="item.evidence.manage",
        )
    except AuthorizationRejected as exc:
        raise HTTPException(403, str(exc)) from exc

    if isinstance(chunks, bytes):
        chunks = bytes_chunks(chunks)

    store = current_store()
    new_key = object_key_for(principal.organization_id, document_id, expected_sha256)
    old_provider = model.storage_provider if model is not None else None
    old_key = model.object_key if model is not None else None

    try:
        stored = await store.put_verified(new_key, chunks, expected_sha256)
    except StorageDigestMismatch as exc:
        raise HTTPException(409, "content digest mismatch") from exc
    except StorageError as exc:
        raise HTTPException(503, "evidence object storage unavailable") from exc

    created = model is None
    if model is None:
        model = EvidenceBlob(
            organization_id=principal.organization_id,
            document_id=document_id,
            item_id=canonical_item_id,
            filename=filename,
            mime_type=mime_type,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            storage_provider=store.provider,
            object_key=new_key,
        )
        db.add(model)
    else:
        if old_provider != store.provider or old_key != new_key:
            await queue_blob_gc(db, model, reason="replaced")
        model.filename = filename
        model.mime_type = mime_type
        model.sha256 = stored.sha256
        model.size_bytes = stored.size_bytes
        model.storage_provider = store.provider
        model.object_key = new_key

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        # New immutable content has not become canonical. Best-effort cleanup is safe unless this
        # was an exact rewrite of the already-canonical key.
        if created or old_provider != store.provider or old_key != new_key:
            try:
                await store.delete(new_key)
            except StorageError:
                pass
        raise
    return model
