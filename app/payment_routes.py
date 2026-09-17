from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal
from app.db import session_scope
from app.models import CanonicalProject, SyncEntity, uid
from app.ownership import EffectiveScope
from app.payment_models import Payment, PaymentAttempt


PAYMENT_READ_CAPABILITY = "payment.read"
PAYMENT_RECORD_CAPABILITY = "payment.record"
PAYMENT_VOID_CAPABILITY = "payment.void"
PAYMENT_REFUND_CAPABILITY = "payment.refund"

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


class PaymentPrepareRequest(BaseModel):
    """Prepare a backend-owned settlement request without collecting payment credentials."""

    model_config = ConfigDict(extra="forbid")

    projectID: str = Field(min_length=1, max_length=256)
    quotationID: str = Field(min_length=1, max_length=256)
    quotationRevision: int = Field(ge=1)
    amount: Decimal = Field(gt=0, max_digits=19, decimal_places=4)
    currencyCode: str = Field(min_length=3, max_length=3)

    @field_validator("projectID", "quotationID")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("identity must be non-empty")
        return value

    @field_validator("currencyCode")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currencyCode must be a three-letter code")
        return value


class PaymentAttemptView(BaseModel):
    id: str
    operation: Literal["prepare"]
    status: Literal["blocked"]
    provider: Literal["unconfigured"]
    failureCode: Literal["processor_unconfigured"]


class PaymentView(BaseModel):
    id: str
    projectID: str
    quotationID: str
    quotationRevision: int
    amount: Decimal
    currencyCode: str
    status: Literal["pending"]
    fundsMoved: Literal[False]
    processorConfigured: Literal[False]
    attempt: PaymentAttemptView


class PaymentListResponse(BaseModel):
    payments: list[PaymentView]


