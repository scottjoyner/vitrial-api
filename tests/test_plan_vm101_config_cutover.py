"""Fail-closed, synthetic-only tests for VM101 two-file recovery planner."""
import importlib.util
from pathlib import Path
import hashlib
import unittest

path = Path(__file__).resolve().parents[1] / "scripts" / "plan_vm101_config_cutover.py"
spec = importlib.util.spec_from_file_location("cutover", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.VERIFIED_V2 = hashlib.sha256(b"synthetic-approved-archiver").hexdigest()
APPROVED = b"synthetic-approved-archiver"
LISTENER = b"listen_addresses = '10.77.0.10' # original host\nshared_buffers = '2GB'\n"
AUTO = (b"archive_mode = 'on'\n"
        b"archive_command = '/usr/local/sbin/vitrial-pg-archive-wal %p %f'\n"
        b"password_encryption = 'scram-sha-256'\n")


class PlannerSafetyTests(unittest.TestCase):
    def test_exact_changes_only_preserve_every_other_byte(self):
        r = m.plan(LISTENER, AUTO, APPROVED)
        self.assertEqual(r["status"], "CANDIDATE_ONLY_HOLD")
        self.assertEqual(r["changed_files"], 2)
        self.assertFalse(r["production_modified"])
        self.assertFalse(r["release_authorized"])
        self.assertEqual(
            r["listener_candidate_sha256"],
            m.digest(LISTENER.replace(b"'10.77.0.10'", b"'10.0.3.28'")))
        self.assertEqual(
            r["auto_candidate_sha256"],
            m.digest(AUTO.replace(b"vitrial-pg-archive-wal %p", b"vitrial-pg-archive-wal-v2 %p")))

    def test_unknown_archiver_hash_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER, AUTO, b"another-archiver")

    def test_new_listener_already_set_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER.replace(b"10.77.0.10", b"10.0.3.28"), AUTO, APPROVED)

    def test_legacy_archive_missing_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER, AUTO.replace(b"vitrial-pg-archive-wal %p",
                                          b"vitrial-pg-archive-wal-v2 %p"), APPROVED)

    def test_duplicate_listener_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER + LISTENER, AUTO, APPROVED)

    def test_duplicate_archive_command_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER, AUTO + AUTO, APPROVED)

    def test_auto_listener_override_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER, AUTO + b"listen_addresses = '*'\n", APPROVED)

    def test_listener_file_archive_override_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER + b"archive_command = 'true'\n", AUTO, APPROVED)

    def test_archive_mode_off_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER, AUTO.replace(b"archive_mode = 'on'",
                                          b"archive_mode = 'off'"), APPROVED)

    def test_archive_mode_duplicate_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER, AUTO + b"archive_mode = 'on'\n", APPROVED)

    def test_comments_not_a_second_setting(self):
        conf = b"# listen_addresses = '*'\n" + LISTENER
        report = m.plan(conf, AUTO, APPROVED)
        self.assertEqual(report["status"], "CANDIDATE_ONLY_HOLD")

    def test_non_utf8_source_refused(self):
        with self.assertRaises(m.Hold):
            m.plan(LISTENER + b"\xff", AUTO, APPROVED)

    def test_no_sensitive_config_values_in_output(self):
        secret = b"  # nonsecret-markers-stay-private\n"
        report = m.plan(LISTENER + secret, AUTO, APPROVED)
        self.assertNotIn("nonsecret-markers", str(report))

if __name__ == "__main__":
    unittest.main()