#!/usr/bin/env python3
"""Fail-closed, read-only plan for Vitrial production private-route repair.

Accept exactly one Docker inspect JSON on stdin; never print credentials,
write a file, modify Docker, or authorize a production cutover.
"""
import json
import sys
from urllib.parse import urlsplit

ROUTES = {
    "DATABASE_URL": ("10.77.0.10", "10.0.3.28", 5432,
                     {"postgresql", "postgres", "postgresql+asyncpg"}),
    "S3_ENDPOINT_URL": ("10.77.0.11", "10.0.3.29", 9000, {"http"}),
}


class CutoverPlanError(Exception):
    pass


def rewrite_endpoint(raw, key):
    if not isinstance(raw, str) or not raw or key not in ROUTES:
        raise CutoverPlanError("missing or unexpected endpoint")
    old, new, port, schemes = ROUTES[key]
    try:
        value = urlsplit(raw)
        hostname, actual_port = value.hostname, value.port
    except ValueError as exc:
        raise CutoverPlanError("invalid endpoint URL") from exc
    if (value.scheme not in schemes or hostname not in (old, new)
            or actual_port != port or value.fragment):
        raise CutoverPlanError("unapproved endpoint configuration")

    if key == "DATABASE_URL":
        if (value.username is None or value.password is None
                or not value.path or value.path == "/"):
            raise CutoverPlanError("incomplete database connection")
        # Everything up through the last @ is kept as raw encoded bytes.
        # Passwords may contain encoded separators; do not parse and re-encode.
        if "@" not in value.netloc:
            raise CutoverPlanError("missing database credentials")
        userinfo, authority = value.netloc.rsplit("@", 1)
        if authority != f"{hostname}:{port}":
            raise CutoverPlanError("unexpected PostgreSQL authority")
        netloc = userinfo + "@" + new + ":" + str(port)
    else:
        if (value.username is not None or value.password is not None
                or value.query or value.path not in ("", "/")
                or value.netloc != f"{hostname}:{port}"):
            raise CutoverPlanError("unexpected object-store authority")
        netloc = new + ":" + str(port)
    replacement = value._replace(netloc=netloc).geturl()
    again = urlsplit(replacement)
    if again.hostname != new or again.port != port:
        raise CutoverPlanError("candidate endpoint is not approved target")
    if hostname == new and replacement != raw:
        raise CutoverPlanError("already-correct endpoint would change")
    return replacement, hostname == old


def plan(container):
    if not isinstance(container, list) or len(container) != 1:
        raise CutoverPlanError("expected exactly one production container")
    config = container[0].get("Config") if isinstance(container[0], dict) else None
    raw = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(raw, list) or any(not isinstance(e, str) or "=" not in e for e in raw):
        raise CutoverPlanError("invalid Docker environment shape")
    seen = set()
    proposed = []
    changed = []

    for original in raw:
        key, value = original.split("=", 1)
        if key in ROUTES:
            if key in seen:
                raise CutoverPlanError("duplicate dependency value")
            seen.add(key)
            replacement, should_change = rewrite_endpoint(value, key)
            if should_change:
                changed.append(key)
            proposed.append(key + "=" + replacement)
        else:
            proposed.append(original)
    if seen != set(ROUTES):
        raise CutoverPlanError("missing required dependency values")
    for before, after in zip(raw, proposed):
        key = before.split("=", 1)[0]
        if key not in ROUTES and before != after:
            raise CutoverPlanError("unrelated environment modified")
    return {
        "status": "PATCH_REQUIRED" if changed else "ALREADY_ON_TARGET",
        "changes_required": sorted(changed),
        "original_environment_count": len(raw),
        "proposed_environment": proposed,  # INTERNAL ONLY: never serialize.
    }


def main():
    try:
        result = plan(json.load(sys.stdin))
        public = {k: v for k, v in result.items() if k != "proposed_environment"}
        public.update({"production_mutated": False, "secrets_printed": False,
                       "db_authenticated": False, "release_authorized": False})
    except (CutoverPlanError, ValueError, TypeError, json.JSONDecodeError):
        public = {"status": "HOLD", "reason": "invalid_private_route_evidence",
                  "production_mutated": False, "release_authorized": False}
    print(json.dumps(public, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())