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


# ── W3C Trace Context ─────────────────────────────────────────────────────────

from app.observability import trace_context_for_header  # noqa: E402

_VALID_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_VALID_PARENT = "00f067aa0ba902b7"


def _render() -> dict:
    record = logging.LogRecord(
        name="vitrial", level=logging.INFO, pathname=__file__, lineno=1,
        msg="t", args=(), exc_info=None,
    )
    record.event_name = "t"
    record.event_fields = {}
    return json.loads(JsonFormatter().format(record))


def test_a_valid_traceparent_joins_the_callers_trace():
    trace = trace_context_for_header(f"00-{_VALID_TRACE}-{_VALID_PARENT}-01")
    assert trace.trace_id == _VALID_TRACE, "the caller's trace id must be kept"
    assert trace.parent_span_id == _VALID_PARENT
    assert trace.sampled is True
    assert len(trace.span_id) == 16 and trace.span_id != _VALID_PARENT, (
        "this request gets its own span, distinct from the caller's"
    )


def test_unsampled_flag_is_preserved():
    assert trace_context_for_header(f"00-{_VALID_TRACE}-{_VALID_PARENT}-00").sampled is False


def test_invalid_traceparents_start_a_new_trace_and_are_never_reflected():
    """Strict, like request_id_for_header: a header is caller-controlled text, and the log
    line is where a credential pasted into the wrong header would otherwise land."""
    hostile = [
        None,
        "",
        "garbage",
        f"01-{_VALID_TRACE}-{_VALID_PARENT}-01",          # unknown version
        f"ff-{_VALID_TRACE}-{_VALID_PARENT}-01",          # version ff is invalid per spec
        f"00-{_VALID_TRACE.upper()}-{_VALID_PARENT}-01",  # uppercase hex
        f"00-{'0' * 32}-{_VALID_PARENT}-01",              # all-zero trace id
        f"00-{_VALID_TRACE}-{'0' * 16}-01",               # all-zero parent id
        f"00-{_VALID_TRACE}-{_VALID_PARENT}-01-extra",    # trailing fields
        "Bearer eyJhbGciOiJIUzI1NiJ9.secret-token-pasted-into-the-wrong-header",
    ]
    for value in hostile:
        trace = trace_context_for_header(value)
        assert trace.parent_span_id is None, value
        assert len(trace.trace_id) == 32 and trace.trace_id != _VALID_TRACE, value
        if value:
            assert trace.trace_id not in value, "nothing from the header may be reflected"


def test_trace_ids_reach_every_log_line_and_do_not_leak_past_the_request():
    trace = trace_context_for_header(f"00-{_VALID_TRACE}-{_VALID_PARENT}-01")
    tokens = begin_request("55555555-5555-4555-8555-555555555555", trace)
    try:
        payload = _render()
        assert payload["traceID"] == _VALID_TRACE
        assert payload["parentSpanID"] == _VALID_PARENT
        assert payload["spanID"] == trace.span_id
    finally:
        end_request(tokens)
    # The context vars are reset, so a later log line outside any request is not
    # mis-attributed to this trace.
    after = _render()
    assert after["traceID"] is None and after["spanID"] is None


def test_traceparent_flows_end_to_end_into_request_logs():
    """Through the real middleware: the caller's trace id appears on the request's logs.

    The handler formats AT EMIT TIME, deliberately. The trace ids live in context vars that
    the middleware resets when the request ends, so a record captured and formatted
    afterwards (e.g. by caplog) would read traceID=None and prove nothing. The `vitrial`
    logger also does not propagate, so caplog would not see it at all.
    """
    from app.observability import logger as vitrial_logger

    rendered: list[dict] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            rendered.append(json.loads(JsonFormatter().format(record)))

    handler = _Capture()
    vitrial_logger.addHandler(handler)
    previous_level = vitrial_logger.level
    vitrial_logger.setLevel(logging.INFO)
    try:
        with TestClient(app) as client:
            response = client.get(
                "/health", headers={"traceparent": f"00-{_VALID_TRACE}-{_VALID_PARENT}-01"}
            )
    finally:
        vitrial_logger.removeHandler(handler)
        vitrial_logger.setLevel(previous_level)

    assert response.status_code == 200
    completed = [p for p in rendered if p.get("event") == "request.completed"]
    assert completed, "request.completed was not logged"
    assert completed[-1]["traceID"] == _VALID_TRACE
    assert completed[-1]["parentSpanID"] == _VALID_PARENT
    assert completed[-1]["spanID"] and completed[-1]["spanID"] != _VALID_PARENT
