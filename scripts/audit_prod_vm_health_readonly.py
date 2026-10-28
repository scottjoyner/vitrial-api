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
CADDY_CONTAINER = "vitrial-api-production-caddy-1"
OBJECT_CONTAINER = "vitrial-object-s3-1"
EXPECTED_PUBLIC_SNI_HOSTNAME = "vitrial.38-143-56-142.sslip.io"


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


def private_ipv4_address(raw):
    """Parse one exact eth0 address from bounded guest iproute2 output."""
    value = guest_stdout(raw)
    if value is None:
        return None
    matches = re.findall(r"\binet\s+(10\.0\.3\.\d{1,3})/\d{1,2}\b", value)
    if len(matches) != 1:
        return None
    return matches[0] if all(int(x) <= 255 for x in matches[0].split(".")) else None


def listen_address_matches_private_ip(raw, address):
    """Only an explicit bind to the verified guest IP passes.

    A broad wildcard is not accepted as a fix to a stale production bind.
    """
    value = guest_stdout(raw)
    if address is None or value is None:
        return False
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    match = re.fullmatch(r"listen_addresses\s*=\s*'([^']+)'", lines[0])
    if not match:
        return False
    bound = [part.strip() for part in match.group(1).split(",")]
    allowed = {address, "127.0.0.1", "::1", "localhost"}
    return address in bound and len(bound) == len(set(bound)) and all(
        part in allowed for part in bound
    )


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
        "ingress_nginx_vhost_configured": False,
        "api_container_healthy": False,
        "api_caddy_host_matches_production": False,
        "postgresql_active": False,
        "postgresql_bind_matches_guest_ip": False,
        "object_store_running_with_data_bind": False,
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
        # This is an uncredentialed, read-only grep of the loaded nginx
        # configuration on the actual OPNsense guest. It emits only a fixed
        # status token, never the nginx -T content (other virtual hosts share
        # this gateway). Existing default TLS listener rejects unmatched SNI.
        nginx_probe = (
            "/usr/local/sbin/nginx -T 2>/dev/null | grep -E "
            "'^[[:space:]]*server_name[[:space:]]+"
            "vitrial[.]38-143-56-142[.]sslip[.]io([[:space:];]|$)' "
            ">/dev/null && printf 'configured\\n'"
        )
        nginx = runner(target, "qm", "guest", "exec", "111", "--",
                       "/bin/sh", "-c", nginx_probe)
        checks["ingress_nginx_vhost_configured"] = guest_stdout(nginx) == "configured"
        api = runner(target, "qm", "guest", "exec", "100", "--",
                     "/usr/bin/env", "docker", "inspect", "--format={{.State.Health.Status}}",
                     CONTAINER)
        caddy_host = runner(target, "qm", "guest", "exec", "100", "--",
                            "/usr/bin/env", "docker", "exec", CADDY_CONTAINER,
                            "printenv", "API_HOST")
        db = runner(target, "qm", "guest", "exec", "101", "--",
                    "/usr/bin/env", "systemctl", "is-active", "postgresql@17-main")
        guest_ip = runner(target, "qm", "guest", "exec", "101", "--",
                          "/usr/bin/env", "ip", "-4", "-o", "addr", "show", "dev", "eth0")
        configured_bind = runner(target, "qm", "guest", "exec", "101", "--",
                                 "/usr/bin/env", "grep", "-E",
                                 "^listen_addresses[[:space:]]*=",
                                 "/etc/postgresql/17/main/conf.d/vitrial.conf")
        checks["api_container_healthy"] = guest_stdout(api) == "healthy"
        checks["api_caddy_host_matches_production"] = (
            guest_stdout(caddy_host) == EXPECTED_PUBLIC_SNI_HOSTNAME
        )
        checks["postgresql_active"] = guest_stdout(db) == "active"
        checks["postgresql_bind_matches_guest_ip"] = listen_address_matches_private_ip(
            configured_bind, private_ipv4_address(guest_ip)
        )
        # Docker running state alone cannot prove S3 credentials, object
        # consistency, or restore durability. This limited structural witness
        # confirms the service is attached to a persistent /data bind rather
        # than container ephemeral storage. No volumes are opened or altered.
        object_mount = runner(target, "qm", "guest", "exec", "102", "--",
                              "/usr/bin/env", "docker", "inspect",
                              "--format={{.State.Running}};{{range .Mounts}}{{.Type}}:{{.Destination}};{{end}}",
                              OBJECT_CONTAINER)
        checks["object_store_running_with_data_bind"] = (
            guest_stdout(object_mount) == "true;bind:/data;"
        )
    return {
        "schema": "vitrial.production-readiness-snapshot.v1",
        "checks": checks,
        "service_health_observed": (
            checks["node_identity"] and checks["vm_inventory"]
            and checks["ingress_gateway_vm_running"]
            and checks["ingress_nginx_vhost_configured"]
            and checks["api_container_healthy"]
            and checks["api_caddy_host_matches_production"]
            and checks["postgresql_active"]
            and checks["postgresql_bind_matches_guest_ip"]
            and checks["object_store_running_with_data_bind"]
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