"""VM101 failed-startup hypothesis must remain evidence-only and fail closed."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location("vm101_startup",SCRIPTS/"diagnose_vm101_postgres_startup_readonly.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SOURCE="name: vitrial-prod-db\nscsi0: local:101/vm-101-disk-0.qcow2,size=160G\n"
DATA={
    "service_active":False,"service_failed":True,"result_protocol":True,
    "unit_forking":True,"inactive_since_oct6":True,
    "postgres_port_listening":False,
    "configured_legacy_listener":True,"configured_current_listener":False,
    "journal_read_ok":True,"journal_window_oct6_count":14,
    "journal_bind_failure":True,"journal_pid_file_missing":True,
    "journal_protocol_failure":True,"journal_wal_marker":False,
    "journal_space_marker":False,"pg_control_state":"in production",
}


class StartupDiagnosisTests(unittest.TestCase):
    def test_positive_hypothesis_is_not_proof_or_approval(self):
        r=m.assess(dict(DATA),"one\n",SOURCE)
        self.assertEqual(r["failure_hypothesis"],"legacy_address_bind_failure_supported")
        self.assertTrue(r["technical_diagnostic_complete"])
        self.assertFalse(r["root_cause_proven"])
        self.assertFalse(r["postgresql_recovery_proven"])
        self.assertFalse(r["production_authorized"])
        self.assertFalse(r["restore_authorized"])
        self.assertEqual(r["executable_commands"],[])
        self.assertEqual(r["gate"],"HOLD")
        self.assertIn("Control file may require crash/WAL recovery",
                      r["sanitized_hold_categories"])

    def test_missing_any_positive_condition_blocks_hypothesis(self):
        fields=("service_failed","result_protocol","journal_read_ok",
                "journal_protocol_failure","unit_forking","inactive_since_oct6",
                "journal_bind_failure","journal_pid_file_missing",
                "configured_legacy_listener")
        for key in fields:
            with self.subTest(key=key):
                e={**DATA,key:False}
                self.assertEqual(m.assess(e,"one",SOURCE)["failure_hypothesis"],
                                 "unverified")
        for key in ("service_active","configured_current_listener",
                    "postgres_port_listening"):
            with self.subTest(key=key):
                e={**DATA,key:True}
                self.assertEqual(m.assess(e,"one",SOURCE)["failure_hypothesis"],
                                 "unverified")

    def test_schema_invalid_and_wrong_guest_deny(self):
        for changed in ("missing","extra","boolean_as_string","journal_nan",
                        "bad_control_state"):
            with self.subTest(case=changed):
                e=DATA.copy()
                if changed=="missing":e.pop("service_failed")
                if changed=="extra":e["secret_env"]="should-not-leak"
                if changed=="boolean_as_string":e["journal_bind_failure"]="yes"
                if changed=="journal_nan":e["journal_window_oct6_count"]=float("nan")
                if changed=="bad_control_state":e["pg_control_state"]="customer_record_here"
                r=m.assess(e,"one",SOURCE)
                self.assertFalse(r["technical_diagnostic_complete"])
                self.assertEqual(r["failure_hypothesis"],"unverified")
                self.assertNotIn("secret_env",str(r))
                self.assertNotIn("customer_record_here",str(r))
        self.assertFalse(m.assess(DATA,"other",SOURCE)["technical_diagnostic_complete"])
        self.assertFalse(m.assess(DATA,"one",SOURCE.replace("vitrial-prod-db","other"))["technical_diagnostic_complete"])

    def test_historic_wal_and_space_signals_preserve_hold(self):
        for flag in ("journal_wal_marker","journal_space_marker"):
            with self.subTest(flag=flag):
                r=m.assess({**DATA,flag:True},"one",SOURCE)
                self.assertTrue(r["technical_diagnostic_complete"])
                self.assertIn("Historical WAL or disk-space diagnostic marker present",
                              r["sanitized_hold_categories"])
                self.assertFalse(r["postgresql_recovery_proven"])

    def test_unavailable_remote_or_unexpected_exec_is_denied(self):
        with patch.object(m,"remote",side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                m.capture("root@offline")
        self.assertNotIn('"qm","start"',(SCRIPTS/"diagnose_vm101_postgres_startup_readonly.py").read_text())
        self.assertNotIn('"pg_ctlcluster","start"',(SCRIPTS/"diagnose_vm101_postgres_startup_readonly.py").read_text())

    def test_raw_logs_and_sql_never_exposed_as_fields(self):
        for forbidden in ("journal_raw","postgresql_log","sql_rows","database_password"):
            self.assertNotIn(forbidden,m.KEYS)
        for obj in (m.assess(DATA,"one",SOURCE),m.assess({**DATA,"journal_bind_failure":False},"one",SOURCE)):
            for forbidden in ("10.77.0.10","password","/etc/postgresql"):
                self.assertNotIn(forbidden,str(obj))


if __name__=="__main__":
    unittest.main()
