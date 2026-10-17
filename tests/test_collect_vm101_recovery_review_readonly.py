"""Fail-closed synthesis: a passing technical probe can never issue restore authority."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import os
import stat
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location("recovery_review",ROOT/"collect_vm101_recovery_review_readonly.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW=datetime(2026,10,17,19,45,0,tzinfo=timezone.utc)
FIELDS={"precreation":"technical_precreation_pass",
        "source_disk":"technical_disk_shape_pass",
        "postgres_startup":"technical_diagnostic_complete",
        "wal_checkpoint":"technical_wal_metadata_pass",
        "clone_quarantine":"technical_isolation_shape_pass",
        "clone_disk":"technical_disk_shape_pass"}


def fake_report(field=True,override=None):
    x={"gate":"HOLD","restore_authorized":False,
       "production_authorized":False,"executable_commands":[],
       "technical_holds":[]}
    x.update({field:True} if isinstance(field,str) else {})
    if field=="technical_wal_metadata_pass":
        x.update({"whole_wal_stream_verified":False,
                  "full_replay_verified":False,"tenant_data_verified":False,
                  "wal_archival_independently_proven":False,
                  "checkpoint_record_sampled_only":True,
                  "wal_end_of_written_data_proven":False,
                  "wal_readable_prefix_records":5,
                  "wal_first_boundary":"zero_length_record_boundary"})
    if field=="technical_diagnostic_complete":
        x.update({"root_cause_proven":False,"postgresql_recovery_proven":False,
                  "service_restarted":False,
                  "failure_hypothesis":"legacy_address_bind_failure_supported"})
    if override:x.update(override)
    return x


def probes(override=None):
    obj={k:fake_report(v) for k,v in FIELDS.items()}
    if override:
        name,field,value=override
        obj[name][field]=value
    return {key:(lambda x=value:x) for key,value in obj.items()}


class RecoveryReviewTests(unittest.TestCase):
    def test_all_synthetic_technical_checks_cannot_authorize(self):
        r=m.collect("root@test-proxmox",probes=probes(),now=NOW)
        self.assertFalse(r["all_technical_checks_pass"])
        self.assertTrue(all(r["checks"][k]["pass"] for k in FIELDS))
        self.assertFalse(r["checks"]["signed_approvals"]["pass"])
        self.assertEqual(r["gate"],"HOLD")
        self.assertFalse(r["restore_authorized"])
        self.assertFalse(r["production_authorized"])
        self.assertEqual(r["executable_commands"],[])
        self.assertEqual(r["expires_at_utc"],"2026-10-17T19:50:00+00:00")
        fingerprint=r.pop("evidence_sha256")
        self.assertEqual(fingerprint,hashlib.sha256(json.dumps(r,sort_keys=True,
              separators=(",",":")).encode()).hexdigest())

    def test_authorization_poison_or_non_hold_component_refused(self):
        for key in FIELDS:
            for field,value in (("restore_authorized",True),
                                ("production_authorized",True),
                                ("executable_commands",["qmrestore"]),
                                ("gate","APPROVED"),
                                (FIELDS[key],"yes")):
                with self.subTest(key=key,field=field):
                    r=m.collect("root@test",probes=probes((key,field,value)),now=NOW)
                    self.assertFalse(r["checks"][key]["pass"])
                    self.assertTrue(r["checks"][key]["unavailable"])
                    self.assertFalse(r["restore_authorized"])

    def test_missing_remote_and_unexpected_return_fail_closed(self):
        for kind in (RuntimeError("denied"),OSError("SSH unreachable"),ValueError("malformed")):
            with self.subTest(error=type(kind).__name__):
                p=probes()
                def err():raise kind
                p["source_disk"]=err
                r=m.collect("root@test",probes=p,now=NOW)
                self.assertFalse(r["checks"]["source_disk"]["pass"])
        p=probes();p["source_disk"]=lambda:None
        self.assertFalse(m.collect("root@test",probes=p,now=NOW)["checks"]["source_disk"]["pass"])

    def test_contradictory_pass_and_reasons_denied(self):
        p=probes()
        def contradiction():
            value=fake_report(FIELDS["precreation"])
            value["technical_holds"]=["Disk source unexpectedly moved"]
            return value
        p["precreation"]=contradiction
        result=m.collect("root@test",probes=p,now=NOW)
        self.assertFalse(result["checks"]["precreation"]["pass"])
        self.assertTrue(result["checks"]["precreation"]["unavailable"])
        self.assertEqual(result["gate"],"HOLD")

    def test_startup_diagnostic_cannot_assert_recovery_proof(self):
        p=probes()
        def forged():
            return {**fake_report("technical_diagnostic_complete"),
                    "root_cause_proven":True,
                    "postgresql_recovery_proven":True,
                    "service_restarted":True,
                    "failure_hypothesis":"legacy_address_bind_failure_supported"}
        p["postgres_startup"]=forged
        r=m.collect("root@test",probes=p,now=NOW)
        self.assertFalse(r["checks"]["postgres_startup"]["pass"])
        self.assertTrue(r["checks"]["postgres_startup"]["unavailable"])
        self.assertFalse(r["production_authorized"])

    def test_full_wal_replay_or_customer_records_never_laundered(self):
        for field in ("whole_wal_stream_verified","full_replay_verified",
                      "tenant_data_verified","wal_archival_independently_proven"):
            with self.subTest(field=field):
                p=probes()
                sample=fake_report("technical_wal_metadata_pass")
                sample.update({"whole_wal_stream_verified":False,
                               "full_replay_verified":False,
                               "tenant_data_verified":False,
                               "wal_archival_independently_proven":False,
                               "checkpoint_record_sampled_only":True,
                               "wal_end_of_written_data_proven":False,
                               "wal_readable_prefix_records":5,
                               "wal_first_boundary":"zero_length_record_boundary",
                               field:True})
                p["wal_checkpoint"]=lambda s=sample:s
                r=m.collect("root@test",probes=p,now=NOW)
                self.assertFalse(r["checks"]["wal_checkpoint"]["pass"])
                self.assertTrue(r["checks"]["wal_checkpoint"]["unavailable"])
                self.assertFalse(r["restore_authorized"])

    def test_wal_zero_length_boundary_is_not_end_of_wal_proof(self):
        r=m.collect("root@test",probes=probes(),now=NOW)
        w=r["checks"]["wal_checkpoint"]
        self.assertTrue(w["pass"])
        self.assertEqual(w["wal_readable_prefix_records"],5)
        self.assertEqual(w["wal_first_boundary"],"zero_length_record_boundary")
        self.assertFalse(r["restore_authorized"])
        for invalid in (("wal_end_of_written_data_proven",True),
                        ("wal_readable_prefix_records",True),
                        ("wal_first_boundary","confirmed_clean_end")):
            with self.subTest(invalid=invalid):
                p=probes(("wal_checkpoint",invalid[0],invalid[1]))
                result=m.collect("root@test",probes=p,now=NOW)
                self.assertFalse(result["checks"]["wal_checkpoint"]["pass"])
                self.assertTrue(result["checks"]["wal_checkpoint"]["unavailable"])

    def test_missing_pair_of_approval_files_denied(self):
        p=probes()
        with self.assertRaises(ValueError):
            m.collect("root@test",bundle=Path("/missing"),probes=p,now=NOW)
        with self.assertRaises(ValueError):
            m.collect("root@test",keys=Path("/missing"),probes=p,now=NOW)
        with self.assertRaises(ValueError):
            m.collect("root@test",probes={"source_disk":lambda:None},now=NOW)

    def test_private_evidence_output_is_exclusive(self):
        r=m.collect("root@test",probes=probes(),now=NOW)
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            os.chmod(base,0o700)
            p=base/"review.json"
            m.store_private(p,r)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
            self.assertEqual(json.loads(p.read_text()),r)
            with self.assertRaises(FileExistsError):m.store_private(p,r)
            os.chmod(base,0o755)
            with self.assertRaises(ValueError):m.store_private(base/"other.json",r)
            os.chmod(base,0o700)
            link=base/"link.json";link.symlink_to(p)
            with self.assertRaises(FileExistsError):m.store_private(link,r)

if __name__=="__main__":unittest.main()
