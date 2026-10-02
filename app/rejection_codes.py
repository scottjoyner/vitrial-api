"""Stable rejection codes for the sync outcome stream.

`VITR-V006`: the sync outcome previously logged `reason=type(exc).__name__`, so every
authorization, lifecycle, and delivery rejection collapsed to one of four Python class
names. That is not aggregatable and is not stable across a refactor.

This module defines a small, closed vocabulary of codes and a single classifier. The
wire contract is unchanged: `reason` is still a string on the `sync.mutation_result`
event. Only its vocabulary is now fixed.

Codes are additive. A new distinct decision gets a new code; a refactor that changes an
exception's *class* must not change the code a client or a metric already keys on.
"""

from __future__ import annotations

from typing import Literal

RejectionCode = Literal[
    # transport / framing
    "invalid_mutation",
    "missing_client_mutation_id",
    "mutation_id_collision",
    "stale_revision",
    "idempotent_replay",
    # authority
    "authorization_rejected",
    "capability_missing",
    "scope_violation",
    "ownership_immutable",
    "ownership_missing",
    "delete_blocked",
    # lifecycle / state machine
    "lifecycle_rejected",
    "delivery_rejected",
    "history_tampering",
    # outcome
    "committed",
    "internal_error",
]


def rejection_code(exc: BaseException) -> str:
    """Map a rejection exception to its stable code.

    Falls back to the exception's own ``rejection_code`` attribute when the raising
    site supplied one, then to a per-class default, then to ``internal_error`` so an
    unmapped exception is visibly unmapped instead of silently reusing a real code.
    """
    explicit = getattr(exc, "rejection_code", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return _DEFAULT_BY_CLASS.get(type(exc).__name__, "internal_error")


# Keep this table tiny and stable. Per-site precision is opt-in via an explicit
# ``rejection_code`` on the raised exception; the table is only the safety net.
_DEFAULT_BY_CLASS: dict[str, str] = {
    "InvalidMutation": "invalid_mutation",
    "AuthorizationRejected": "authorization_rejected",
    "LifecycleRejected": "lifecycle_rejected",
    "DeliveryExecutionRejected": "delivery_rejected",
    "DeliveryInstallationRejected": "delivery_rejected",
    "DeliveryProductionRejected": "delivery_rejected",
}