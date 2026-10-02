"""The sync outcome stream must carry stable codes, not exception class names (VITR-V006)."""

from __future__ import annotations

import pytest

from app.delivery_execution import DeliveryExecutionRejected
from app.lifecycle import LifecycleRejected
from app.ownership import AuthorizationRejected
from app.rejection_codes import rejection_code
from app.sync_service import InvalidMutation


def test_rejection_codes_are_not_python_class_names():
    for exc in (
        InvalidMutation("x"),
        AuthorizationRejected("x"),
        LifecycleRejected("x"),
        DeliveryExecutionRejected("x"),
    ):
        assert rejection_code(exc) != type(exc).__name__


def test_classifier_is_stable_across_distinct_messages():
    """Two different rejections of the same class must not produce two codes.

    The vocabulary is coarse on purpose; per-site precision is opt-in via an explicit
    `rejection_code` attribute. This test pins that the safety net does not drift with
    the message text, which is what made the old `type(exc).__name__` unusable.
    """
    a = AuthorizationRejected("Customer is outside authorized scope")
    b = AuthorizationRejected("payload identity does not match canonical entityID")
    assert rejection_code(a) == rejection_code(b) == "authorization_rejected"


def test_explicit_code_on_the_exception_wins():
    exc = AuthorizationRejected("Evidence cannot be deleted while referenced")
    exc.rejection_code = "delete_blocked"
    assert rejection_code(exc) == "delete_blocked"


def test_unmapped_exception_is_visibly_unmapped_not_silently_bucketized():
    assert rejection_code(RuntimeError("boom")) == "internal_error"