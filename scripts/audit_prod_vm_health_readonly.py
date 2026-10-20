#!/usr/bin/env python3
"""SCRUM-39: fail-closed, read-only production VM health snapshot.

This tool NEVER authorizes recovery, API deploys, or TestFlight. It cannot
validate backups, S3 credentials, human approvals, or phone acceptance.
"""
import argparse
import json
import re
import subprocess
import sys

VM_IDS = {
    100: "vitrial-prod-api",
    101: "vitrial-prod-db",
    102: "vitrial-prod-obj",
    111: "opnsense",
}
CONTAINER = "vitrial-api-production-api-1"


def valid_target(target):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", target))


def ssh_readonly(target, *argv):
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
             "-o", "ConnectTimeout=4", target, *argv],
            capture_output=True, text=True, timeout=12, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 32768:
            return None
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def guest_stdout(payload):
    """Only accept successful, completed, bounded QEMU-guest-agent results."""
    try:
        record = json.loads(payload or "")
        if (type(record) is not dict or record.get("exited") != 1
                or record.get("exitcode") != 0):
            return None
        output = record.get("out-data")
        if not isinstance(output, str) or len(output) > 1024:
            return None
        return output.strip()
    except (ValueError, TypeError):
        return None


def inventory_valid(data):
    if not isinstance(data, str):
        return False
    entries = {}
    for line in data.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[0].isdigit():
            vmid = int(fields[0])
            if vmid in VM_IDS:
                if vmid in entries:
                    return False
                entries[vmid] = (fields[1], fields[2])
    return entries == {vmid: (name, "running") for vmid, name in VM_IDS.items()}


def snapshot(target, node, runner=ssh_readonly):
    if not valid_target(target) or not re.fullmatch(r"[A-Za-z0-9_.-]+", node):
        raise ValueError("invalid SSH target or expected node")
    checks = {
        "node_identity": False,
        "vm_inventory": False,
        "ingress_gateway_vm_running": False,
        "api_container_healthy": False,
        "postgresql_active": False,
        "object_store_authenticated": False,
        "independent_restore_verified": False,
        "physical_testflight_accepted": False,
    }
    remote_node = runner(target, "hostname")
    checks["node_identity"] = remote_node is not None and remote_node.strip() == node
    if checks["node_identity"]:
        checks["vm_inventory"] = inventory_valid(runner(target, "qm", "list"))
        checks["ingress_gateway_vm_running"] = checks["vm_inventory"]
    if checks["vm_inventory"]:
        api = runner(target, "qm", "guest", "exec", "100", "--",
                     "/usr/bin/env", "docker", "inspect", "--format={{.State.Health.Status}}",
                     CONTAINER)
        db = runner(target, "qm", "guest", "exec", "101", "--",
                    "/usr/bin/env", "systemctl", "is-active", "postgresql@17-main")
        checks["api_container_healthy"] = guest_stdout(api) == "healthy"
        checks["postgresql_active"] = guest_stdout(db) == "active"
    return {
        "schema": "vitrial.production-readiness-snapshot.v1",
        "checks": checks,
        "service_health_observed": (
            checks["node_identity"] and checks["vm_inventory"]
            and checks["ingress_gateway_vm_running"]
            and checks["api_container_healthy"] and checks["postgresql_active"]
        ),
        "release_authorized": False,
        "gate": "HOLD",
        "reason": "independent restore, authenticated S3 and physical iOS release evidence required",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxmox-host", required=True, help="SSH user@hostname")
    parser.add_argument("--expected-node", required=True, help="Pinned PVE node hostname")
    args = parser.parse_args(argv)
    try:
        report = snapshot(args.proxmox_host, args.expected_node)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True, indent=2))
    return 2  # Inspection is never a production deployment approval.


if __name__ == "__main__":
    sys.exit(main())