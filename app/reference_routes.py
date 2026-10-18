from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal
from app.db import session_scope
from app.reference_data import (
    PublicationConflict,
    PublicationKind,
    PublicationNotFound,
    ReferencePublicationCreate,
    ReferencePublicationListResponse,
    ReferencePublicationManifest,
    ReferencePublicationResponse,
    current_publications,
    get_publication,
    list_publications,
    publication_manifest,
    publish_reference,
)

router = APIRouter(prefix="/api/v1/reference-data", tags=["reference-data"])


def _private_reference_response(response: Response) -> None:
    """Company supplier costs/pricing must never be cached as public content."""
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"



@router.get(
    "/manifest",
    response_model=ReferencePublicationManifest,
    operation_id="referencePublicationManifest",
)
async def manifest(
    response: Response,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> ReferencePublicationManifest:
    _private_reference_response(response)
    return await publication_manifest(db, principal)


@router.get(
    "/current",
    response_model=ReferencePublicationListResponse,
    operation_id="referenceCurrentPublications",
)
async def current(
    response: Response,
    at: datetime | None = Query(default=None),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> ReferencePublicationListResponse:
    _private_reference_response(response)
    return await current_publications(db, principal, at=at)


@router.get(
    "/publications",
    response_model=ReferencePublicationListResponse,
    operation_id="referencePublications",
)
async def publications(
    response: Response,
    kind: PublicationKind | None = Query(default=None),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> ReferencePublicationListResponse:
    _private_reference_response(response)
    return await list_publications(db, principal, kind=kind)


@router.get(
    "/publications/{publicationID}",
    response_model=ReferencePublicationResponse,
    operation_id="referencePublication",
)
async def publication(
    publicationID: Annotated[str, Path(min_length=1, max_length=256)],
    response: Response,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> ReferencePublicationResponse:
    try:
        _private_reference_response(response)
        return await get_publication(db, principal, publicationID)
    except PublicationNotFound as exc:
        raise HTTPException(404, "reference publication not found") from exc


@router.post(
    "/publications",
    response_model=ReferencePublicationResponse,
    operation_id="publishReferencePublication",
    status_code=status.HTTP_201_CREATED,
)
async def publish(
    request: ReferencePublicationCreate,
    response: Response,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> ReferencePublicationResponse:
    try:
        _private_reference_response(response)
        return await publish_reference(db, principal, request)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except PublicationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
