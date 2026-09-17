from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal
from app.db import session_scope
from app.settings import settings
from app.sync_service import InvalidMutation
from app.sync_v2 import (
    SyncBatchV2,
    SyncResultV2,
    VersionResponseV2,
    apply_push_v2,
    pull_since_v2,
)

router = APIRouter(prefix="/api/v2", tags=["sync-v2"])


@router.get("/version", response_model=VersionResponseV2, operation_id="versionV2")
async def version_v2() -> VersionResponseV2:
    return VersionResponseV2(serviceVersion=settings.service_version)


@router.post("/sync/push", response_model=SyncResultV2, operation_id="syncPushV2")
async def sync_push_v2(
    batch: SyncBatchV2,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> SyncResultV2:
    try:
        return await apply_push_v2(db, principal, batch)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except InvalidMutation as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/sync/pull", response_model=SyncBatchV2, operation_id="syncPullV2")
async def sync_pull_v2(
    cursor: str | None = None,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> SyncBatchV2:
    try:
        return await pull_since_v2(db, principal, cursor)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except InvalidMutation as exc:
        raise HTTPException(400, str(exc)) from exc
