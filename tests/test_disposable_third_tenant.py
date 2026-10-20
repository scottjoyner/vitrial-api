"""Network-free denial coverage for disposable HTTP third-company verifier."""
import importlib.util
import json
from pathlib import Path
import stat

import pytest

SPEC = importlib.util.spec_from_file_location(
    "third_tenant_verifier",
    Path(__file__).resolve().parents[1] / "scripts/verify_disposable_third_tenant.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

BASE = "http://127.0.0.1:45678"


def private_fixture(tmp_path):
    file = tmp_path / "handoff.json"
    file.write_text(json.dumps({
        "format": "vitrial.two-user-acceptance.v2",
        "baseURL": BASE, "organizationID": "synthetic-org-a",
        "customerID": "synthetic-customer", "projectID": "synthetic-project",
        "users": [{"label": "A"}, {"label": "B"}],
        "sharedProjectVerifiedFromUserB": True,
    }))
    file.chmod(0o600)
    return file


@pytest.mark.parametrize("url", [
    "https://example.invalid", "http://192.168.1.1:45678",
    "http://localhost:45678", "http://127.0.0.1:45678/path",
    "http://127.0.0.1:45678?query=1", "http://127.0.0.1",
    "http://root@127.0.0.1:45678", "file:///etc/passwd",
])
def test_requires_exact_loopback_origin(tmp_path, url):
    with pytest.raises(ValueError):
        module.verify(url, private_fixture(tmp_path), "test-admin-key")


def test_refuses_public_handoff_permissions(tmp_path):
    file = private_fixture(tmp_path)
    file.chmod(0o644)
    with pytest.raises(ValueError):
        module.verify(BASE, file, "test-admin-key")


def test_refuses_unverified_shared_first_pair(tmp_path):
    file = private_fixture(tmp_path)
    data = json.loads(file.read_text())
    data["sharedProjectVerifiedFromUserB"] = False
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        module.verify(BASE, file, "test-admin-key")


def test_detects_cross_tenant_record_leak(tmp_path, monkeypatch):
    file = private_fixture(tmp_path)
    calls = []

    def exchange(base, path, **kwargs):
        calls.append((path, kwargs))
        if path == "/internal/admin/v1/bootstrap":
            return {"accessToken": "x" * 40}
        if path == "/api/v1/auth/me":
            payload = calls[0][1]["payload"]
            return {"organizationID": payload["organizationID"],
                    "principalID": payload["userID"]}
        if path == "/api/v1/sync/pull?cursor=seq:0":
            return {"records": [{"entityID": "synthetic-customer"}]}
        raise AssertionError(path)

    monkeypatch.setattr(module, "exchange", exchange)
    with pytest.raises(RuntimeError, match="third tenant observed"):
        module.verify(BASE, file, "test-admin-key")
    assert len(calls) == 3


def test_zero_foreign_records_does_not_claim_default_company_sharing(tmp_path, monkeypatch):
    file = private_fixture(tmp_path)
    result = {}
    def exchange(base, path, **kwargs):
        if path == "/internal/admin/v1/bootstrap":
            result.update(kwargs["payload"])
            return {"accessToken": "x" * 40}
        if path == "/api/v1/auth/me":
            return {"organizationID": result["organizationID"],
                    "principalID": result["userID"]}
        return {"records": []}
    monkeypatch.setattr(module, "exchange", exchange)
    report = module.verify(BASE, file, "test-admin-key")
    assert report["gate"] == "PASS"
    assert report["default_company_sharing_proven"] is False

def test_refuses_symlinked_handoff(tmp_path):
    original = private_fixture(tmp_path)
    alias = tmp_path / "alias.json"
    alias.symlink_to(original)
    with pytest.raises(ValueError):
        module.verify(BASE, alias, "test-admin-key")