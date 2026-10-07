from __future__ import annotations
import logging
import time
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import (
    AdminBootstrapRequest,
    AdminBootstrapResponse,
    AdminSessionRevokeRequest,
    AdminSessionRevokeResponse,
    bootstrap_identity,
    require_admin_key,
    revoke_session,
)
from app.auth import Principal, current_principal, profile_for
from app.auth_session_routes import router as auth_session_router
from app.db import session_scope
from app.drain import drain_state
from app.drain import lifespan as drain_lifespan
from app.evidence import put_blob
from app.models import EvidenceBlob
from app.metrics import refresh_dependency_gauges
from app.metrics import refresh_gc_backlog
from app.metrics import registry
from app.metrics_middleware import MetricsMiddleware
from app.request_size import RequestSizeLimitMiddleware
from app.rate_limit import (
    client_key_for,
    limiter_for,
    limiter_key_for,
    throttled_surface_for,
)
from app.observability import (
    begin_request,
    bind_request_metadata,
    end_request,
    log_event,
    request_id_for_header,
    trace_context_for_header,
)
from app.ownership import AuthorizationRejected, require_item_access
from app.readiness import collect_dependency_readiness
from app.reference_routes import router as reference_router
from app.schemas import AuthorizationProfile, HealthResponse, ReadinessResponse, SyncBatch, SyncResult, VersionResponse
from app.settings import settings
from app.storage import StorageError, store_for_provider
from app.sync_service import InvalidMutation, apply_push, pull_since
from app.sync_v2_routes import router as sync_v2_router

app = FastAPI(
    title="Vitrial Connected Operations API",
    version=settings.service_version,
    lifespan=drain_lifespan,
)
app.include_router(auth_session_router)
app.include_router(reference_router)
app.include_router(sync_v2_router)

DocumentID = Annotated[str, Path(min_length=1)]


def _route_template(request: Request) -> str | None:
    route = request.scope.get("route")
    return getattr(route, "path", None)


@app.middleware("http")
async def request_correlation(request: Request, call_next):
    request_id = request_id_for_header(request.headers.get("x-request-id"))
    # W3C Trace Context: a valid caller `traceparent` joins this request to the caller's
    # trace; anything else starts a new one. Logged on every line as traceID/spanID/
    # parentSpanID. Not echoed in a response header -- the response shape is pinned to the
    # V1 client contract, and logs are where cross-system correlation is consumed today.
    trace = trace_context_for_header(request.headers.get("traceparent"))
    tokens = begin_request(request_id, trace)
    bind_request_metadata(
        device_id=request.headers.get("x-vitrial-device-id"),
        app_version=request.headers.get("x-vitrial-app-version"),
        app_build=request.headers.get("x-vitrial-app-build"),
    )
    started = time.perf_counter()
    # Do not log the unresolved raw URL here: resource IDs appear in evidence/admin paths.
    # Device identity is stored only as a one-way correlation reference by observability.py.
    log_event("request.started", method=request.method)
    try:
        response = await call_next(request)
    except Exception as exc:
        log_event(
            "request.failed",
            level=logging.ERROR,
            method=request.method,
            routeTemplate=_route_template(request),
            durationMs=round((time.perf_counter() - started) * 1000, 2),
            errorType=type(exc).__name__,
        )
        raise
    else:
        response.headers["X-Request-ID"] = request_id
        log_event(
            "request.completed",
            method=request.method,
            routeTemplate=_route_template(request),
            statusCode=response.status_code,
            durationMs=round((time.perf_counter() - started) * 1000, 2),
        )
        return response
    finally:
        end_request(tokens)


@app.middleware("http")
async def auth_surface_rate_limit(request: Request, call_next):
    """Bound request rate on the unauthenticated auth surface and the admin plane.

    Registered after `request_correlation` so a throttled request still gets a request
    id and a completed-event log line, and so it is refused *before* the route handler:
    no credential is read, no database work is done, and no `auth.pairing_exchange_*`
    event is emitted. That last point matters, because unbounded rejected traffic is
    otherwise an unbounded log-volume source.

    The 429 body is fixed and carries no credential-derived information, so throttling
    cannot be used as an oracle for whether a pairing code exists.

    A limiter fault fails open: an internal error in the limiter must not become an
    outage on the sign-in path.
    """
    surface = throttled_surface_for(request.url.path)
    if surface is None:
        return await call_next(request)
    try:
        allowed, retry_after = limiter_for().allow(
            limiter_key_for(surface, client_key_for(request))
        )
    except Exception as exc:  # pragma: no cover - defensive
        log_event(
            "auth.rate_limit_bypassed",
            level=logging.ERROR,
            errorType=type(exc).__name__,
        )
        return await call_next(request)
    if allowed:
        return await call_next(request)
    retry_after_seconds = max(1, int(retry_after + 0.999))
    log_event(
        "auth.rate_limit_refused",
        level=logging.WARNING,
        routeTemplate=_route_template(request),
        retryAfterSeconds=retry_after_seconds,
    )
    # Validated false positive, suppressed on the statement below for this ONE rule. The
    # only header value derived from the request path is Retry-After, and it is int() of a
    # limiter timestamp difference: the path selects WHICH bucket (throttled_surface_for),
    # it never contributes bytes to the header. The rest of main.py stays fully scanned.
    return JSONResponse(  # ubs:ignore[py.security.header-injection]
        status_code=429,
        content={"detail": "too many requests"},
        headers={
            "Retry-After": str(retry_after_seconds),
            "Cache-Control": "no-store",
        },
    )


