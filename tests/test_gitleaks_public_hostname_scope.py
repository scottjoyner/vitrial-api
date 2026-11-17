"""Protect the scope of the single historical public-host Gitleaks exception.

Pure offline checks; the hosted security job independently runs real Gitleaks
against the repository's full Git history before scanning dependencies/images.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = (
    "ab3a6e8e1ad1e05b9176836915f048a8efb552f9:"
    "scripts/audit_prod_vm_health_readonly.py:generic-api-key:21"
)


class PublicHostnameGitleaksScope(unittest.TestCase):
    def test_ignore_contains_exactly_one_fingerprint(self):
        lines = (ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
        exceptions = [line.strip() for line in lines
                      if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(exceptions, [EXPECTED])
        self.assertRegex(exceptions[0], r"^[0-9a-f]{40}:[A-Za-z0-9_./-]+:[a-z0-9-]+:[0-9]+$")
        # Never let this turn into a file-level, generic-rule, or suffix wildcard.
        self.assertNotIn("*", exceptions[0])

    def test_security_job_still_scans_full_history_and_redacts(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("GITLEAKS_VERSION=8.28.0", workflow)
        self.assertIn("gitleaks detect --source . --redact --verbose --no-banner", workflow)
        self.assertIn("Dependency + filesystem vulnerability scan (trivy)", workflow)
        self.assertIn("Container image scan (trivy)", workflow)
        self.assertIn("Image SBOM (CycloneDX)", workflow)

    def test_default_rules_and_existing_fixture_scope_retained(self):
        config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
        self.assertIn("useDefault = true", config)
        self.assertIn("[allowlist]", config)
        self.assertNotIn('id = "generic-api-key"', config)
        # No new regex is being added to the global allowlist.
        regexes = re.findall(r"'''([^']+)'''", config)
        self.assertEqual(len(regexes), 4)


if __name__ == "__main__":
    unittest.main()
