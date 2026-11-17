"""Offline contract for GHSA-6q48-6c7w-9gw2 certifi lock remediation.

No external network, database, credentials, API or system package mutations.
The pinned release wheel was also independently checked against PyPI metadata.
"""
from __future__ import annotations

from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
PIN = "2026.10.23"
PYPI_WHEEL_SHA256 = "e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"
PYPI_SDIST_SHA256 = "a9895e6e2758b371e0545e321a3a8fd1fcfd9829ada8824a936dfb16aaaf36cd"


class CertifiLockSecurity(unittest.TestCase):
    def test_dev_lock_has_one_patched_certifi_pin(self):
        lines = (ROOT / "requirements-lock.txt").read_text().splitlines()
        pins = [line for line in lines if line.startswith("certifi==")]
        self.assertEqual(pins, ["certifi==" + PIN])
        self.assertGreaterEqual(tuple(map(int, PIN.split("."))), (2026, 10, 19))

    def test_uv_lock_matches_pin_and_verified_pypi_artifacts(self):
        with (ROOT / "uv.lock").open("rb") as handle:
            lock = tomllib.load(handle)
        records = [item for item in lock["package"] if item["name"] == "certifi"]
        self.assertEqual(len(records), 1)
        item = records[0]
        self.assertEqual(item["version"], PIN)
        self.assertEqual(item["source"], {"registry": "https://pypi.org/simple"})
        self.assertEqual(item["sdist"]["hash"], "sha256:" + PYPI_SDIST_SHA256)
        self.assertEqual(item["sdist"]["size"], 136225)
        self.assertIn("certifi-" + PIN + ".tar.gz", item["sdist"]["url"])
        self.assertEqual(len(item["wheels"]), 1)
        wheel = item["wheels"][0]
        self.assertEqual(wheel["hash"], "sha256:" + PYPI_WHEEL_SHA256)
        self.assertEqual(wheel["size"], 135082)
        self.assertIn("certifi-" + PIN + "-py3-none-any.whl", wheel["url"])
        for file in [item["sdist"], wheel]:
            self.assertTrue(file["url"].startswith("https://files.pythonhosted.org/"))
            self.assertRegex(file["hash"], r"^sha256:[0-9a-f]{64}$")

    def test_no_vulnerable_certifi_in_runtime_lock(self):
        # Production's different dependency closure intentionally omits certifi.
        # Do not introduce a test/dev dependency into its image to repair a scan.
        lines = (ROOT / "requirements-runtime-lock.txt").read_text().splitlines()
        self.assertFalse(any(re.match(r"^certifi[=<>]", s, re.I) for s in lines))
        self.assertNotIn("2026.7.22", (ROOT / "requirements-lock.txt").read_text())


if __name__ == "__main__":
    unittest.main()
