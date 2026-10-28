#!/usr/bin/env python3
"""Read-only fail-closed VM101 PostgreSQL listener/archive cutover rehearsal.

No configuration writes, DB startup, network changes or secret output.
Reports only exact source/proposed SHA256 values for an operator-reviewed
atomic two-file cutover after independent backup and restore acceptance.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

CONF = Path("/etc/postgresql/17/main/conf.d/vitrial.conf")
AUTO = Path("/var/lib/postgresql/17/main/postgresql.auto.conf")
ARCHIVER = Path("/usr/local/sbin/vitrial-pg-archive-wal-v2")
VERIFIED_V2 = "7bb87fc7ec78d789243e8a38b80d35b44c9457d079238fe71635963703741b5e"
LEGACY = "'/usr/local/sbin/vitrial-pg-archive-wal %p %f'"
SAFE = "'/usr/local/sbin/vitrial-pg-archive-wal-v2 %p %f'"
OLD_LISTENER = "'10.77.0.10'"
NEW_LISTENER = "'10.0.3.28'"


class Hold(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def allowed_replace(raw, key, original, desired):
    try:
        contents = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Hold("invalid config encoding") from exc
    active = re.findall(r"^[ \\t]*" + re.escape(key) + r"[ \\t]*=", contents, re.M)
    if len(active) != 1:
        raise Hold("missing or ambiguous configuration setting")
    pattern = (r"^([ \\t]*" + re.escape(key) + r"[ \\t]*=[ \\t]*)"
               + re.escape(original) + r"([ \\t]*(?:#.*)?)$")
    result, count = re.subn(pattern, lambda m: m.group(1) + desired + m.group(2), contents,
                            flags=re.M)
    if count != 1 or result == contents:
        raise Hold("unexpected configuration value")
    return result.encode("utf-8")


def plan(conf, auto, archiver):
    if digest(archiver) != VERIFIED_V2:
        raise Hold("unreviewed WAL archiver code")
    if re.search(rb"^[ \\t]*listen_addresses[ \\t]*=", auto, re.M):
        raise Hold("listener overridden by auto configuration")

    if re.search(rb"^[ \\t]*archive_command[ \\t]*=", conf, re.M):
        raise Hold("unexpected archive command in listener config")
    if len(re.findall(rb"^[ \\t]*archive_mode[ \\t]*=", auto, re.M)) != 1:
        raise Hold("ambiguous archive_mode")
    if not re.search(rb"^[ \\t]*archive_mode[ \\t]*=[ \\t]*'on'[ \\t]*(?:#.*)?$", auto, re.M):
        raise Hold("archive_mode is not on")
    conf_new = allowed_replace(conf, "listen_addresses", OLD_LISTENER, NEW_LISTENER)
    auto_new = allowed_replace(auto, "archive_command", LEGACY, SAFE)
    return {
        "status": "CANDIDATE_ONLY_HOLD",
        "listener_current_sha256": digest(conf),
        "listener_candidate_sha256": digest(conf_new),
        "auto_current_sha256": digest(auto),
        "auto_candidate_sha256": digest(auto_new),
        "wal_archiver_verified_sha256": digest(archiver),
        "changed_files": 2,
        "production_modified": False,
        "postgres_restarted": False,
        "sql_restore_accepted": False,
        "configuration_applied": False,
        "release_authorized": False,
    }


def safe_read(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > 32768:
        raise Hold("configuration file type or size refused")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        initial = os.fstat(fd)
        data = os.read(fd, 32769)
        final = os.fstat(fd)
        if (len(data) != initial.st_size or initial.st_size != final.st_size
                or initial.st_mtime_ns != final.st_mtime_ns
                or initial.st_ctime_ns != final.st_ctime_ns):
            raise Hold("source changed during read")
        return data
    finally:
        os.close(fd)


def main():
    try:
        result = plan(safe_read(CONF), safe_read(AUTO), safe_read(ARCHIVER))
    except (Hold, OSError, ValueError, TypeError):
        result = {"status": "HOLD", "reason": "unverified_configuration_or_archive",
                  "production_modified": False, "release_authorized": False}
    print(json.dumps(result, sort_keys=True))
    return 2


if __name__ == "__main__":
    sys.exit(main())