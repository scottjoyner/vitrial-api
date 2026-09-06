from __future__ import annotations
import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.models import EvidenceBlob
from app.ownership import AuthorizationRejected, require_item_access
from app.settings import settings


def object_path(principal: Principal, document_id: str) -> Path:
    safe_doc = hashlib.sha256(document_id.encode()).hexdigest()
    return settings.evidence_root / principal.organization_id / safe_doc


async def put_blob(
    db: AsyncSession,
    principal: Principal,
    document_id: str,
    item_id: str,
    filename: str,
    mime_type: str,
    expected_sha256: str,
    body: bytes,
) -> EvidenceBlob:
    model = await db.get(EvidenceBlob, (principal.organization_id, document_id))
    if model is not None and model.item_id != item_id:
        # Reject a client attempt to re-parent existing evidence before touching canonical bytes.
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

    digest = hashlib.sha256(body).hexdigest()
    if digest.lower() != expected_sha256.lower():
        raise HTTPException(409, "content digest mismatch")

    destination = object_path(principal, document_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{uuid4().hex}.tmp")
    temporary.write_bytes(body)
    temporary.replace(destination)

    if model is None:
        model = EvidenceBlob(
            organization_id=principal.organization_id,
            document_id=document_id,
            item_id=canonical_item_id,
            filename=filename,
            mime_type=mime_type,
            sha256=digest,
            size_bytes=len(body),
            object_key=str(destination),
        )
        db.add(model)
    else:
        model.filename = filename
        model.mime_type = mime_type
        model.sha256 = digest
        model.size_bytes = len(body)
        model.object_key = str(destination)
    await db.commit()
    return model
