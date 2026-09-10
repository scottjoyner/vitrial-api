import json
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.observability import (
    JsonFormatter,
    begin_request,
    bind_principal,
    bind_request_metadata,
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

    tokens = begin_request("11111111-1111-4111-8111-111111111111")
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
        assert payload["requestID"] == "11111111-1111-4111-8111-111111111111"
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


def test_request_metadata_correlates_device_and_release_without_raw_device_id():
    request_id = "33333333-3333-4333-8333-333333333333"
    raw_device_id = "77777777-7777-4777-8777-777777777777"
    tokens = begin_request(request_id)
    try:
        bind_request_metadata(
            device_id=raw_device_id,
            app_version="0.2.0",
            app_build="121",
        )
        record = logging.LogRecord(
            name="vitrial",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="request.test",
            args=(),
            exc_info=None,
        )
        record.event_name = "request.test"
        record.event_fields = {}
        rendered = JsonFormatter().format(record)
        payload = json.loads(rendered)
        assert payload["requestID"] == request_id
        assert payload["deviceRef"] == correlation_ref(raw_device_id)
        assert payload["appVersion"] == "0.2.0"
        assert payload["appBuild"] == "121"
        assert raw_device_id not in rendered
    finally:
        end_request(tokens)


def test_request_metadata_rejects_free_form_release_strings():
    tokens = begin_request("44444444-4444-4444-8444-444444444444")
    try:
        bind_request_metadata(
            device_id=None,
            app_version="0.2.0 token=should-not-log",
            app_build="121/../../secret",
        )
        record = logging.LogRecord(
            name="vitrial",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="request.test",
            args=(),
            exc_info=None,
        )
        record.event_name = "request.test"
        record.event_fields = {}
        payload = json.loads(JsonFormatter().format(record))
        assert payload["deviceRef"] is None
        assert payload["appVersion"] is None
        assert payload["appBuild"] is None
    finally:
        end_request(tokens)


def test_request_id_validation_and_response_echo():
    valid = "22222222-2222-4222-8222-222222222222"
    assert request_id_for_header(valid) == valid
    generated = request_id_for_header("bad id with spaces")
    assert generated != "bad id with spaces"
    generated_from_arbitrary_safe_text = request_id_for_header("bearer-looking-but-not-a-uuid")
    assert generated_from_arbitrary_safe_text != "bearer-looking-but-not-a-uuid"

    response = TestClient(app).get(
        "/health",
        headers={
            "X-Request-ID": valid,
            "X-Vitrial-Device-ID": "77777777-7777-4777-8777-777777777777",
            "X-Vitrial-App-Version": "0.2.0",
            "X-Vitrial-App-Build": "121",
        },
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == valid


def test_internal_admin_routes_are_not_part_of_pinned_public_openapi():
    paths = app.openapi()["paths"]
    assert "/internal/admin/v1/bootstrap" not in paths
    assert "/internal/admin/v1/sessions/{sessionID}/revoke" not in paths