@app.get("/health", response_model=HealthResponse, operation_id="health")
async def health() -> HealthResponse:
    return HealthResponse()


@app.get("/ready", response_model=ReadinessResponse, include_in_schema=False)
async def ready(response: Response) -> ReadinessResponse:
    # Draining is a readiness fact, not a dependency fact: the database and object
    # store may both be perfectly healthy while this instance must receive no new
    # work. Report it as not-ready so the edge stops routing here while in-flight
    # requests finish. Dependencies are still probed and still reported, so an
    # operator can tell "draining on purpose" from "draining because broken".
    if drain_state.draining:
        readiness = await collect_dependency_readiness()
        response.status_code = 503
        return ReadinessResponse(
            status="not-ready",
            serviceVersion=settings.service_version,
            database=readiness.database,
            objectStorage=readiness.object_storage,
        )
    readiness = await collect_dependency_readiness()
    if readiness.status != "ready":
        response.status_code = 503
    return ReadinessResponse(
        status=readiness.status,
        serviceVersion=settings.service_version,
        database=readiness.database,
        objectStorage=readiness.object_storage,
    )


@app.get("/metrics", include_in_schema=False)
async def metrics(
    _: None = Depends(require_admin_key),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    """Prometheus exposition of the counters that back the runbook.

    Excluded from the schema because the pinned public contract does not describe it,
    and gated on the admin key for the same reason the other internal routes are:
    the counters carry route names, status classes and rejection codes, which is
    reconnaissance even without a body. Disabled entirely when no admin key hash is
    configured, so a default deployment exposes nothing.
    """
    refresh_dependency_gauges()
    # One cheap count per scrape (two indexed-or-small aggregates over the GC queue),
    # behind the admin key, at Prometheus's scrape interval -- not per request. Fails soft:
    # a database error leaves the backlog gauge unset for this scrape and the rest renders.
    await refresh_gc_backlog(db)
    return Response(content=registry.render(), media_type="text/plain; version=0.0.4")


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
        # The body is a raw byte stream (request.stream()), not a base64
        # string. Declaring `contentEncoding: binary` would tell a code
        # generator to base64-encode the payload before sending, and the server
        # would then store that base64 text as if it were the file. An empty
        # schema models the raw octet-stream body directly, which is also what
        # the client's contract validator requires.
        "requestBody": {
            "required": True,
            "content": {"application/octet-stream": {}},
        }
    },
)
async def evidence_put(
    documentID: DocumentID,
    request: Request,
    x_vitrial_item_id: Annotated[str, Header(alias="X-Vitrial-Item-ID", min_length=1, max_length=256)],
    x_vitrial_filename: Annotated[str, Header(alias="X-Vitrial-Filename", min_length=1, max_length=512)],
    x_content_sha256: Annotated[
        str,
        Header(alias="X-Content-SHA256", pattern=r"^[0-9a-fA-F]{64}$"),
    ],
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> Response:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > settings.evidence_max_bytes:
        raise HTTPException(413, "evidence blob exceeds configured upload limit")

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
    response_class=StreamingResponse,
    openapi_extra={
        # Without response_class above, FastAPI documents the 200 as
        # application/json — it defaults to JSONResponse and the handler
        # declares no response_model — and merging would leave that bogus entry
        # beside the real one, so a generated client could parse a blob as JSON.
        # The handler streams the stored bytes back under the blob's own media
        # type, so an empty octet-stream schema is the honest description.
        "responses": {
            "200": {
                "description": "The stored evidence blob as a raw byte stream.",
                "content": {"application/octet-stream": {}},
            }
        },
    },
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


# Internal operational control plane. These routes are deliberately excluded
# from the public/pinned V1 OpenAPI contract and require a separate admin key.
@app.post(
    "/internal/admin/v1/bootstrap",
    response_model=AdminBootstrapResponse,
    include_in_schema=False,
)
async def admin_bootstrap(
    request: AdminBootstrapRequest,
    response: Response,
    _: None = Depends(require_admin_key),
    db: AsyncSession = Depends(session_scope),
) -> AdminBootstrapResponse:
    response.headers["Cache-Control"] = "no-store"
    return await bootstrap_identity(db, request)


@app.post(
    "/internal/admin/v1/sessions/{sessionID}/revoke",
    response_model=AdminSessionRevokeResponse,
    include_in_schema=False,
)
async def admin_revoke_session(
    sessionID: Annotated[str, Path(min_length=1, max_length=128)],
    request: AdminSessionRevokeRequest,
    _: None = Depends(require_admin_key),
    db: AsyncSession = Depends(session_scope),
) -> AdminSessionRevokeResponse:
    return await revoke_session(db, sessionID, request)


# The whole-request byte guard is registered last so it runs outermost of the user
# middleware, ahead of correlation and rate limiting.
#
# `add_middleware` rather than reassigning `app` on purpose: wrapping the ASGI
# application in a plain object hides `app.openapi()`, which the pinned-contract tests
# assert against. Registering it keeps `app` a FastAPI instance.
#
# It must be a pure ASGI middleware rather than `@app.middleware("http")`, because
# the decorator form builds a new downstream request and cannot intercept the body
# stream. See app/request_size.py.
app.add_middleware(RequestSizeLimitMiddleware)

# Registered after the byte guard, so instrumentation sits INSIDE it: a request
# refused for being oversized is still observed with its 413 status, which is exactly
# the signal an operator wants when diagnosing a client that streams too much. The
# route label is read from the matched template, so this adds no cardinality.
app.add_middleware(MetricsMiddleware)
