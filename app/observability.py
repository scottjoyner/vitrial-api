from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
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


def begin_request(request_id: str) -> RequestContextTokens:
    return RequestContextTokens(
        request_id=_request_id.set(request_id),
        organization_ref=_organization_ref.set(None),
        actor_ref=_actor_ref.set(None),
        membership_ref=_membership_ref.set(None),
        session_ref=_session_ref.set(None),
        device_ref=_device_ref.set(None),
        app_version=_app_version.set(None),
        app_build=_app_build.set(None),
    )


def end_request(tokens: RequestContextTokens) -> None:
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
