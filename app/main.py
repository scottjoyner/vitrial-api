from __future__ import annotations
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal, profile_for
from app.db import session_scope
from app.evidence import put_blob
from app.models import EvidenceBlob
from app.ownership import AuthorizationRejected, require_item_access
from app.schemas import AuthorizationProfile, HealthResponse, SyncBatch, SyncResult, VersionResponse
from app.settings import settings
from app.storage import StorageError, store_for_provider
from app.sync_service import InvalidMutation, apply_push, pull_since

app = FastAPI(title="Vitrial Connected Operations API", version=settings.service_version)

DocumentID = Annotated[str, Path(min_length=1)]


@app.get("/health", response_model=HealthResponse, operation_id="health")
async def health() -> HealthResponse:
    return HealthResponse()


@app.get("/api/v1/version", response_model=VersionResponse, operation_id="version")
async def version() -> VersionResponse:
    return VersionResponse(serviceVersion=settings.service_version)


@app.get("/api/v1/auth/me", response_model=AuthorizationProfile, operation_id="authMe")
async def auth_me(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> AuthorizationProfile:
    return await profile_for(principal, db)


@app.post("/api/v1/sync/push", response_model=SyncResult, operation_id="syncPush")
async def sync_push(
    batch: SyncBatch,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> SyncResult:
    try:
        return await apply_push(db, principal, batch)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except InvalidMutation as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/sync/pull", response_model=SyncBatch, operation_id="syncPull")
async def sync_pull(
    cursor: str | None = None,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> SyncBatch:
    try:
        return await pull_since(db, principal, cursor)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except InvalidMutation as exc:
        raise HTTPException(400, str(exc)) from exc


async def _authorized_blob(
    db: AsyncSession,
    principal: Principal,
    document_id: str,
) -> EvidenceBlob:
    model = await db.get(EvidenceBlob, (principal.organization_id, document_id))
    if model is None:
        raise HTTPException(404)
    try:
        await require_item_access(db, principal, model.item_id)
    except AuthorizationRejected as exc:
        raise HTTPException(403, str(exc)) from exc
    return model


@app.head(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidencePresence",
)
async def evidence_head(
    documentID: DocumentID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    model = await _authorized_blob(db, principal, documentID)
    try:
        present = await store_for_provider(model.storage_provider).exists(model.object_key)
    except StorageError as exc:
        raise HTTPException(503, "evidence object storage unavailable") from exc
    if not present:
        raise HTTPException(404)
    return Response(status_code=200, headers={"X-Content-SHA256": model.sha256})


@app.put(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidenceUpload",
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "contentEncoding": "binary"}
                }
            },
        }
    },
)
async def evidence_put(
    documentID: DocumentID,
    request: Request,
    x_vitrial_item_id: Annotated[str, Header(alias="X-Vitrial-Item-ID", min_length=1)],
    x_vitrial_filename: Annotated[str, Header(alias="X-Vitrial-Filename", min_length=1)],
    x_content_sha256: Annotated[
        str,
        Header(alias="X-Content-SHA256", pattern=r"^[0-9a-fA-F]{64}$"),
    ],
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    model = await put_blob(
        db,
        principal,
        documentID,
        x_vitrial_item_id,
        x_vitrial_filename,
        request.headers.get("content-type", "application/octet-stream"),
        x_content_sha256,
        request.stream(),
    )
    return Response(status_code=201, headers={"X-Content-SHA256": model.sha256})


@app.get(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidenceDownload",
)
async def evidence_get(
    documentID: DocumentID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
):
    model = await _authorized_blob(db, principal, documentID)
    store = store_for_provider(model.storage_provider)
    try:
        if not await store.exists(model.object_key):
            raise HTTPException(404)
    except StorageError as exc:
        raise HTTPException(503, "evidence object storage unavailable") from exc
    return StreamingResponse(
        store.stream(model.object_key),
        media_type=model.mime_type or "application/octet-stream",
        headers={"X-Content-SHA256": model.sha256},
    )
