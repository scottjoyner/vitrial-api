"""Adversarial offline tests for the VM101 pre-creation admission gate."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
import sys
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("restore_admission", SCRIPTS / "audit_vm101_restore_precreation_readonly.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

GUESTS = ([{"id": f"qemu/{i}", "vmid": i, "type": "qemu", "node": "one"}
           for i in (100, 101, 102, 105, 111, 150)] +
          [{"id": f"lxc/{i}", "vmid": i, "type": "lxc", "node": "one"}
           for i in (106, 107)])
CFG = {"storage": "local", "type": "dir", "path": "/var/lib/vz",
       "content": "backup,iso,images,rootdir"}
STATE = {"type": "dir", "active": 1, "enabled": 1, "shared": 0,
         "content": "rootdir,images,iso", "avail": 1200000000000,
         "total": 1600000000000, "used": 400000000000}
SOURCE = "name: vitrial-prod-db\nscsi0: local:101/vm-101-disk-0.qcow2,cache=none,size=160G\n"
HEADER = "Volid Format  Type      Size VMID\n"


def audit(host="one\n", before=None, after=None, config=None, status=None,
          source=SOURCE, volumes=HEADER, path="ABSENT_PARENT_READY\n"):
    before = GUESTS if before is None else before
    after = before if after is None else after
    return m.assess(host, json.dumps(before), json.dumps(after),
                    json.dumps(CFG if config is None else config),
                    json.dumps(STATE if status is None else status),
                    source, volumes, path)


class RestoreAdmissionTests(unittest.TestCase):
    def test_technical_shape_pass_still_holds(self):
        result = audit()
        self.assertTrue(result["technical_precreation_pass"])
        self.assertEqual(result["gate"], "HOLD")
        for key in ("restore_authorized", "production_authorized", "dba_approval_verified",
                    "data_custodian_approval_verified", "network_isolation_verified",
                    "archive_bytes_verified_this_run", "destination_disk_reserved"):
            self.assertFalse(result[key])
        self.assertEqual(result["executable_commands"], [])

    def test_guest_id_collision(self):
        for kind in ("qemu", "lxc"):
            with self.subTest(kind=kind):
                self.assertFalse(audit(before=GUESTS+[{"id": f"{kind}/9101", "vmid": 9101,
                                                       "type": kind, "node": "one"}])["technical_precreation_pass"])

    def test_guest_inventory_drift_and_missing_source(self):
        self.assertFalse(audit(after=GUESTS[:-1])["technical_precreation_pass"])
        self.assertFalse(audit(before=GUESTS[:1]+GUESTS[2:])["technical_precreation_pass"])
        self.assertFalse(audit(before=GUESTS+[GUESTS[0]])["technical_precreation_pass"])
        self.assertFalse(audit(before=[{**GUESTS[1], "node": "unexpected"}]+GUESTS[2:])["technical_precreation_pass"])

    def test_wrong_host_or_source(self):
        self.assertFalse(audit(host="wrongnode\n")["technical_precreation_pass"])
        self.assertFalse(audit(source=SOURCE.replace("vitrial-prod-db", "other"))["technical_precreation_pass"])
        self.assertFalse(audit(source=SOURCE.replace("size=160G", "size=80G"))["technical_precreation_pass"])

    def test_storage_mismatch_and_capacity(self):
        for cfg in ({**CFG, "path": "/mnt/nfs"}, {**CFG, "content": "backup"},
                    {**CFG, "type": "nfs"}):
            with self.subTest(cfg=cfg):
                self.assertFalse(audit(config=cfg)["technical_precreation_pass"])
        for state in ({**STATE, "shared": 1}, {**STATE, "active": 0},
                      {**STATE, "avail": m.REQUIRED_FREE-1},
                      {**STATE, "avail": True}, {**STATE, "active": True},
                      {**STATE, "shared": False}, {**STATE, "avail": "very large"},
                      {**STATE, "avail": -1}):
            with self.subTest(state=state):
                self.assertFalse(audit(status=state)["technical_precreation_pass"])

    def test_volume_conflict_or_symlink(self):
        self.assertFalse(audit(volumes=HEADER+"local:9101/disk qcow2 images 500 9101\n")["technical_precreation_pass"])
        self.assertFalse(audit(path="OCCUPIED\n")["technical_precreation_pass"])
        self.assertFalse(audit(path="PARENT_UNTRUSTED\n")["technical_precreation_pass"])

    def test_malformed_remote_data(self):
        with self.assertRaises(ValueError):
            m._resources(json.dumps(GUESTS+[GUESTS[0]]))
        with self.assertRaises(ValueError):
            m._obj('{"storage":"local","storage":"else"}')
        with self.assertRaises(ValueError):
            m._obj("[]")
        with self.assertRaises(ValueError):
            m._resources("[]")

    def test_unauthorized_actions_are_not_implemented(self):
        source = (SCRIPTS / "audit_vm101_restore_precreation_readonly.py").read_text()
        for forbidden in ('"qm", "create"', '"qm", "start"', '"qmrestore"',
                          '"pct", "create"', '"qm", "set"', '"pg_resetwal"'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
