from __future__ import annotations

import base64
import importlib.util
import json
import stat
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_two_user_acceptance.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_two_user_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_base_url_requires_https_except_explicit_localhost():
    assert module._validated_base_url("https://api.example.test/", allow_http_localhost=False) == "https://api.example.test"
    with pytest.raises(ValueError):
        module._validated_base_url("http://api.example.test", allow_http_localhost=True)
    assert module._validated_base_url("http://127.0.0.1:8000", allow_http_localhost=True) == "http://127.0.0.1:8000"


def test_private_write_json_is_mode_0600(tmp_path: Path):
    output = tmp_path / "handoff.json"
    module._private_write_json(output, {"accessToken": "secret-token"})
    assert json.loads(output.read_text()) == {"accessToken": "secret-token"}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_profile_scope_verification_is_fail_closed():
    profile = {
        "organizationID": "org-1",
        "principalID": "user-1",
        "projectIDs": ["project-1"],
        "allProjects": False,
        "capabilities": ["sync", "project.create", "item.create", "item.edit"],
    }
    assert module._profile_matches(profile, organization_id="org-1", user_id="user-1", project_id="project-1")
    assert not module._profile_matches(profile, organization_id="org-1", user_id="user-2", project_id="project-1")
    assert not module._profile_matches({**profile, "allProjects": True}, organization_id="org-1", user_id="user-1", project_id="project-1")
    assert not module._profile_matches({**profile, "capabilities": ["sync", "item.create"]}, organization_id="org-1", user_id="user-1", project_id="project-1")


def test_sync_record_encodes_typed_payload():
    record = module._sync_record(
        record_id="record-1",
        entity_type="project",
        entity_id="project-1",
        payload={"id": "project-1", "customerID": "customer-1", "name": "Project"},
        mutation_id="mutation-1",
        updated_at="2026-09-08T00:00:00+00:00",
    )
    assert record["clientMutationID"] == "mutation-1"
    decoded = json.loads(base64.b64decode(record["payload"]))
    assert decoded["id"] == "project-1"
    assert decoded["customerID"] == "customer-1"


def test_seed_shared_project_requires_both_records_accepted(monkeypatch):
    observed = {}

    def fake_request(method, url, *, headers=None, payload=None, timeout=20.0):
        observed.update({"method": method, "url": url, "headers": headers, "payload": payload})
        return {
            "acceptedRecordIDs": ["record-customer-fixed", "record-project-fixed"],
            "rejectedRecordIDs": [],
            "nextCursor": "seq:2",
        }

    monkeypatch.setattr(module, "_request_json", fake_request)
    result = module._seed_shared_project(
        base_url="https://vitrial.example.test",
        token="token-secret",
        run_id="fixed",
        customer_id="customer-fixed",
        project_id="project-fixed",
    )
    assert result["nextCursor"] == "seq:2"
    assert observed["headers"]["Authorization"] == "Bearer token-secret"
    records = observed["payload"]["records"]
    assert {record["entityType"] for record in records} == {"customer", "project"}
    assert {record["entityID"] for record in records} == {"customer-fixed", "project-fixed"}


def test_main_bootstraps_seeds_and_cross_user_verifies_without_printing_tokens(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("VITRIAL_ADMIN_KEY", "admin-secret-never-print")
    output = tmp_path / "two-users.json"
    calls: list[tuple[str, str, dict | None, dict | None]] = []
    token_by_user = {
        "user-a-acceptance-fixed": "token-a-secret",
        "user-b-acceptance-fixed": "token-b-secret",
    }

    def fake_request(method, url, *, headers=None, payload=None, timeout=20.0):
        calls.append((method, url, headers, payload))
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/api/v1/version"):
            return {"apiVersion": "v1", "serviceVersion": "0.2.0-test"}
        if url.endswith("/internal/admin/v1/bootstrap"):
            user_id = payload["userID"]
            return {
                "organizationID": payload["organizationID"],
                "userID": user_id,
                "membershipID": payload["membershipID"],
                "sessionID": payload["sessionID"],
                "authorizationRevision": 1 if user_id.startswith("user-a-") else 2,
                "issuedAt": "2026-09-08T00:00:00Z",
                "expiresAt": "2026-09-09T00:00:00Z",
                "accessToken": token_by_user[user_id],
            }
        if url.endswith("/api/v1/sync/push"):
            assert headers["Authorization"] == "Bearer token-a-secret"
            record_ids = {record["id"] for record in payload["records"]}
            assert record_ids == {"record-customer-fixed", "record-project-fixed"}
            return {
                "acceptedRecordIDs": ["record-customer-fixed", "record-project-fixed"],
                "rejectedRecordIDs": [],
                "nextCursor": "seq:2",
            }
        if "/api/v1/sync/pull?" in url:
            assert headers["Authorization"] == "Bearer token-b-secret"
            return {
                "deviceID": "server",
                "cursor": "seq:2",
                "records": [
                    {"id": "pulled-customer", "entityID": "customer-acceptance-fixed"},
                    {"id": "pulled-project", "entityID": "project-acceptance-fixed"},
                ],
            }
        if url.endswith("/api/v1/auth/me"):
            token = headers["Authorization"].removeprefix("Bearer ")
            user_id = next(user for user, candidate in token_by_user.items() if candidate == token)
            return {
                "organizationID": "org-acceptance-fixed",
                "principalID": user_id,
                "membershipID": user_id.replace("user-", "membership-"),
                "sessionID": user_id.replace("user-", "session-"),
                "authorizationRevision": 2,
                "expiresAt": "2026-09-09T00:00:00Z",
                "projectIDs": ["project-acceptance-fixed"],
                "allProjects": False,
                "capabilities": list(module.DEFAULT_CAPABILITIES),
            }
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(module, "_request_json", fake_request)
    result = module.main(
        [
            "--base-url",
            "https://vitrial.example.test",
            "--output",
            str(output),
            "--run-id",
            "fixed",
        ]
    )
    assert result == 0
    handoff = json.loads(output.read_text())
    assert handoff["format"] == "vitrial.two-user-acceptance.v2"
    assert handoff["organizationID"] == "org-acceptance-fixed"
    assert handoff["customerID"] == "customer-acceptance-fixed"
    assert handoff["projectID"] == "project-acceptance-fixed"
    assert handoff["seedCursor"] == "seq:2"
    assert handoff["sharedProjectVerifiedFromUserB"] is True
    assert [entry["accessToken"] for entry in handoff["users"]] == ["token-a-secret", "token-b-secret"]
    assert {entry["authorizationRevision"] for entry in handoff["users"]} == {2}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    output_text = capsys.readouterr()
    combined = output_text.out + output_text.err
    assert "token-a-secret" not in combined
    assert "token-b-secret" not in combined
    assert "admin-secret-never-print" not in combined

    bootstrap_calls = [call for call in calls if call[1].endswith("/internal/admin/v1/bootstrap")]
    assert len(bootstrap_calls) == 2
    assert all(call[2]["X-Vitrial-Admin-Key"] == "admin-secret-never-print" for call in bootstrap_calls)
    assert all(call[3]["projectIDs"] == ["project-acceptance-fixed"] for call in bootstrap_calls)
    assert all("customer.create" in call[3]["capabilities"] for call in bootstrap_calls)
    assert all("project.create" in call[3]["capabilities"] for call in bootstrap_calls)

    push_calls = [call for call in calls if call[1].endswith("/api/v1/sync/push")]
    pull_calls = [call for call in calls if "/api/v1/sync/pull?" in call[1]]
    assert len(push_calls) == 1
    assert len(pull_calls) == 1
