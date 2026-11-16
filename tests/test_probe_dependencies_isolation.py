"""Offline regression tests for bounded Docker healthchecks; no database or S3."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
from unittest import TestCase, main, mock

from scripts import probe_dependencies as probe


class DependencyProbeContracts(TestCase):
    def test_both_healthy_and_no_child_output(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ]) as runner:
            result = probe.run_probe()
        self.assertEqual(result, {"database": "ok", "objectStorage": "ok", "status": "ready"})
        self.assertEqual(runner.call_count, 2)
        self.assertEqual([call.args[0][-1] for call in runner.call_args_list],
                         ["database", "objectStorage"])
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 3.0)
            self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(call.kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertIs(call.kwargs["check"], False)
            self.assertEqual(call.args[0][-2], "--_probe")

    def test_failed_database_still_checks_storage_and_refuses_ready(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
        ]) as runner:
            result = probe.run_probe()
        self.assertEqual(result, {"database": "unavailable", "objectStorage": "ok",
                                  "status": "not-ready"})
        self.assertEqual(runner.call_count, 2)

    def test_failed_storage_or_both_refuses_ready(self):
        for exit_codes in ((0, 1), (1, 1)):
            with self.subTest(codes=exit_codes):
                with mock.patch.object(probe.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], n) for n in exit_codes
                ]):
                    result = probe.run_probe()
                self.assertEqual(result["status"], "not-ready")
                self.assertEqual(result["database"], "ok" if exit_codes[0] == 0 else "unavailable")
                self.assertEqual(result["objectStorage"], "ok" if exit_codes[1] == 0 else "unavailable")

    def test_timeout_and_spawn_errors_fail_closed_but_check_other_dependency(self):
        for failure in (subprocess.TimeoutExpired(["private"], 3.0),
                        OSError("secret=never-display")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(probe.subprocess, "run", side_effect=[
                    failure, subprocess.CompletedProcess([], 0),
                ]) as runner:
                    result = probe.run_probe()
                self.assertEqual(runner.call_count, 2)
                self.assertEqual(result["database"], "unavailable")
                self.assertEqual(result["objectStorage"], "ok")
                self.assertEqual(result["status"], "not-ready")

    def test_real_subprocess_timeout_terminates_hung_child_without_leaking_output(self):
        # An isolated fake child demonstrates kill-on-timeout rather than relying
        # on asyncio cancellation that may await a blocked default executor.
        with tempfile.TemporaryDirectory(prefix="vitrial-probe-contract-") as td:
            dummy = Path(td) / "mock_probe.py"
            dummy.write_text(
                "import sys, time\n"
                "print('SECRET_NEVER_ECHO', flush=True)\n"
                "if sys.argv[-1] == 'database': time.sleep(5)\n"
            )
            output = io.StringIO()
            start = time.monotonic()
            with mock.patch.object(probe, "__file__", str(dummy)), \
                 mock.patch.object(probe, "PER_DEPENDENCY_TIMEOUT_SECONDS", 0.20), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = probe.run_probe()
            self.assertLess(time.monotonic() - start, 2.0)
            self.assertNotIn("SECRET_NEVER_ECHO", output.getvalue())
            self.assertEqual(result, {"database": "unavailable", "objectStorage": "ok",
                                      "status": "not-ready"})

    def test_cli_quiet_fail_and_success_are_silent(self):
        for status, expected_code in (("ready", 0), ("not-ready", 1)):
            with self.subTest(status=status):
                out = io.StringIO()
                with mock.patch.object(probe, "run_probe", return_value={"status": status}), \
                     mock.patch.object(sys, "argv", ["probe_dependencies.py", "--quiet"]), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    code = probe.main()
                self.assertEqual(code, expected_code)
                self.assertEqual(out.getvalue(), "")

    def test_cli_nonquiet_reports_only_categorical_flags(self):
        out = io.StringIO()
        with mock.patch.object(probe, "run_probe", return_value={
                "database": "unavailable", "objectStorage": "ok", "status": "not-ready"}), \
             mock.patch.object(sys, "argv", ["probe_dependencies.py"]), \
             contextlib.redirect_stdout(out):
            code = probe.main()
        self.assertEqual(code, 1)
        self.assertIn('"database": "unavailable"', out.getvalue())
        self.assertNotIn("password", out.getvalue().lower())

    def test_internal_subprocess_probe_uses_actual_dependency_coroutines(self):
        parent = types.ModuleType("app")
        parent.__path__ = []
        readiness = types.ModuleType("app.readiness")
        called = []
        async def database():
            called.append("database")
        async def storage():
            called.append("objectStorage")
        readiness.probe_database = database
        readiness.probe_storage = storage
        with mock.patch.dict(sys.modules, {"app": parent, "app.readiness": readiness}):
            self.assertEqual(probe._internal_probe("database"), 0)
            self.assertEqual(probe._internal_probe("objectStorage"), 0)
            self.assertEqual(probe._internal_probe("bad-name"), 1)
        self.assertEqual(called, ["database", "objectStorage"])


if __name__ == "__main__":
    main()
