"""Synthetic-only tests of the exact VM101 HBA client CIDR candidate."""
import hashlib
import importlib.util
from pathlib import Path
import pytest

target = Path(__file__).resolve().parents[1] / "scripts" / "plan_vm101_hba_cidr.py"
spec = importlib.util.spec_from_file_location("hba_cidr", target)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SOURCE = (
    b"# Existing host access:\n"
    b"local all postgres peer\n"
    b"host all all 127.0.0.1/32 scram-sha-256\n"
    b"host vitrial vitrial 10.77.0.2/32 scram-sha-256\n"
    b"# host vitrial vitrial 0.0.0.0/0 trust\n"
)


def test_exact_32bit_client_change_preserves_all_other_bytes():
    r = mod.plan(SOURCE)
    target = SOURCE.replace(b"10.77.0.2/32", b"10.0.3.27/32", 1)
    assert r["status"] == "CANDIDATE_ONLY_HOLD"
    assert r["changed_rules"] == 1
    assert r["current_sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert r["candidate_sha256"] == hashlib.sha256(target).hexdigest()
    assert r["candidate_bytes"] == target


def test_comments_are_ignored_and_preserved():
    r = mod.plan(SOURCE + b"# 10.0.3.27/32 mentioned in comment\n")
    assert b"# 10.0.3.27/32 mentioned in comment" in r["candidate_bytes"]


@pytest.mark.parametrize("bad", [
    SOURCE + b"host vitrial vitrial 10.77.0.2/32 scram-sha-256\n",
    SOURCE.replace(b"scram-sha-256\n# host", b"trust\n# host"),
    SOURCE.replace(b"10.77.0.2/32", b"10.0.3.27/32", 1),
    SOURCE.replace(b"10.77.0.2/32", b"10.77.0.3/32", 1),
    SOURCE + b"host all all 0.0.0.0/0 trust\n",
    SOURCE + b"host all all ::/0 md5\n",
    SOURCE + b"host vitrial vitrial 10.0.3.27/32 scram-sha-256\n",
    SOURCE + b"host other other 10.77.0.2/32 scram-sha-256\n",
    SOURCE.replace(b"host vitrial vitrial", b"host all vitrial"),
    SOURCE + b"\xff",
])
def test_unreviewed_hba_changes_fail_closed(bad):
    with pytest.raises(mod.Hold):
        mod.plan(bad)


def test_no_modified_configuration_leaks_from_cli():
    r = mod.plan(SOURCE)
    public = {k: v for k, v in r.items() if k != "candidate_bytes"}
    assert "secret" not in str(public).lower()
    assert "candidate_sha256" in public
    assert "10.0.3.27/32" in str(public)