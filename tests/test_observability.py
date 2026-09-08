import json
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.observability import (
    JsonFormatter,
    begin_request,
    bind_principal,
    correlation_ref,
    end_request,
    redact,
    request_id_for_header,
)


def test_correlation_refs_are_stable_and_non_reversible():
    raw = "org-sensitive-identifier"
    ref = correlation_ref(raw)
    assert ref == correlation_ref(raw)
    assert ref is not None
    assert ref.startswith("sha256:")
    assert raw not in ref


def test_redaction_blocks_credentials_without_hiding_authorization_revision():
    value = redact({
        "Authorization": "Bearer super-secret-access",
        "accessToken": "super-secret-access",
        "refresh_token": "refresh-secret",
        "cookie": "session=secret",
        "password": "password-secret",
        "authorizationRevision": 17,
        "nested": {"X-Vitrial-Admin-Key": "admin-secret", "safe": "visible"},
    })
    assert value["Authorization"] == "<redacted>"
    assert value["accessToken"] == "<redacted>"
    assert value["refresh_token"] == "<redacted>"
    assert value["cookie"] == "<redacted>"
    assert value["password"] == "<redacted>"
    assert value["authorizationRevision"] == 17
    assert value["nested"]["X-Vitrial-Admin-Key"] == "<redacted>"
    assert value["nested"]["safe"] == "visible"


def test_json_formatter_correlates_hashed_principal_and_never_emits_raw_credentials():
    raw_org = "org-raw"
    raw_user = "user-raw"
    raw_membership = "membership-raw"
    raw_session = "session-raw"
    raw_token = "bearer-token-must-never-appear"
    raw_admin_key = "admin-key-must-never-appear"

    tokens = begin_request("request-12345678")
    try:
        bind_principal(SimpleNamespace(
            organization_id=raw_org,
            user_id=raw_user,
            membership_id=raw_membership,
            session_id=raw_session,
        ))
        record = logging.LogRecord(
            name="vitrial",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="test.event",
            args=(),
            exc_info=None,
        )
        record.event_name = "test.event"
        record.event_fields = {
            "authorizationRevision": 3,
            "Authorization": f"Bearer {raw_token}",
            "accessToken": raw_token,
            "X-Vitrial-Admin-Key": raw_admin_key,
        }
        rendered = JsonFormatter().format(record)
        payload = json.loads(rendered)
        assert payload["requestID"] == "request-12345678"
        assert payload["organizationRef"] == correlation_ref(raw_org)
        assert payload["actorRef"] == correlation_ref(raw_user)
        assert payload["membershipRef"] == correlation_ref(raw_membership)
        assert payload["sessionRef"] == correlation_ref(raw_session)
        assert payload["authorizationRevision"] == 3
        assert raw_org not in rendered
        assert raw_user not in rendered
        assert raw_membership not in rendered
        assert raw_session not in rendered
        assert raw_token not in rendered
        assert raw_admin_key not in rendered
    finally:
        end_request(tokens)


def test_request_id_validation_and_response_echo():
    valid = "client-request-1234"
    assert request_id_for_header(valid) == valid
    generated = request_id_for_header("bad id with spaces")
    assert generated != "bad id with spaces"

    response = TestClient(app).get("/health", headers={"X-Request-ID": valid})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == valid


def test_internal_admin_routes_are_not_part_of_pinned_public_openapi():
    paths = app.openapi()["paths"]
    assert "/internal/admin/v1/bootstrap" not in paths
    assert "/internal/admin/v1/sessions/{sessionID}/revoke" not in paths