class PaymentContractRejected(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require_capability(principal: Principal, capability: str) -> None:
    if capability not in principal.capabilities:
        raise PaymentContractRejected(f"{capability} capability required", status_code=403)


def _decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PaymentContractRejected(f"{label} is invalid")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaymentContractRejected(f"{label} is invalid") from exc


def _quotation_revision(payload: dict) -> int:
    revision = payload.get("revision", 1)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise PaymentContractRejected("canonical quotation revision is invalid")
    return revision


def _quotation_total(payload: dict) -> Decimal:
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        raise PaymentContractRejected("canonical quotation lines are unavailable")
    total = Decimal(0)
    for line in lines:
        if not isinstance(line, dict):
            raise PaymentContractRejected("canonical quotation line is malformed")
        total += _decimal(line.get("totalPrice"), label="canonical quotation totalPrice")
    return total


def _request_fingerprint(request: PaymentPrepareRequest) -> str:
    normalized = {
        "projectID": request.projectID,
        "quotationID": request.quotationID,
        "quotationRevision": request.quotationRevision,
        "amount": format(request.amount, "f"),
        "currencyCode": request.currencyCode,
    }
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _canonical_quote(
    db: AsyncSession,
    principal: Principal,
    *,
    project_id: str,
    quotation_id: str,
) -> dict:
    project = await db.get(CanonicalProject, (principal.organization_id, project_id))
    if project is None or project.deleted_at is not None:
        raise PaymentContractRejected("payment Project is not canonical and active", status_code=404)
    scope = EffectiveScope.from_principal(principal)
    if not scope.can_access_project(project.project_id, project.customer_id):
        raise PaymentContractRejected("payment Project is outside authorized scope", status_code=403)

    quotation = await db.get(
        SyncEntity,
        (principal.organization_id, "quotation", quotation_id),
    )
    if quotation is None or quotation.deleted_at is not None or not isinstance(quotation.payload_json, dict):
        raise PaymentContractRejected("payment requires a canonical quotation", status_code=404)
    quote = quotation.payload_json
    if quote.get("status") != "approved":
        raise PaymentContractRejected("payment requires an approved quotation")
    if quote.get("projectID") != project_id or quote.get("customerID") != project.customer_id:
        raise PaymentContractRejected("payment quotation ownership does not match Project")
    events = quote.get("events") or []
    if not isinstance(events, list) or not any(
        isinstance(event, dict) and event.get("kind") == "approved" for event in events
    ):
        raise PaymentContractRejected("payment quotation is missing immutable approval history")
    return quote


async def _attempt_for(db: AsyncSession, principal: Principal, payment_id: str) -> PaymentAttempt:
    attempt = await db.scalar(
        select(PaymentAttempt)
        .where(
            PaymentAttempt.organization_id == principal.organization_id,
            PaymentAttempt.payment_id == payment_id,
        )
        .order_by(PaymentAttempt.created_at.desc(), PaymentAttempt.id.desc())
        .limit(1)
    )
    if attempt is None:
        raise PaymentContractRejected("payment attempt audit record is unavailable", status_code=500)
    return attempt


def _view(payment: Payment, attempt: PaymentAttempt) -> PaymentView:
    # This contract slice is deliberately fail-closed. No route can produce any other
    # state until a processor-backed state machine is implemented and reviewed.
    if payment.status != "pending" or attempt.status != "blocked" or attempt.provider != "unconfigured":
        raise PaymentContractRejected("unsupported payment state in settlement contract", status_code=500)
    return PaymentView(
        id=payment.id,
        projectID=payment.project_id,
        quotationID=payment.quotation_id,
        quotationRevision=payment.quotation_revision,
        amount=payment.amount,
        currencyCode=payment.currency_code,
        status="pending",
        fundsMoved=False,
        processorConfigured=False,
        attempt=PaymentAttemptView(
            id=attempt.id,
            operation="prepare",
            status="blocked",
            provider="unconfigured",
            failureCode="processor_unconfigured",
        ),
    )


async def _existing_for_idempotency(
    db: AsyncSession,
    principal: Principal,
    idempotency_key: str,
) -> Payment | None:
    return await db.scalar(
        select(Payment).where(
            Payment.organization_id == principal.organization_id,
            Payment.idempotency_key == idempotency_key,
        )
    )


@router.post(
    "/prepare",
    response_model=PaymentView,
    operation_id="paymentPrepare",
    status_code=201,
)
async def prepare_payment(
    request: PaymentPrepareRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> PaymentView:
    """Create a pending settlement request and a blocked attempt.

    This endpoint does not collect payment credentials, contact a processor, or claim
    that funds moved. It exists so clients can integrate the authoritative commercial
    binding and idempotency contract before a processor is selected.
    """

    try:
        _require_capability(principal, PAYMENT_RECORD_CAPABILITY)
        idempotency_key = idempotency_key.strip()
        if not idempotency_key:
            raise PaymentContractRejected("Idempotency-Key must be non-empty")

        quote = await _canonical_quote(
            db,
            principal,
            project_id=request.projectID,
            quotation_id=request.quotationID,
        )
        canonical_revision = _quotation_revision(quote)
        if request.quotationRevision != canonical_revision:
            raise PaymentContractRejected("payment quotation revision does not match canonical quotation")
        canonical_currency = str(quote.get("currencyCode") or "").strip().upper()
        if request.currencyCode != canonical_currency:
            raise PaymentContractRejected("payment currency does not match canonical quotation")

        fingerprint = _request_fingerprint(request)
        existing = await _existing_for_idempotency(db, principal, idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise PaymentContractRejected("Idempotency-Key was already used for a different payment request", status_code=409)
            return _view(existing, await _attempt_for(db, principal, existing.id))

        quote_total = _quotation_total(quote)
        reserved = await db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.organization_id == principal.organization_id,
                Payment.quotation_id == request.quotationID,
                Payment.quotation_revision == canonical_revision,
                Payment.status.in_(("pending", "settled", "partially_refunded")),
            )
        )
        remaining = quote_total - Decimal(str(reserved or 0))
        if request.amount > remaining:
            raise PaymentContractRejected("payment amount exceeds unreserved quotation balance")

        payment = Payment(
            id=uid(),
            organization_id=principal.organization_id,
            project_id=request.projectID,
            quotation_id=request.quotationID,
            quotation_revision=canonical_revision,
            amount=request.amount,
            currency_code=canonical_currency,
            status="pending",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            created_by_user_id=principal.user_id,
            membership_id=principal.membership_id,
            session_id=principal.session_id,
            authorization_revision=principal.authorization_revision,
        )
        attempt = PaymentAttempt(
            id=uid(),
            organization_id=principal.organization_id,
            payment_id=payment.id,
            operation="prepare",
            status="blocked",
            provider="unconfigured",
            amount=request.amount,
            currency_code=canonical_currency,
            failure_code="processor_unconfigured",
            created_by_user_id=principal.user_id,
            membership_id=principal.membership_id,
            session_id=principal.session_id,
            authorization_revision=principal.authorization_revision,
        )
        db.add(payment)
        db.add(attempt)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await _existing_for_idempotency(db, principal, idempotency_key)
            if existing is None or existing.request_fingerprint != fingerprint:
                raise PaymentContractRejected("Idempotency-Key collision", status_code=409)
            return _view(existing, await _attempt_for(db, principal, existing.id))
        return _view(payment, attempt)
    except PaymentContractRejected as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get(
    "",
    response_model=PaymentListResponse,
    operation_id="paymentList",
)
async def list_payments(
    projectID: str = Query(min_length=1, max_length=256),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> PaymentListResponse:
    try:
        _require_capability(principal, PAYMENT_READ_CAPABILITY)
        project = await db.get(CanonicalProject, (principal.organization_id, projectID))
        if project is None or project.deleted_at is not None:
            raise PaymentContractRejected("payment Project is not canonical and active", status_code=404)
        if not EffectiveScope.from_principal(principal).can_access_project(project.project_id, project.customer_id):
            raise PaymentContractRejected("payment Project is outside authorized scope", status_code=403)

        payments = list((await db.scalars(
            select(Payment)
            .where(
                Payment.organization_id == principal.organization_id,
                Payment.project_id == projectID,
            )
            .order_by(Payment.created_at.desc(), Payment.id.desc())
        )).all())
        views = [_view(payment, await _attempt_for(db, principal, payment.id)) for payment in payments]
        return PaymentListResponse(payments=views)
    except PaymentContractRejected as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
