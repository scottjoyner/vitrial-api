#!/usr/bin/env python3
"""Offline committed-file contract: zlib research note <-> Dockerfile/CI (R21).

Reads HEAD through local `git show` only. No network, credentials, apt/Docker,
service, database, model load, or working-tree file is used. This complements
tests/test_zlib1g_docker_security_gate.py (which exercises the Dockerfile script)
by pinning that the research narrative and the committed build/CI files still
describe the same patch and the same release holds.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = "docs/research/ZLIB1G_DEBIAN13_IMAGE_FIX_20261116.md"
DOCKERFILE = "Dockerfile"
CI = ".github/workflows/ci.yml"
GATE_TEST = "tests/test_zlib1g_docker_security_gate.py"
CVE = "CVE-2026-85091"
PATCHED = "1:1.3.dfsg+really1.3.1-1+deb13u1"
VULNERABLE = "1:1.3.dfsg+really1.3.1-1+b1"
CREDENTIAL_SHAPED = re.compile(
    r"https?://[^\s/@:]+:[^\s/@]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}"
)


def git_text(*args: str) -> str:
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
    result = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, env=env, timeout=15,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def committed(path: str) -> str:
    return git_text("show", f"HEAD:{path}")


def patch_contract_errors(doc: str, dockerfile: str) -> list[str]:
    """Fail-closed check that the note and the Dockerfile pin the same fix."""
    errors = []
    for needle in (CVE, PATCHED, VULNERABLE, DOCKERFILE, GATE_TEST):
        if needle not in doc:
            errors.append(f"research note lost pinned reference: {needle}")
    for needle in (
        "FROM python:3.12-slim AS runtime",
        "apt-get update;",
        "apt-get install -y --no-install-recommends --only-upgrade zlib1g;",
        "dpkg-query -W -f='${Version}' zlib1g",
        "dpkg --compare-versions",
        PATCHED,
        "rm -rf /var/lib/apt/lists/*",
        "USER 10001:10001",
    ):
        if needle not in dockerfile:
            errors.append(f"Dockerfile lost pinned build step: {needle}")
    if "--allow-unauthenticated" in dockerfile:
        errors.append("Dockerfile allows unauthenticated apt")
    if "NO PRODUCTION DEPLOYMENT" not in doc or "Release and review holds" not in doc:
        errors.append("research-only / release-hold disclosure missing")
    return errors


class CommittedFileContractR21(unittest.TestCase):
    def test_pinned_files_are_committed(self):
        tracked = set(git_text("ls-files").splitlines())
        for path in (DOC, DOCKERFILE, CI, GATE_TEST):
            self.assertIn(path, tracked, f"not committed at HEAD: {path}")
            self.assertTrue(committed(path).strip(), f"empty committed file: {path}")

    def test_research_note_and_dockerfile_pin_same_patch(self):
        errors = patch_contract_errors(committed(DOC), committed(DOCKERFILE))
        self.assertEqual(errors, [], "; ".join(errors))

    def test_ci_image_scan_still_enforces_high_critical(self):
        pipeline = committed(CI)
        start = pipeline.split("name: Container image scan (trivy)", 1)
        self.assertEqual(len(start), 2, "Trivy image gate disappeared from CI")
        block = start[1].split("name: Image SBOM (CycloneDX)", 1)[0]
        for needle in ("scan-type: image", "exit-code: '1'", "severity: HIGH,CRITICAL"):
            self.assertIn(needle, block, f"image gate weakened: {needle}")

    def test_committed_text_has_no_credential_shaped_literal(self):
        for path in (DOC, DOCKERFILE, GATE_TEST):
            match = CREDENTIAL_SHAPED.search(committed(path))
            self.assertIsNone(match, f"credential-shaped literal in {path}: {match}")

    def test_validators_detect_planted_drift(self):
        doc, dockerfile = committed(DOC), committed(DOCKERFILE)
        self.assertEqual(patch_contract_errors(doc, dockerfile), [])
        self.assertTrue(any(
            "pinned reference" in e
            for e in patch_contract_errors(doc.replace(CVE, "CVE-1999-0001"), dockerfile)
        ))
        self.assertTrue(any(
            "build step" in e
            for e in patch_contract_errors(doc, dockerfile.replace("--only-upgrade ", ""))
        ))
        self.assertTrue(CREDENTIAL_SHAPED.search(
            "image: https://user:token@example.invalid/path"
        ))
        self.assertIsNone(CREDENTIAL_SHAPED.search("https://security-tracker.debian.org/tracker/" + CVE))


if __name__ == "__main__":
    unittest.main()
