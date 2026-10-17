#!/usr/bin/env python3
"""Read-only pre-creation admission for a proposed VM101 offline clone.

This checks host, identity, allocation, and capacity; never reads NFS contents,
creates a guest/disk, validates approval signatures, or grants restore authority.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import re
import sys

from audit_vm101_clone_quarantine_readonly import remote

VMID = 9101
EXPECTED_SOURCE = "qemu/101"
EXPECTED_HOST = "one"
LOCAL_STORAGE = "/var/lib/vz"
REQUIRED_FREE = (2 * 160 + 64) * 1024**3
PINNED_ARCHIVE = "vzdump-qemu-101-2026_10_17-05_01_05.vma.zst"
PINNED_SHA256 = "49b05b89aef9005bc93887ef128d12c00a2d9f9b54ca8809edc56cb331a856f8"


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _obj(raw: str) -> dict:
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def _resources(raw: str) -> dict[str, tuple[int, str, str]]:
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, list) or not value:
        raise ValueError("Missing VM/LXC cluster resource inventory")
    out = {}
    seen_ids = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Malformed cluster resource record")
        kind, vmid, node, full_id = (item.get("type"), item.get("vmid"),
                                     item.get("node"), item.get("id"))
        if (kind not in ("qemu", "lxc") or type(vmid) is not int or
                not isinstance(node, str) or not isinstance(full_id, str) or
                full_id != f"{kind}/{vmid}" or full_id in out or vmid in seen_ids):
            raise ValueError("Unrecognized or duplicate VM/LXC identity")
        seen_ids.add(vmid)
        out[full_id] = (vmid, kind, node)
    return out


def assess(hostname: str, first: str, final: str, config: str,
           status: str, source_config: str, volumes: str,
           directory_probe: str, candidate: int = VMID) -> dict:
    holds = []
    available = None
    count = None
    try:
        before = _resources(first)
        after = _resources(final)
        count = len(before)
        cfg, st = _obj(config), _obj(status)
        if before != after:
            holds.append("VM/LXC inventory changed during preflight")
        if hostname.strip() != EXPECTED_HOST:
            holds.append("Wrong Proxmox host identity")
        if candidate != VMID or EXPECTED_SOURCE not in before or any(
                record[0] == candidate for record in before.values()):
            holds.append("Source/destination identity invalid or VMID occupied")
        if before.get(EXPECTED_SOURCE) != (101, "qemu", EXPECTED_HOST):
            holds.append("VM101 source not on approved Proxmox node")
        if not re.search(r"(?m)^name:\s*vitrial-prod-db\s*$", source_config):
            holds.append("Production source name mismatch")
        if not re.search(r"(?m)^scsi0:\s*\S+.*(?:^|,)size=160G(?:,|$)", source_config):
            holds.append("Production source disk shape mismatch")
        if (cfg.get("storage") != "local" or cfg.get("type") != "dir"
                or cfg.get("path") != LOCAL_STORAGE or
                "images" not in str(cfg.get("content", "")).split(",")):
            holds.append("Destination local image storage configuration mismatch")
        if (st.get("type") != "dir" or
                any(type(st.get(k)) is not int for k in ("active", "enabled", "shared")) or
                st.get("active") != 1 or st.get("enabled") != 1 or
                st.get("shared") != 0 or
                "images" not in str(st.get("content", "")).split(",")):
            holds.append("Destination storage is not active private image storage")
        available = st.get("avail")
        total, used = st.get("total"), st.get("used")
        if (any(type(x) is not int for x in (available, total, used))
                or min(available, total, used) < 0
                or available > total or used > total):
            holds.append("Storage capacity counters invalid")
            available = None
        elif available < REQUIRED_FREE:
            holds.append("Insufficient headroom for recovery plus working copy")
        if volumes.strip() != "Volid Format  Type      Size VMID":
            holds.append("Candidate already has disk allocation or volume list ambiguous")
        if directory_probe.strip() != "ABSENT_PARENT_READY":
            holds.append("Destination image directory present, symlinked or unsafe")
    except (ValueError, TypeError, AttributeError):
        holds.append("Malformed or incomplete pre-creation evidence")
    clock = datetime.now(timezone.utc)
    return {
        "gate": "HOLD",
        "technical_precreation_pass": not holds,
        "technical_holds": sorted(set(holds)),
        "node": EXPECTED_HOST, "source_vmid": 101, "candidate_vmid": candidate,
        "cluster_guest_count": count,
        "available_bytes": available, "required_free_bytes": REQUIRED_FREE,
        "destination_disk_reserved": False,
        "network_isolation_verified": False,
        "archive_basename_pinned_from_previous_review": PINNED_ARCHIVE,
        "archive_sha256_pinned_from_previous_review": PINNED_SHA256,
        "archive_bytes_verified_this_run": False,
        "dba_approval_verified": False, "data_custodian_approval_verified": False,
        "restore_authorized": False, "production_authorized": False,
        "executable_commands": [],
        "reviewed_at_utc": clock.isoformat(timespec="seconds"),
        "review_expires_at_utc": (clock + timedelta(minutes=5)).isoformat(timespec="seconds"),
    }


def capture(host: str) -> dict:
    before = remote(host, ["pvesh", "get", "/cluster/resources", "--type", "vm",
                           "--output-format", "json"])
    host_name = remote(host, ["hostname"])
    source = remote(host, ["qm", "config", "101"])
    cfg = remote(host, ["pvesh", "get", "/storage/local", "--output-format", "json"])
    st = remote(host, ["pvesh", "get", "/nodes/one/storage/local/status",
                       "--output-format", "json"])
    volumes = remote(host, ["pvesm", "list", "local", "--vmid", str(VMID)])
    probe = ("import os,sys; p='/var/lib/vz/images'; c=p+'/9101'; "
             "ok=os.path.isdir(p) and not os.path.islink(p) and os.path.realpath(p)==p; "
             "print('PARENT_UNTRUSTED' if not ok else "
             "('OCCUPIED' if os.path.lexists(c) else 'ABSENT_PARENT_READY'))")
    path_state = remote(host, ["python3", "-c", probe])
    after = remote(host, ["pvesh", "get", "/cluster/resources", "--type", "vm",
                          "--output-format", "json"])
    return assess(host_name, before, after, cfg, st, source, volumes, path_state)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--proxmox-host", required=True)
    args = p.parse_args()
    try:
        result = capture(args.proxmox_host)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError):
        result = {"gate": "HOLD", "technical_precreation_pass": False,
                  "technical_holds": ["Remote pre-creation evidence unavailable"],
                  "restore_authorized": False, "production_authorized": False,
                  "executable_commands": []}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2


if __name__ == "__main__":
    sys.exit(main())
