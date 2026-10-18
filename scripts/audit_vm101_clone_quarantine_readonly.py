#!/usr/bin/env python3
"""Read-only, pre-boot quarantine audit of a proposed VM101 recovery clone.

Never creates/starts guests, reads database contents, or grants restore authority.
All returned data is allowlisted; raw qm configurations can contain credentials.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from typing import Any

PROTECTED = frozenset({100, 101, 102, 105, 111, 150})
VMID = 9101
SOURCE = 101
DISK = re.compile(r"^local:9101/vm-9101-disk-0\.(?:qcow2|raw)(?:,|$)")
DANGEROUS = re.compile(
    r"^(?:net\d+|ipconfig\d+|ide\d+|sata\d+|virtio\d+|"
    r"hostpci\d+|usb\d+|serial\d+|parallel\d+|"
    r"hookscript|args|sshkeys|ciuser|cicustom|cipassword|"
    r"nameserver|searchdomain|startup|lock)$"
)


def config_fields(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z][a-z0-9]*):\s*(.*)", line)
        if not match or match.group(1) in result:
            raise ValueError("Malformed or duplicate QEMU configuration field")
        result[match.group(1)] = match.group(2)
    return result


def inventory_ids(raw: str, expected: str) -> set[int]:
    lines = [x for x in raw.splitlines() if x.strip()]
    if not lines or not lines[0].strip().startswith(expected):
        raise ValueError("Unexpected guest inventory header")
    ids: set[int] = set()
    for line in lines[1:]:
        match = re.match(r"^\s*(\d+)\s+", line)
        if not match:
            raise ValueError("Unexpected guest inventory row")
        key = int(match.group(1))
        if key in ids:
            raise ValueError("Duplicate guest identity")
        ids.add(key)
    return ids


def assess(source_config: str, clone_config: str, clone_status: str,
           qemu_list: str, lxc_list: str, candidate: int = VMID) -> dict[str, Any]:
    holds: list[str] = []
    try:
        qemu = inventory_ids(qemu_list, "VMID")
        lxc = inventory_ids(lxc_list, "VMID")
        original = config_fields(source_config)
        clone = config_fields(clone_config)
        if candidate != VMID or candidate in PROTECTED:
            holds.append("Unexpected or protected recovery VMID")
        if SOURCE not in qemu or candidate not in qemu or candidate in lxc:
            holds.append("Production or recovery guest identity mismatch")
        if qemu.intersection(lxc):
            holds.append("QEMU/LXC ID collision")
        if original.get("name") != "vitrial-prod-db":
            holds.append("Source VM identity mismatch")
        if clone.get("name") != "vitrial-pg-recovery-quarantine":
            holds.append("Recovery clone lacks the designated quarantine identity")
        if clone_status.strip() != "status: stopped":
            holds.append("Recovery clone must be stopped before first boot")
        if clone.get("onboot") != "0":
            holds.append("Recovery clone autostart is not explicitly disabled")
        if clone.get("boot") != "order=scsi0":
            holds.append("Recovery boot order is not restricted")
        if not DISK.match(clone.get("scsi0", "")) or "size=160G" not in clone.get("scsi0", "").split(","):
            holds.append("Recovery disk is not the expected separate local 160G disk")
        if any(DANGEROUS.fullmatch(k) for k in clone):
            holds.append("Inherited NIC, cloud-init, passthrough, startup or hook is present")
        allowed = {"name", "onboot", "boot", "scsi0", "memory", "cores",
                   "sockets", "cpu", "ostype", "scsihw", "agent", "balloon",
                   "vga", "bootdisk", "numa", "tags", "vmgenid", "smbios1",
                   "description", "bios"}
        if set(clone).difference(allowed):
            holds.append("Unreviewed QEMU configuration directive")
        if any(re.fullmatch(r"scsi\d+", k) and k != "scsi0" for k in clone):
            holds.append("Unexpected additional SCSI disk")
        for key in ("smbios1", "vmgenid"):
            if key in original and clone.get(key) == original[key]:
                holds.append("Inherited production guest hardware identity")
        if any(re.search(r"\blocal:101/|\bvm-101-disk-", val)
               for val in clone.values()):
            holds.append("Recovery configuration references production storage")
    except (TypeError, ValueError):
        holds.append("Malformed or missing live VM inventory/configuration")
    return {
        "source_vmid": SOURCE,
        "candidate_vmid": candidate,
        "gate": "HOLD",
        "technical_isolation_shape_pass": not holds,
        "technical_holds": sorted(set(holds)),
        "live_disk_read": False,
        "database_recovery_proven": False,
        "independent_dba_approved": False,
        "restore_authorized": False,
        "production_authorized": False,
        "executable_commands": [],
    }


def remote(host: str, command: list[str]) -> str:
    if not re.fullmatch(r"root@[A-Za-z0-9.-]{3,100}", host):
        raise ValueError("Invalid fixed SSH target")
    try:
        result = subprocess.run(
            ["ssh", "-T", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
             "-oConnectTimeout=5", host, shlex.join(command)],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Read-only Proxmox query failed") from exc
    if result.returncode or len(result.stdout) > 32768:
        raise RuntimeError("Read-only Proxmox query failed")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxmox-host", required=True)
    args = parser.parse_args()
    try:
        qemu = remote(args.proxmox_host, ["qm", "list"])
        lxc = remote(args.proxmox_host, ["pct", "list"])
        source = remote(args.proxmox_host, ["qm", "config", str(SOURCE)])
        clone = remote(args.proxmox_host, ["qm", "config", str(VMID)])
        status = remote(args.proxmox_host, ["qm", "status", str(VMID)])
        result = assess(source, clone, status, qemu, lxc)
    except (RuntimeError, ValueError):
        result = {"candidate_vmid": VMID, "gate": "HOLD",
                  "technical_isolation_shape_pass": False,
                  "technical_holds": ["Read-only recovery clone evidence unavailable"],
                  "restore_authorized": False, "production_authorized": False,
                  "executable_commands": []}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
