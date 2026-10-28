"""Synthetic-only cutover planning tests; never write live service config."""
import importlib.util
from pathlib import Path
import pytest

p = Path(__file__).resolve().parents[1] / "scripts" / "plan_vm100_private_routes.py"
spec = importlib.util.spec_from_file_location("private_plan", p)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

DB = "postgresql+asyncpg://vitrial:user%40secret%3Akeep@10.77.0.10:5432/vitrial?sslmode=prefer"
S3 = "http://10.77.0.11:9000"


def container(db=DB, s3=S3):
    return [{"Config": {"Env": [
        "DATABASE_URL=" + db,
        "S3_ENDPOINT_URL=" + s3,
        "S3_ACCESS_KEY_ID=not-for-logs",
        "S3_SECRET_ACCESS_KEY=retain-byte-for-byte",
        "UNRELATED=value=with=equals",
    ]}}]


def test_plan_preserves_all_secrets_and_unrelated_bytes():
    report = mod.plan(container())
    assert report["status"] == "PATCH_REQUIRED"
    assert report["changes_required"] == ["DATABASE_URL", "S3_ENDPOINT_URL"]
    env = report["proposed_environment"]
    assert "user%40secret%3Akeep@10.0.3.28:5432/vitrial?sslmode=prefer" in env[0]
    assert env[1] == "S3_ENDPOINT_URL=http://10.0.3.29:9000"
    assert env[2:] == container()[0]["Config"]["Env"][2:]

def test_already_correct_routes_are_bytewise_unchanged():
    report = mod.plan(container(DB.replace("10.77.0.10", "10.0.3.28"),
                                S3.replace("10.77.0.11", "10.0.3.29")))
    assert report["status"] == "ALREADY_ON_TARGET"
    assert report["changes_required"] == []
    assert report["proposed_environment"] == container(
        DB.replace("10.77.0.10", "10.0.3.28"),
        S3.replace("10.77.0.11", "10.0.3.29"))[0]["Config"]["Env"]


@pytest.mark.parametrize("bad_db", [
    DB.replace("10.77.0.10", "38.143.56.139"),
    DB.replace("5432", "5433"),
    DB.replace("postgresql+asyncpg", "http"),
    "postgresql+asyncpg://10.77.0.10:5432/vitrial",
    DB + "#fragment",
    DB.replace("10.77.0.10", "10.0.3.28.evil.com"),
])
def test_wrong_db_destinations_denied(bad_db):
    with pytest.raises(mod.CutoverPlanError):
        mod.plan(container(db=bad_db))


@pytest.mark.parametrize("bad_s3", [
    "http://10.77.0.11:443",
    "http://other.example.com:9000",
    "ftp://10.77.0.11:9000",
    "http://user:pw@10.77.0.11:9000",
    "http://10.77.0.11:9000/prefix",
    "http://10.77.0.11:9000?token=secret",
])
def test_unapproved_s3_routes_denied(bad_s3):
    with pytest.raises(mod.CutoverPlanError):
        mod.plan(container(s3=bad_s3))

def test_duplicate_dependency_denied():
    c = container()
    c[0]["Config"]["Env"].append("DATABASE_URL=" + DB)
    with pytest.raises(mod.CutoverPlanError):
        mod.plan(c)


def test_missing_dependency_denied():
    c = container()
    c[0]["Config"]["Env"] = c[0]["Config"]["Env"][:-1]
    c[0]["Config"]["Env"] = [
        x for x in c[0]["Config"]["Env"] if not x.startswith("S3_ENDPOINT_URL=")
    ]
    with pytest.raises(mod.CutoverPlanError):
        mod.plan(c)


@pytest.mark.parametrize("bad", [{}, [], [{}], [{"Config": {"Env": None}}],
                                  [{"Config": {"Env": ["NO_EQUALS"]}}]])
def test_malformed_docker_inspect_denied(bad):
    with pytest.raises(mod.CutoverPlanError):
        mod.plan(bad)


def test_no_sensitive_fields_in_sanitized_response():
    report = mod.plan(container())
    public = {k: v for k, v in report.items() if k != "proposed_environment"}
    assert "user%40secret" not in str(public)
    assert "retain-byte-for-byte" not in str(public)
    assert "S3_ACCESS_KEY_ID" not in str(public)