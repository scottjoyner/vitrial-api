from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import secrets
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_SENSITIVE_KEYS = {
    "authorization",
    "proxyauthorization",
    "bearer",
    "cookie",
    "setcookie",
    "credential",
    "credentials",
    "password",
    "secret",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "xvitrialadminkey",
}

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_request_id", default=None
)
_organization_ref: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_organization_ref", default=None
)
_actor_ref: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_actor_ref", default=None
)
_membership_ref: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_membership_ref", default=None
)
_session_ref: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_session_ref", default=None
)
_device_ref: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_device_ref", default=None
)
_app_version: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_app_version", default=None
)
_app_build: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_app_build", default=None
)
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_trace_id", default=None
)
_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_span_id", default=None
)
_parent_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrial_parent_span_id", default=None
)


@dataclass(frozen=True)
class RequestContextTokens:
    request_id: contextvars.Token
    organization_ref: contextvars.Token
    actor_ref: contextvars.Token
    membership_ref: contextvars.Token
    session_ref: contextvars.Token
    device_ref: contextvars.Token
    app_version: contextvars.Token
    app_build: contextvars.Token
    trace_id: contextvars.Token | None = None
    span_id: contextvars.Token | None = None
    parent_span_id: contextvars.Token | None = None


def correlation_ref(value: str | None) -> str | None:
    """Return a stable, non-reversible correlation reference for an identifier."""
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def request_id_for_header(value: str | None) -> str:
    # Only accept canonical UUID correlation values from callers. This prevents
    # arbitrary header contents (including accidentally copied credentials)
    # from being reflected into logs under the request-ID field.
    if value:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError):
            pass
    return str(uuid.uuid4())


@dataclass(frozen=True)
class TraceContext:
    """W3C Trace Context identity for one request.

    `trace_id` is shared with the caller when it sent a valid `traceparent`, so an iOS
    request and this service's log lines carry ONE trace id -- which X-Request-ID alone
    could not give: it is this service's own correlation value, not a cross-system one.
    `parent_span_id` is the caller's span; `span_id` is minted here for this request.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    sampled: bool


_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")


def trace_context_for_header(value: str | None) -> TraceContext:
    """Parse a W3C `traceparent`, or start a new trace.

    Strict on purpose, matching `request_id_for_header`: only a well-formed version-00
    header is honoured. Anything else -- wrong version, uppercase hex, the all-zero ids the
    spec declares invalid, extra fields -- starts a fresh trace instead of being reflected
    into every log line. A header is caller-controlled text, and the log is where a
    credential copied into the wrong header would otherwise land.
    """
    span_id = secrets.token_hex(8)
    if value:
        match = _TRACEPARENT.fullmatch(value.strip())
        if match:
            trace_id, parent_id, flags = match.groups()
            if trace_id != "0" * 32 and parent_id != "0" * 16:
                return TraceContext(
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent_id,
                    sampled=bool(int(flags, 16) & 0x01),
                )
    return TraceContext(
        trace_id=secrets.token_hex(16),
        span_id=span_id,
        parent_span_id=None,
        sampled=False,
    )


def _release_value(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        return None
    # Version/build are operational labels, never arbitrary free-form log fields.
    if re.fullmatch(r"[A-Za-z0-9._+\-]+", normalized) is None:
        return None
    return normalized


def begin_request(request_id: str, trace: TraceContext | None = None) -> RequestContextTokens:
    trace = trace or trace_context_for_header(None)
    return RequestContextTokens(
        request_id=_request_id.set(request_id),
        organization_ref=_organization_ref.set(None),
        actor_ref=_actor_ref.set(None),
        membership_ref=_membership_ref.set(None),
        session_ref=_session_ref.set(None),
        device_ref=_device_ref.set(None),
        app_version=_app_version.set(None),
        app_build=_app_build.set(None),
        trace_id=_trace_id.set(trace.trace_id),
        span_id=_span_id.set(trace.span_id),
        parent_span_id=_parent_span_id.set(trace.parent_span_id),
    )


def end_request(tokens: RequestContextTokens) -> None:
    if tokens.parent_span_id is not None:
        _parent_span_id.reset(tokens.parent_span_id)
    if tokens.span_id is not None:
        _span_id.reset(tokens.span_id)
    if tokens.trace_id is not None:
        _trace_id.reset(tokens.trace_id)
    _app_build.reset(tokens.app_build)
    _app_version.reset(tokens.app_version)
    _device_ref.reset(tokens.device_ref)
    _session_ref.reset(tokens.session_ref)
    _membership_ref.reset(tokens.membership_ref)
    _actor_ref.reset(tokens.actor_ref)
    _organization_ref.reset(tokens.organization_ref)
    _request_id.reset(tokens.request_id)


def bind_request_metadata(
    *,
    device_id: str | None,
    app_version: str | None,
    app_build: str | None,
) -> None:
    _device_ref.set(correlation_ref(device_id.strip()) if device_id and device_id.strip() else None)
    _app_version.set(_release_value(app_version))
    _app_build.set(_release_value(app_build))


def bind_principal(principal: Any) -> None:
    _organization_ref.set(correlation_ref(principal.organization_id))
    _actor_ref.set(correlation_ref(principal.user_id))
    _membership_ref.set(correlation_ref(principal.membership_id))
    _session_ref.set(correlation_ref(principal.session_id))


def _sensitive_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return (
        compact in _SENSITIVE_KEYS
        or compact.endswith("token")
        or compact.endswith("password")
        or compact.endswith("secret")
    )


def redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _sensitive_key(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event_name", record.getMessage())
        fields = redact(getattr(record, "event_fields", {}))
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "event": event,
            "requestID": _request_id.get(),
            "traceID": _trace_id.get(),
            "spanID": _span_id.get(),
            "parentSpanID": _parent_span_id.get(),
            "organizationRef": _organization_ref.get(),
            "actorRef": _actor_ref.get(),
            "membershipRef": _membership_ref.get(),
            "sessionRef": _session_ref.get(),
            "deviceRef": _device_ref.get(),
            "appVersion": _app_version.get(),
            "appBuild": _app_build.get(),
        }
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exceptionType"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


logger = logging.getLogger("vitrial")


def configure_logging() -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if any(getattr(handler, "_vitrial_json", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._vitrial_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(
        level,
        event,
        extra={"event_name": event, "event_fields": fields},
    )


configure_logging()
