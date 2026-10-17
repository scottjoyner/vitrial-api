"""Synthetic keys prove denial/verification mechanics only. No operator approved a restore."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("approval", HERE / "verify_vm101_approval_receipts.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026, 10, 17, 19, 0, tzinfo=timezone.utc)


def iso(v):
    return v.strftime("%Y-%m-%dT%H:%M:%SZ")


def fixtures():
    import base64
    payload = {
        "action": "offline_isolated_validation_only",
        "node": "one", "source_vmid": 101, "candidate_vmid": 9101,
        "backup_name": m.BACKUP, "backup_sha256": m.SHA,
        "source_disk_gib": 160, "network_interfaces": [], "onboot": False,
        "window_start_utc": iso(NOW+timedelta(minutes=15)),
        "window_end_utc": iso(NOW+timedelta(hours=2)),
        "nonce": "a"*32, "recovery_objective": "isolated catalog and WAL probe only",
        "custody_ticket": "SCRUM-40-approved-by-real-owner",
    }
    roots = {"schema": "vitrial.vm101.owner-trust/v1", "keys": []}
    bundle = {"schema": "vitrial.vm101.approval-bundle/v1",
              "proposal": payload, "approvals": []}
    privs = []
    for idx, role in enumerate(sorted(m.ROLES)):
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        kid, principal = f"synthetic-owner-{idx}", f"independent-test-principal-{idx}"
        roots["keys"].append({
            "key_id": kid, "principal": principal, "roles": [role],
            "public_key_b64": base64.b64encode(pub).decode(),
            "revoked": False, "not_before_utc": iso(NOW-timedelta(days=7)),
            "not_after_utc": iso(NOW+timedelta(days=7)),
        })
        approval = {"role": role, "principal": principal, "key_id": kid,
                    "signed_at_utc": iso(NOW-timedelta(minutes=10))}
        approval["signature_b64"] = base64.b64encode(
            key.sign(m.canonical(payload, approval))).decode()
        bundle["approvals"].append(approval)
        privs.append(key)
    return bundle, roots, privs


class CryptographicApprovalTests(unittest.TestCase):
    def test_valid_synthetic_signatures_do_not_authorize(self):
        b, t, _ = fixtures()
        result = m.assess(b, t, NOW)
        self.assertTrue(result["crypto_approval_reviewable"])
        self.assertEqual(result["verified_roles"], sorted(m.ROLES))
        self.assertEqual(result["gate"], "HOLD")
        for key in ("restoration_authorized", "production_authorized",
                    "live_hypervisor_and_isolation_checked", "approval_replay_ledger_checked"):
            self.assertFalse(result[key])
        self.assertEqual(result["executable_commands"], [])

    def test_changed_proposal_never_reuses_signatures(self):
        for key, value in (("source_vmid", 102), ("candidate_vmid", 101),
                           ("backup_name", "wrong.vma.zst"), ("backup_sha256", "b"*64),
                           ("network_interfaces", ["net0"]), ("onboot", True),
                           ("recovery_objective", "changed interpretation"),
                           ("nonce", "b"*32), ("window_end_utc", iso(NOW+timedelta(hours=3)))):
            with self.subTest(key=key):
                b, t, _ = fixtures()
                b["proposal"][key] = value
                self.assertFalse(m.assess(b, t, NOW)["crypto_approval_reviewable"])

    def test_signer_identity_tamper_and_expired_or_revoked(self):
        variants = ("role", "principal", "key_id", "signed_at_utc", "signature_b64")
        for field in variants:
            with self.subTest(field=field):
                b,t,_=fixtures()
                b["approvals"][0][field] = "invalid"
                self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])
        for modifier in ("revoked", "role", "expired", "wrong_public_key"):
            with self.subTest(modifier=modifier):
                b,t,_ = fixtures()
                record=t["keys"][0]
                if modifier == "revoked": record["revoked"] = True
                elif modifier == "role": record["roles"] = ["infrastructure"] if record["roles"][0] != "infrastructure" else ["dba"]
                elif modifier == "expired": record["not_after_utc"] = iso(NOW-timedelta(days=2))
                else: record["public_key_b64"] = t["keys"][1]["public_key_b64"]
                self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])

    def test_independent_principals_and_unique_roles(self):
        b,t,_ = fixtures()
        b["approvals"].pop()
        self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])
        b,t,_ = fixtures()
        b["approvals"][1]["role"] = b["approvals"][0]["role"]
        self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])
        b,t,_ = fixtures()
        b["approvals"][1]["principal"] = b["approvals"][0]["principal"]
        self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])
        b,t,_ = fixtures()
        t["keys"][1]["key_id"] = t["keys"][0]["key_id"]
        self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])

    def test_window_stale_or_overlong(self):
        for start,end in ((NOW-timedelta(days=1),NOW-timedelta(hours=1)),
                          (NOW+timedelta(hours=15),NOW+timedelta(hours=16)),
                          (NOW+timedelta(minutes=5),NOW+timedelta(days=2))):
            with self.subTest(start=start):
                b,t,_=fixtures()
                b["proposal"]["window_start_utc"] = iso(start)
                b["proposal"]["window_end_utc"] = iso(end)
                self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])

    def test_broken_bundle_or_registry_fails_closed(self):
        for mutation in ("extra", "no_schema", "bad_list", "boolean_vmid", "unknown_role"):
            with self.subTest(mutation=mutation):
                b,t,_=fixtures()
                if mutation=="extra": b["untrusted"]=True
                if mutation=="no_schema": b.pop("schema")
                if mutation=="bad_list": b["approvals"]={}
                if mutation=="boolean_vmid": b["proposal"]["source_vmid"]=True
                if mutation=="unknown_role": b["approvals"][0]["role"]="operator"
                self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])

    def test_private_permissions_and_symlink_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"trust.json"
            path.write_text('{"hello":"world"}')
            os.chmod(path,0o600)
            self.assertEqual(m.safe_json_file(path), {"hello":"world"})
            os.chmod(path,0o644)
            with self.assertRaises(ValueError): m.safe_json_file(path)
            os.chmod(path,0o600)
            link=Path(tmp)/"link.json"
            link.symlink_to(path)
            with self.assertRaises(OSError): m.safe_json_file(link)
            path.write_text('{"x":1,"x":2}')
            with self.assertRaises(ValueError): m.safe_json_file(path)

    def test_malformed_signature_fails_without_exception(self):
        b,t,_=fixtures()
        b["approvals"][0]["signature_b64"] = "%"*88
        self.assertFalse(m.assess(b,t,NOW)["crypto_approval_reviewable"])


if __name__ == "__main__":
    unittest.main()
