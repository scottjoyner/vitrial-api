#!/usr/bin/env python3
"""Read-only, fail-closed VM101 PostgreSQL client CIDR repair rehearsal.

Never edits pg_hba.conf, authenticates, restarts PostgreSQL, or grants release
approval. Preserves every source byte except a single reviewed /32 CIDR.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

HBA = Path("/etc/postgresql/17/main/pg_hba.conf")
OLD = b"10.77.0.2/32"
NEW = b"10.0.3.27/32"
REQUIRED = (b"host", b"vitrial", b"vitrial", OLD, b"scram-sha-256")


class Hold(ValueError):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def plan(raw):
    if not isinstance(raw, bytes) or len(raw) > 65536:
        raise Hold("invalid file")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Hold("bad encoding") from exc
    lines = raw.splitlines(keepends=True)
    candidates = []
    for idx, line in enumerate(lines):
        active = line.split(b"#", 1)[0].split()
        if not active:
            continue
        if len(active) >= 4 and active[0].startswith(b"host"):
            if active[3] in (b"0.0.0.0/0", b"::/0", b"all"):
                raise Hold("unexpected public-wide database access")
            if active[3] == NEW:
                raise Hold("target CIDR already exists, avoid duplicated grants")
            if active[3] == OLD:
                if tuple(active) != REQUIRED:
                    raise Hold("old CIDR used in unexpected auth rule")
                if line.count(OLD) != 1:
                    raise Hold("ambiguous source line")
                candidates.append(idx)
    if len(candidates) != 1:
        raise Hold("expected exactly one legacy API host rule")
    i = candidates[0]
    proposed = list(lines)
    proposed[i] = lines[i].replace(OLD, NEW, 1)
    candidate = b"".join(proposed)
    if candidate == raw or proposed[i].replace(NEW, OLD, 1) != lines[i]:
        raise Hold("candidate invalid")
    return {
        "status": "CANDIDATE_ONLY_HOLD",
        "current_sha256": sha(raw),
        "candidate_sha256": sha(candidate),
        "candidate_bytes": candidate,
        "old_cidr": OLD.decode(),
        "new_cidr": NEW.decode(),
        "changed_rules": 1,
    }


def safe_read(path=HBA):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
        raise Hold("unexpected config file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        first = os.fstat(fd)
        raw = os.read(fd, 65537)
        last = os.fstat(fd)
        if len(raw) != st.st_size or (
            first.st_ino, first.st_mtime_ns, first.st_size
        ) != (
            last.st_ino, last.st_mtime_ns, last.st_size
        ):
            raise Hold("source file changed during read")
        return raw
    finally:
        os.close(fd)


def main():
    try:
        result = plan(safe_read())
        result.pop("candidate_bytes")
        result.update({
            "configuration_applied": False,
            "postgres_restarted": False,
            "production_modified": False,
            "release_authorized": False,
        })
    except (Hold, OSError, TypeError, ValueError):
        result = {
            "status": "HOLD", "reason": "hba_source_or_policy_mismatch",
            "configuration_applied": False, "release_authorized": False,
            "production_modified": False,
        }
    print(json.dumps(result, sort_keys=True))
    return 2


if __name__ == "__main__":
    sys.exit(main())