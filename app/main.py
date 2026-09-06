from __future__ import annotations
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal, profile_for
from app.db import session_scope
from app.evidence import object_path, put_blob
from app.models import EvidenceBlob
from app.schemas import AuthorizationProfile, HealthResponse, SyncBatch, SyncResult, VersionResponse
from app.settings import settings
from app.sync_service import InvalidMutation, apply_push, pull_since

app = FastAPI(title="Vitrial Connected Operations API", version=settings.service_version)

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()

@app.get("/api/v1/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    return VersionResponse(serviceVersion=settings.service_version)

@app.get("/api/v1/auth/me", response_model=AuthorizationProfile)
async def auth_me(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> AuthorizationProfile:
    return await profile_for(principal, db)

@app.post("/api/v1/sync/push", response_model=SyncResult)
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

@app.get("/api/v1/sync/pull", response_model=SyncBatch)
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

@app.head("/api/v1/sync/evidence-blobs/{document_id}")
async def evidence_head(
    document_id: str,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    model = await db.get(EvidenceBlob, (principal.organization_id, document_id))
    if model is None or not object_path(principal, document_id).exists():
        raise HTTPException(404)
    return Response(status_code=200, headers={"X-Content-SHA256": model.sha256})

@app.put("/api/v1/sync/evidence-blobs/{document_id}")
async def evidence_put(
    document_id: str,
    request: Request,
    x_vitrial_item_id: str = Header(alias="X-Vitrial-Item-ID"),
    x_vitrial_filename: str = Header(alias="X-Vitrial-Filename"),
    x_content_sha256: str = Header(alias="X-Content-SHA256"),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    body = await request.body()
    model = await put_blob(
        db, principal, document_id, x_vitrial_item_id, x_vitrial_filename,
        request.headers.get("content-type", "application/octet-stream"),
        x_content_sha256, body,
    )
    return Response(status_code=201, headers={"X-Content-SHA256": model.sha256})

@app.get("/api/v1/sync/evidence-blobs/{document_id}")
async def evidence_get(
    document_id: str,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
):
    model = await db.get(EvidenceBlob, (principal.organization_id, document_id))
    path = object_path(principal, document_id)
    if model is None or not path.exists():
        raise HTTPException(404)
    return FileResponse(
        path,
        media_type=model.mime_type or "application/octet-stream",
        headers={"X-Content-SHA256": model.sha256},
    )
