#!/usr/bin/env python3
"""Fail-closed, time-bounded deployment dependency probe.

The Docker healthcheck has a 10-second overall timeout. Keep database and
object-store probes isolated in separate processes so a hung thread or
connection cannot consume the other dependency's diagnostic opportunity.
No exception message, URI, path or credentials are printed by either child.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

# Two sequential checks fit inside the Docker image's 10-second health budget,
# leaving four seconds for Python startup, spawning and bounded teardown.
PER_DEPENDENCY_TIMEOUT_SECONDS = 3.0
DEPENDENCIES = ("database", "objectStorage")


def _check_dependency(name: str) -> str:
    """Run one isolated subprocess and return only a categorical result."""
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--_probe", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PER_DEPENDENCY_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "unavailable"
    return "ok" if proc.returncode == 0 else "unavailable"


def run_probe() -> dict[str, str]:
    # Always attempt both independently. An absent database must not make S3
    # health unknowable; both must be OK before Docker may report healthy.
    result = {name: _check_dependency(name) for name in DEPENDENCIES}
    result["status"] = (
        "ready" if all(result[name] == "ok" for name in DEPENDENCIES)
        else "not-ready"
    )
    return result


def _internal_probe(name: str) -> int:
    """Child-only execution, deliberately without printing exception details."""
    try:
        from app.readiness import probe_database, probe_storage

        if name == "database":
            asyncio.run(probe_database())
        elif name == "objectStorage":
            asyncio.run(probe_storage())
        else:
            return 1
    except Exception:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe deployment dependencies without printing secrets")
    parser.add_argument("--quiet", action="store_true", help="emit no success output")
    parser.add_argument("--_probe", choices=DEPENDENCIES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._probe:
        return _internal_probe(args._probe)

    result = run_probe()
    if not args.quiet:
        print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
