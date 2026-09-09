#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.readiness import probe_database, probe_storage


async def run_probe() -> dict[str, str]:
    result: dict[str, str] = {}
    await probe_database()
    result["database"] = "ok"
    await probe_storage()
    result["objectStorage"] = "ok"
    result["status"] = "ready"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe deployment dependencies without printing secrets")
    parser.add_argument("--quiet", action="store_true", help="emit no success output")
    args = parser.parse_args()
    try:
        result = asyncio.run(run_probe())
    except Exception as exc:
        if not args.quiet:
            print(json.dumps({"status": "not-ready", "errorType": type(exc).__name__}, sort_keys=True))
        return 1
    if not args.quiet:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
