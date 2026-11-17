"""Offline executed contracts for the isolated S3 CI image immutable digest.

No real Docker, registry, buckets, network or credentials are touched. Mocked
executables verify invalid overrides fail before a container is started.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
PIN = "rustfs/rustfs@sha256:ee717072290bec9b11abfc0365f8310b00df1cd9b3639943edae47bdef05044a"
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def s3_setup_script() -> str:
    source = WORKFLOW.read_text()
    assert source.count("- name: Start S3-compatible object storage") == 1
    start = source.index("      - name: Start S3-compatible object storage\n")
    end = source.index("      - name: Create evidence bucket\n", start)
    entry = source[start:end]
    assert entry.count("        run: |\n") == 1
    return textwrap.dedent(entry.split("        run: |\n", 1)[1])


class ImmutableS3ImageCI(unittest.TestCase):
    def run_mock(self, image: str | None = None):
        with tempfile.TemporaryDirectory(prefix="vitrial-s3-ci-mock-") as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "commands"
            github_env = root / "github-env"
            for name, script in {
                "docker": '#!/bin/sh\necho "docker:$*" >> "$CALLS"\nexit 0\n',
                "curl": '#!/bin/sh\necho "curl:$*" >> "$CALLS"\nexit 0\n',
            }.items():
                exe = bin_dir / name
                exe.write_text(script)
                exe.chmod(0o755)
            env = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "GITHUB_ENV": str(github_env),
                "CALLS": str(calls),
                "S3_ACCESS_KEY_ID": "only-synthetic-user",
                "S3_SECRET_ACCESS_KEY": "only-synthetic-password",
            }
            if image is None:
                env.pop("S3_IMAGE", None)
            else:
                env["S3_IMAGE"] = image
            result = subprocess.run(
                ["/bin/bash", "-ec", s3_setup_script()],
                capture_output=True, text=True, env=env, timeout=8,
            )
            commands = calls.read_text().splitlines() if calls.exists() else []
            status = github_env.read_text().strip() if github_env.exists() else None
            return result, commands, status

    def test_registry_index_digest_is_exactly_pinned(self):
        script = s3_setup_script()
        self.assertIn(PIN, script)
        self.assertNotIn("rustfs/rustfs:latest}", script)
        self.assertIn("image@sha256:digest reference", script)
        self.assertIn("S3_INTEGRATION=1", script)
        self.assertIn("S3_INTEGRATION=0", script)

    def test_default_pinned_image_starts_isolated_fixture(self):
        result, commands, status = self.run_mock()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[0].startswith("docker:run "))
        self.assertIn(PIN, commands[0])
        self.assertEqual(commands[1], "curl:-fsS http://localhost:9000/health")
        self.assertEqual(status, "S3_INTEGRATION=1")
        self.assertNotIn("only-synthetic-password", result.stdout + result.stderr)

    def test_digest_pinned_override_allowed(self):
        alternate = "ghcr.io/rustfs/rustfs@sha256:" + "a" * 64
        result, commands, status = self.run_mock(alternate)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(alternate, commands[0])
        self.assertEqual(status, "S3_INTEGRATION=1")

    def test_mutable_tags_are_rejected_before_docker(self):
        for unpinned in ("rustfs/rustfs:latest", "rustfs/rustfs:1.0.0",
                         "rustfs/rustfs", "rustfs/rustfs@sha256:abcd"):
            with self.subTest(unpinned=unpinned):
                result, commands, status = self.run_mock(unpinned)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(commands, [])
                self.assertIsNone(status)
                self.assertIn("immutable OCI image", result.stdout + result.stderr)

    def test_malformed_registry_override_does_not_execute(self):
        for invalid in ("rustfs/rustfs@sha256:" + "G" * 64,
                        "rustfs/rustfs@sha256:" + "b" * 64 + ";echo unexpected",
                        "https://user:password@registry.invalid/rustfs@sha256:" + "a" * 64):
            with self.subTest(invalid=invalid):
                result, commands, status = self.run_mock(invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(commands, [])
                self.assertIsNone(status)

    def test_existing_unskippable_live_probe_fails_closed(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("Prove bounded Docker probe against live isolated PostgreSQL and S3", workflow)
        self.assertIn('test "' + chr(36) + '{S3_INTEGRATION:-0}" = "1" || {', workflow)
        self.assertIn("python scripts/validate_live_dependency_probe.py", workflow)
        self.assertIn("rustfs/rustfs@sha256:", workflow)


if __name__ == "__main__":
    unittest.main()
