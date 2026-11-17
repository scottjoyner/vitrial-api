"""No-network contracts for the real-service Docker health-probe acceptance."""
from __future__ import annotations

import json
import subprocess
from unittest import TestCase, mock

from scripts import validate_live_dependency_probe as validate


def isolated_env():
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ID": "123456789",
        "POSTGRES_INTEGRATION": "1",
        "S3_INTEGRATION": "1",
        "EVIDENCE_STORAGE_PROVIDER": "s3",
        "S3_BUCKET": "vitrial-evidence",
        "DATABASE_URL": "postgresql+asyncpg://synthetic:synthetic@localhost:5432/vitrial",
        "S3_ENDPOINT_URL": "http://localhost:9000",
    }


class LiveProbeAdmission(TestCase):
    def test_only_known_loopback_fixtures_are_admitted(self):
        self.assertTrue(validate.require_isolated_services(isolated_env()))
        for key, values in {
            "GITHUB_ACTIONS": ["false", ""],
            "GITHUB_RUN_ID": ["", "undefined", "abc"],
            "POSTGRES_INTEGRATION": ["0", ""],
            "S3_INTEGRATION": ["0", ""],
            "EVIDENCE_STORAGE_PROVIDER": ["local", ""],
            "S3_BUCKET": ["production-evidence", ""],
            "DATABASE_URL": ["postgresql+asyncpg://prod.internal:5432/vitrial",
                             "postgresql+asyncpg://localhost:5433/vitrial"],
            "S3_ENDPOINT_URL": ["https://prod.example", "http://localhost:9010",
                                "http://evil@localhost:9000",
                                "http://localhost:9000/path"],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    env = isolated_env()
                    env[key] = value
                    self.assertFalse(validate.require_isolated_services(env))

    def test_missing_fields_fail_closed(self):
        for key in isolated_env():
            with self.subTest(key=key):
                env = isolated_env()
                del env[key]
                self.assertFalse(validate.require_isolated_services(env))

    def test_healthy_real_probe_schema_and_exit(self):
        expected = {"database": "ok", "objectStorage": "ok", "status": "ready"}
        with mock.patch.object(validate.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(expected), stderr="")) as runner:
            elapsed = validate.run_case("both-healthy", {}, expected, isolated_env())
        self.assertGreaterEqual(elapsed, 0)
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 9.5)
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["cwd"], validate.ROOT)
        self.assertEqual(kwargs["env"]["S3_ENDPOINT_URL"], "http://localhost:9000")
        self.assertEqual(kwargs["env"]["DATABASE_URL"], isolated_env()["DATABASE_URL"])

    def test_negative_case_requires_exit_one_and_storage_still_ok(self):
        expected = {"database": "unavailable", "objectStorage": "ok", "status": "not-ready"}
        with mock.patch.object(validate.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout=json.dumps(expected), stderr="")) as runner:
            validate.run_case("database-unavailable", {"DATABASE_URL": "postgresql+asyncpg://probe@127.0.0.1:1/unreachable"}, expected, isolated_env())
        self.assertEqual(runner.call_args.kwargs["env"]["DATABASE_URL"],
                         "postgresql+asyncpg://probe@127.0.0.1:1/unreachable")
        with mock.patch.object(validate.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(expected), stderr="")):
            with self.assertRaisesRegex(RuntimeError, "exit status"):
                validate.run_case("database-unavailable", {}, expected, isolated_env())

    def test_refuse_extra_fields_to_prevent_secret_output(self):
        expected = {"database": "ok", "objectStorage": "ok", "status": "ready"}
        actual = {**expected, "database_url": "postgresql://secret@prod"}
        with mock.patch.object(validate.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(actual), stderr="")):
            with self.assertRaisesRegex(RuntimeError, "unapproved fields"):
                validate.run_case("unexpected-field", {}, expected, isolated_env())

    def test_timeout_and_stderr_fail_closed_without_echoing_secrets(self):
        expected = {"database": "ok", "objectStorage": "ok", "status": "ready"}
        with mock.patch.object(validate.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["private"], 9.5)):
            with self.assertRaisesRegex(RuntimeError, "budget"):
                validate.run_case("timeout", {}, expected, isolated_env())
        with mock.patch.object(validate.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(expected), stderr="secret=never-log")):
            with self.assertRaisesRegex(RuntimeError, "unexpected diagnostics") as cm:
                validate.run_case("stderr", {}, expected, isolated_env())
        self.assertNotIn("never-log", str(cm.exception))

    def test_main_unapproved_endpoint_refuses_before_any_probe(self):
        env = isolated_env()
        env["DATABASE_URL"] = "postgresql+asyncpg://production.example:5432/prod"
        with mock.patch.dict(validate.os.environ, env, clear=True), \
             mock.patch.object(validate, "run_case") as checker:
            self.assertEqual(validate.main(), 2)
        checker.assert_not_called()

    def test_main_executes_all_three_cases_in_order(self):
        with mock.patch.dict(validate.os.environ, isolated_env(), clear=True), \
             mock.patch.object(validate, "run_case", return_value=0.5) as checker:
            self.assertEqual(validate.main(), 0)
        self.assertEqual([call.args[0] for call in checker.call_args_list],
                         ["both-healthy", "database-unavailable", "storage-unavailable"])
        self.assertEqual(checker.call_count, 3)


if __name__ == "__main__":
    import unittest
    unittest.main()
