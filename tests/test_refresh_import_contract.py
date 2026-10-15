"""Import and schema smoke tests for the opt-in research refresh endpoint.

These are source-level/ASGI contract checks only, not real session rotation,
database migration, or approval to enable refresh-token delivery in production.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError


def test_auth_refresh_models_import_and_validate_strict_wire():
    from app.auth import RefreshTokenRequest, RefreshTokenResponse

    request = RefreshTokenRequest.model_validate({"refreshToken": "x" * 32})
    assert request.refreshToken == "x" * 32

    with pytest.raises(ValidationError):
        RefreshTokenRequest.model_validate({"refreshToken": "short"})
    with pytest.raises(ValidationError):
        RefreshTokenRequest.model_validate({
            "refreshToken": "x" * 32,
            "unsafePrivilege": True,
        })

    reply = RefreshTokenResponse(
        accessToken="synthetic-test-only",
        expiresAt=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert reply.accessToken == "synthetic-test-only"


def test_incomplete_refresh_path_is_not_exposed_to_real_devices():
    from app.main import app

    # The research model compiles but no refresh route is mounted by the
    # production API. Rotation without a complete secure client handoff,
    # next refresh token and idempotency semantics is not release-ready.
    assert "/api/v1/auth/refresh" not in app.openapi()["paths"]
