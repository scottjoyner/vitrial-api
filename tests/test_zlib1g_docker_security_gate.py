"""Synthetic Dockerfile OS-patch contract tests; no Docker or apt operations.

Run the exact RUN shell command with fake apt-get/dpkg-query/rm in an isolated
temporary directory; only the host's read-only dpkg version comparator runs.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCHED = "1:1.3.dfsg+really1.3.1-1+deb13u1"
VULNERABLE = "1:1.3.dfsg+really1.3.1-1+b1"


def docker_security_script() -> str:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, value in enumerate(lines) if value.startswith("RUN set -eu;"))
    finish = next(i for i in range(start + 1, len(lines)) if not lines[i].strip())
    return "\n".join(lines[start:finish]).removeprefix("RUN ").replace("\\\n", "\n")


class Zlib1gDockerSecurityGate(unittest.TestCase):
    def test_dockerfile_requires_only_affected_package_upgrade(self):
        src = (ROOT / "Dockerfile").read_text()
        command = docker_security_script()
        self.assertTrue(src.startswith("FROM python:3.12-slim AS runtime\n"))
        self.assertIn("apt-get update;", command)
        self.assertIn("apt-get install -y --no-install-recommends --only-upgrade zlib1g;", command)
        self.assertIn("dpkg-query -W -f='${Version}' zlib1g", command)
        self.assertIn("dpkg --compare-versions", command)
        self.assertIn(PATCHED, command)
        self.assertIn("rm -rf /var/lib/apt/lists/*", command)
        self.assertLess(src.index("RUN set -eu;"), src.index("USER 10001:10001"))
        self.assertNotIn("--allow-unauthenticated", command)

    def test_security_ci_continues_to_enforce_image_findings(self):
        pipeline = (ROOT / ".github/workflows/ci.yml").read_text()
        block = pipeline.split("name: Container image scan (trivy)", 1)[1]
        block = block.split("name: Image SBOM (CycloneDX)", 1)[0]
        self.assertIn("scan-type: image", block)
        self.assertIn("exit-code: '1'", block)
        self.assertIn("severity: HIGH,CRITICAL", block)
        self.assertIn("ignore-unfixed: true", block)

    def run_mock(self, installed: str):
        with tempfile.TemporaryDirectory(prefix="vitrial-docker-zlib-mock-") as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            calls = base / "calls.log"
            for name, script in {
                "apt-get": "#!/bin/sh\necho apt-get:\"$*\" >> \"$CALL_LOG\"\nexit 0\n",
                "dpkg-query": "#!/bin/sh\nprintf %s \"$FIXTURE_VERSION\"\n",
                "rm": "#!/bin/sh\necho rm:\"$*\" >> \"$CALL_LOG\"\n",
            }.items():
                exe = bin_dir / name
                exe.write_text(script)
                exe.chmod(0o755)
            env = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "CALL_LOG": str(calls),
                "FIXTURE_VERSION": installed,
            }
            result = subprocess.run(
                ["/bin/sh", "-ec", docker_security_script()],
                capture_output=True, text=True, env=env, timeout=8,
            )
            log = calls.read_text().splitlines() if calls.exists() else []
            return result, log

    def test_stale_image_package_refuses_success(self):
        result, log = self.run_mock(VULNERABLE)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(log[0].startswith("apt-get:update"))
        self.assertTrue(log[1].startswith("apt-get:install"))
        self.assertFalse(any(entry.startswith("rm:") for entry in log))

    def test_patched_image_package_passes_with_scoped_cleanup(self):
        result, log = self.run_mock(PATCHED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(log), 3)
        self.assertIn("--only-upgrade zlib1g", log[1])
        # The shell may expand the apt list glob; the mock logs paths, not deletes.
        self.assertTrue(log[2].startswith("rm:-rf /var/lib/apt/lists/"), log[2])


if __name__ == "__main__":
    unittest.main()
