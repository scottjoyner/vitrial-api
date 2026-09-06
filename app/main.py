from __future__ import annotations
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal, profile_for
from app.db import session_scope
from app.evidence import object_path, put_blob
from app.models import EvidenceBlob
from app.ownership import AuthorizationRejected, require_item_access
from app.schemas import AuthorizationProfile, HealthResponse, SyncBatch, SyncResult, VersionResponse
from app.settings import settings
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


@app.head(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidencePresence",
)
async def evidence_head(
    documentID: DocumentID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    model = await db.get(EvidenceBlob, (principal.organization_id, documentID))
    if model is None or not object_path(principal, documentID).exists():
        raise HTTPException(404)
    try:
        await require_item_access(db, principal, model.item_id)
    except AuthorizationRejected as exc:
        raise HTTPException(403, str(exc)) from exc
    return Response(status_code=200, headers={"X-Content-SHA256": model.sha256})


@app.put(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidenceUpload",
    status_code=201,
)
async def evidence_put(
    documentID: DocumentID,
    request: Request,
    body: Annotated[bytes, Body(media_type="application/octet-stream")],
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
        body,
    )
    return Response(status_code=201, headers={"X-Content-SHA256": model.sha256})


@app.get(
    "/api/v1/sync/evidence-blobs/{documentID}",
    operation_id="evidenceDownload",
    response_class=FileResponse,
)
async def evidence_get(
    documentID: DocumentID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
):
    model = await db.get(EvidenceBlob, (principal.organization_id, documentID))
    path = object_path(principal, documentID)
    if model is None or not path.exists():
        raise HTTPException(404)
    try:
        await require_item_access(db, principal, model.item_id)
    except AuthorizationRejected as exc:
        raise HTTPException(403, str(exc)) from exc
    return FileResponse(
        path,
        media_type=model.mime_type or "application/octet-stream",
        headers={"X-Content-SHA256": model.sha256},
    )
