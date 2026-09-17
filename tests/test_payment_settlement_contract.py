from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.payment_models import Payment, PaymentAttempt
from app.payment_routes import (
    PAYMENT_READ_CAPABILITY,
    PAYMENT_RECORD_CAPABILITY,
    PAYMENT_REFUND_CAPABILITY,
    PAYMENT_VOID_CAPABILITY,
    PaymentPrepareRequest,
    _request_fingerprint,
    _view,
)


def request(**overrides) -> PaymentPrepareRequest:
    payload = {
        "projectID": "project-1",
        "quotationID": "quotation-1",
        "quotationRevision": 2,
        "amount": "125.50",
        "currencyCode": "cop",
    }
    payload.update(overrides)
    return PaymentPrepareRequest.model_validate(payload)


def test_prepare_request_normalizes_currency_and_rejects_payment_credentials():
    parsed = request()
    assert parsed.currencyCode == "COP"
    assert parsed.amount == Decimal("125.50")

    with pytest.raises(ValidationError):
        PaymentPrepareRequest.model_validate({
            **parsed.model_dump(),
            "cardNumber": "4111111111111111",
        })
    with pytest.raises(ValidationError):
        PaymentPrepareRequest.model_validate({
            **parsed.model_dump(),
            "processorToken": "tok_should_not_be_accepted",
        })
    with pytest.raises(ValidationError):
        request(amount="0")


def test_idempotency_fingerprint_is_stable_for_normalized_request():
    first = request(currencyCode="cop", amount="125.5000")
    second = request(currencyCode="COP", amount=Decimal("125.5000"))
    assert _request_fingerprint(first) == _request_fingerprint(second)

    changed = request(amount="125.5001")
    assert _request_fingerprint(first) != _request_fingerprint(changed)


def test_scaffold_view_cannot_claim_funds_moved():
    payment = Payment(
        id="payment-1",
        organization_id="org-1",
        project_id="project-1",
        quotation_id="quotation-1",
        quotation_revision=2,
        amount=Decimal("125.50"),
        currency_code="COP",
        status="pending",
        idempotency_key="idem-1",
        request_fingerprint="f" * 64,
        created_by_user_id="user-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=7,
    )
    attempt = PaymentAttempt(
        id="attempt-1",
        organization_id="org-1",
        payment_id="payment-1",
        operation="prepare",
        status="blocked",
        provider="unconfigured",
        amount=Decimal("125.50"),
        currency_code="COP",
        failure_code="processor_unconfigured",
        created_by_user_id="user-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=7,
    )

    view = _view(payment, attempt)
    assert view.status == "pending"
    assert view.fundsMoved is False
    assert view.processorConfigured is False
    assert view.attempt.status == "blocked"
    assert view.attempt.provider == "unconfigured"
    assert view.attempt.failureCode == "processor_unconfigured"


def test_payment_capabilities_are_explicit_and_separate():
    assert {
        PAYMENT_READ_CAPABILITY,
        PAYMENT_RECORD_CAPABILITY,
        PAYMENT_VOID_CAPABILITY,
        PAYMENT_REFUND_CAPABILITY,
    } == {
        "payment.read",
        "payment.record",
        "payment.void",
        "payment.refund",
    }
