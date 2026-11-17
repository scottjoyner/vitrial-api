#!/usr/bin/env python3
"""Read-only, bounded acceptance of the actual Docker health probe.

Only run against the isolated PostgreSQL/S3 services in the GitHub postgres
integration job. No production, migrations, bucket writes or secret output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_dependencies.py"
# Docker allows ten seconds for the *whole* script, not each dependency.
DOCKER_BUDGET_SECONDS = 10.0
CASE_TIMEOUT_SECONDS = 9.5
LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))
FIELDS = frozenset(("database", "objectStorage", "status"))


def require_isolated_services(env: dict[str, str]) -> bool:
    """No remote hosts, user databases or production buckets are admissible."""
    try:
        if env.get("GITHUB_ACTIONS") != "true":
            return False
        if not env.get("GITHUB_RUN_ID", "").isdigit():
            return False
        if env.get("POSTGRES_INTEGRATION") != "1" or env.get("S3_INTEGRATION") != "1":
            return False
        if env.get("EVIDENCE_STORAGE_PROVIDER") != "s3":
            return False
        if env.get("S3_BUCKET") != "vitrial-evidence":
            return False
        db = urlsplit(env["DATABASE_URL"])
        storage = urlsplit(env["S3_ENDPOINT_URL"])
        return (
            db.scheme == "postgresql+asyncpg"
            and db.hostname in LOCAL_HOSTS and db.port == 5432
            and storage.scheme == "http"
            and storage.hostname in LOCAL_HOSTS and storage.port == 9000
            and not storage.username and not storage.password
            and not storage.path.strip("/") and not storage.query
        )
    except (KeyError, ValueError):
        return False


def run_case(name: str, overrides: dict[str, str], expected: dict[str, str],
             env: dict[str, str]) -> float:
    effective = {**env, **overrides}
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(PROBE)],
            cwd=ROOT,
            env=effective,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=CASE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"{name}: health probe exceeded runtime budget or could not launch") from None

    elapsed = time.monotonic() - started
    if elapsed >= DOCKER_BUDGET_SECONDS:
        raise RuntimeError(f"{name}: Docker's ten-second health budget exceeded")
    if result.stderr.strip():
        raise RuntimeError(f"{name}: health probe emitted unexpected diagnostics")
    try:
        actual = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(f"{name}: no valid categorical JSON result") from None

    if not isinstance(actual, dict) or set(actual) != FIELDS:
        raise RuntimeError(f"{name}: health probe emitted unapproved fields")
    if any(not isinstance(v, str) for v in actual.values()):
        raise RuntimeError(f"{name}: health probe returned noncategorical fields")
    if actual != expected:
        raise RuntimeError(f"{name}: dependency readiness differs from expected categories")
    expected_exit = 0 if expected["status"] == "ready" else 1
    if result.returncode != expected_exit:
        raise RuntimeError(f"{name}: health probe exit status violates readiness contract")
    return elapsed


def main() -> int:
    env = dict(os.environ)
    if not require_isolated_services(env):
        print("HOLD: live health-probe acceptance requires isolated GitHub PostgreSQL+S3 fixtures",
              file=sys.stderr)
        return 2

    cases = (
        ("both-healthy", {},
         {"database": "ok", "objectStorage": "ok", "status": "ready"}),
        ("database-unavailable", {"DATABASE_URL": "postgresql+asyncpg://probe@127.0.0.1:1/unreachable"},
         {"database": "unavailable", "objectStorage": "ok", "status": "not-ready"}),
        ("storage-unavailable", {"S3_ENDPOINT_URL": "http://127.0.0.1:1"},
         {"database": "ok", "objectStorage": "unavailable", "status": "not-ready"}),
    )
    try:
        times = [run_case(name, overrides, expected, env)
                 for name, overrides, expected in cases]
    except RuntimeError as exc:
        # Only fixed contract messages; no URL, exception detail or credentials.
        print(f"HOLD: {exc}", file=sys.stderr)
        return 1
    print("PASS: isolated live PostgreSQL + S3 Docker probe, both negative dependency cases, "
          f"all three under {DOCKER_BUDGET_SECONDS:g}s (max {max(times):.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
