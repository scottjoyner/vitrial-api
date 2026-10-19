"""Offline negative-policy tests for the disposable real HTTP company-scope witness."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts"
import sys
sys.path.insert(0, str(SOURCE))
SPEC = importlib.util.spec_from_file_location("company_scope_verify", SOURCE / "verify_disposable_company_scope.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

BASE = "http://127.0.0.1:47231"


def fixture(tmp_path):
    file = tmp_path / "handoff.json"
    file.write_text(json.dumps({
        "format": "vitrial.two-user-acceptance.v2",
        "baseURL": BASE, "organizationID": "synthetic-company",
        "customerID": "synthetic-customer", "projectID": "synthetic-project",
        "users": [{"label": "A"}, {"label": "B"}],
        "sharedProjectVerifiedFromUserB": True,
    }))
    file.chmod(0o600)
    return file


@pytest.mark.parametrize("bad", [
    "https://example.invalid:47231", "http://localhost:47231",
    "http://192.168.1.20:47231", "http://127.0.0.1:47231/admin",
    "http://127.0.0.1:47231?scope=all", "http://127.0.0.1",
    "http://user@127.0.0.1:47231", "file:///tmp/anything",
])
def test_rejects_any_remote_or_ambiguous_target(tmp_path, bad):
    with pytest.raises(ValueError):
        module.ensure_local_handoff(bad, fixture(tmp_path))


def test_refuses_world_readable_or_symlinked_handoff(tmp_path):
    file = fixture(tmp_path)
    file.chmod(0o644)
    with pytest.raises(ValueError):
        module.ensure_local_handoff(BASE, file)
    file.chmod(0o600)
    alias = tmp_path / "alias.json"
    alias.symlink_to(file)
    with pytest.raises(ValueError):
        module.ensure_local_handoff(BASE, alias)


def test_refuses_unverified_initial_two_user_source(tmp_path):
    file = fixture(tmp_path)
    data = json.loads(file.read_text())
    data["sharedProjectVerifiedFromUserB"] = False
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        module.ensure_local_handoff(BASE, file)


def fake_http(monkeypatch, mode="good"):
    state = {"last": None, "created": [], "fifth": None}

    def exchange(base, path, **kw):
        if path == "/internal/admin/v1/bootstrap":
            payload = kw["payload"]
            state["last"] = payload
            state["created"].append(payload)
            return {"accessToken": "synthetic-"+payload["userID"]+"-"+("z"*40)}
        if path == "/api/v1/auth/me":
            payload = state["last"]
            assert kw["headers"]["Authorization"].startswith("Bearer synthetic-")
            scope = payload["sharedCompanyAccess"]
            if mode == "scope_mismatch" and scope:
                scope = False
            return {"organizationID": payload["organizationID"],
                    "principalID": payload["userID"],
                    "allCustomers": scope, "allProjects": scope}
        if path == "/api/v1/sync/pull?cursor=seq:0":
            payload = state["last"]
            if payload["sharedCompanyAccess"]:
                values = [{"entityID": "synthetic-customer"},
                          {"entityID": "synthetic-project"}]
                if mode == "missing_project":
                    values.pop()
                return {"records": values}
            if mode == "scoped_leak":
                return {"records": [{"entityID": "synthetic-customer"}]}
            return {"records": []}
        raise AssertionError(path)

    def fifth(*, base, admin, payload):
        assert base == BASE and admin == "valid-test-key-" + "x"*32
        state["fifth"] = payload
        if mode == "fifth_admitted":
            raise RuntimeError("fifth synthetic active seat was admitted")

    monkeypatch.setattr(module, "exchange", exchange)
    monkeypatch.setattr(module, "assert_fifth_seat_denied", fifth)
    return state


def test_new_shared_principal_uses_no_id_grants(tmp_path, monkeypatch):
    state = fake_http(monkeypatch)
    result = module.verify(BASE, fixture(tmp_path), "valid-test-key-"+"x"*32)
    assert result["gate"] == "PASS"
    assert result["new_shared_member_uses_explicit_ids"] is False
    assert result["new_shared_member_project_visible"] is True
    assert result["limited_member_visible_records"] == 0
    assert result["fifth_active_seat_denied_http_409"] is True
    assert len(state["created"]) == 2
    assert [p["sharedCompanyAccess"] for p in state["created"]] == [True, False]
    assert all(p["customerIDs"] == [] and p["projectIDs"] == []
               and p["allCustomers"] is False and p["allProjects"] is False
               for p in state["created"])
    assert state["fifth"]["organizationID"] == "synthetic-company"


@pytest.mark.parametrize(("mode", "message"), [
    ("missing_project", "new unscoped shared-company"),
    ("scoped_leak", "ungranted same-company"),
    ("scope_mismatch", "server effective scope"),
    ("fifth_admitted", "fifth synthetic active seat"),
])
def test_refuses_default_sharing_or_seat_failure(tmp_path, monkeypatch, mode, message):
    fake_http(monkeypatch, mode=mode)
    with pytest.raises(RuntimeError, match=message):
        module.verify(BASE, fixture(tmp_path), "valid-test-key-"+"x"*32)


def test_new_member_id_grants_never_supplied():
    member = module.new_member(company="synthetic-org", nonce="abc", label="shared", shared=True)
    assert member["sharedCompanyAccess"] is True
    assert not member["customerIDs"] and not member["projectIDs"]
    assert member["allCustomers"] is False and member["allProjects"] is False
    assert member["capabilities"] == ["sync"]


def test_short_admin_key_refused_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "exchange", lambda *args, **kwargs: pytest.fail("network called"))
    with pytest.raises(ValueError):
        module.verify(BASE, fixture(tmp_path), "short")