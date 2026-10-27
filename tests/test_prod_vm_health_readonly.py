"""Synthetic, network-free regression tests for SCRUM-39 VM health probe."""
import importlib.util
import json
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "audit_prod_vm_health_readonly.py"
spec = importlib.util.spec_from_file_location("prod_vm_health", MODULE)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)

INVENTORY = """      VMID NAME                 STATUS     MEM(MB)  BOOTDISK(GB) PID
       100 vitrial-prod-api     running    8192     80.00        1
       101 vitrial-prod-db      running    8192     160.00       2
       102 vitrial-prod-obj     running    8192     500.00       3
       111 opnsense             running    8192     32.00        4
"""


def response(value, code=0):
    return json.dumps({"exitcode": code, "exited": 1, "out-data": value})


def mocked_runner(*, node="one", inventory=INVENTORY, api="unhealthy", db="failed",
                  private_ip="10.0.3.28", listen_ip="10.77.0.10",
                  caddy_host="vitrial.38-143-56-139.sslip.io"):
    invocations = []

    def call(target, *args):
        invocations.append(args)
        if args == ("hostname",):
            return node + "\n"
        if args == ("qm", "list"):
            return inventory
        if args[:4] == ("qm", "guest", "exec", "100"):
            if "inspect" in args:
                return response(api)
            if "printenv" in args and "API_HOST" in args:
                return response(caddy_host)
            raise AssertionError("unknown API guest action: " + repr(args))
        if args[:4] == ("qm", "guest", "exec", "101"):
            if "systemctl" in args:
                return response(db, 0 if db == "active" else 3)
            if "ip" in args:
                return response(f"2: eth0 inet {private_ip}/24 brd 10.0.3.255 scope global eth0")
            if "grep" in args:
                return response(f"listen_addresses = '{listen_ip}'")
        raise AssertionError("unknown action: " + repr(args))
    return call, invocations


class ProdVmHealthTests(unittest.TestCase):
    def test_realistic_failing_state_is_hold(self):
        runner, calls = mocked_runner()
        result = health.snapshot("root@pve.example", "one", runner)
        self.assertEqual(result["gate"], "HOLD")
        self.assertFalse(result["checks"]["api_container_healthy"])
        self.assertTrue(result["checks"]["ingress_gateway_vm_running"])
        self.assertFalse(result["checks"]["postgresql_active"])
        self.assertFalse(result["release_authorized"])
        self.assertFalse(result["checks"]["postgresql_bind_matches_guest_ip"])
        self.assertFalse(result["checks"]["api_caddy_host_matches_production"])
        self.assertEqual(len(calls), 7)

    def test_fully_healthy_services_still_cannot_approve_release(self):
        runner, _ = mocked_runner(
            api="healthy", db="active", listen_ip="10.0.3.28",
            caddy_host=health.EXPECTED_PUBLIC_SNI_HOSTNAME
        )
        result = health.snapshot("root@pve.example", "one", runner)
        self.assertTrue(result["service_health_observed"])
        self.assertFalse(result["release_authorized"])
        self.assertFalse(result["checks"]["independent_restore_verified"])

    def test_healthy_api_with_retired_caddy_host_is_still_hold(self):
        runner, _ = mocked_runner(
            api="healthy", db="active", listen_ip="10.0.3.28",
            caddy_host="vitrial.38-143-56-139.sslip.io"
        )
        report = health.snapshot("root@pve.example", "one", runner)
        self.assertFalse(report["checks"]["api_caddy_host_matches_production"])
        self.assertFalse(report["service_health_observed"])

    def test_caddy_hostname_requires_exact_approved_value(self):
        for caddy_host in ("", "vitrial.38-143-56-139.sslip.io",
                           "vitrial.38-143-56-142.sslip.io.evil.example",
                           "https://vitrial.38-143-56-142.sslip.io",
                           "VITRIAL.38-143-56-142.sslip.io"):
            runner, _ = mocked_runner(api="healthy", db="active",
                                      listen_ip="10.0.3.28", caddy_host=caddy_host)
            self.assertFalse(health.snapshot("root@pve.example", "one", runner)[
                "checks"]["api_caddy_host_matches_production"])

    def test_bind_drift_blocks_even_healthy_service(self):
        runner, _ = mocked_runner(api="healthy", db="active", listen_ip="10.77.0.10")
        result = health.snapshot("root@pve.example", "one", runner)
        self.assertFalse(result["service_health_observed"])
        self.assertFalse(result["checks"]["postgresql_bind_matches_guest_ip"])

    def test_malformed_or_wildcard_bind_fails_closed(self):
        for bound in ("*", "0.0.0.0", "10.0.3.280", "10.0.3.28.example",
                      "10.0.3.28,*", "10.0.3.28,0.0.0.0", "10.0.3.28,10.0.3.1"):
            runner, _ = mocked_runner(api="healthy", db="active", listen_ip=bound)
            self.assertFalse(health.snapshot("root@pve.example", "one", runner)[
                "checks"]["postgresql_bind_matches_guest_ip"])

    def test_missing_guest_address_fails_closed(self):
        runner, _ = mocked_runner(api="healthy", db="active", private_ip="127.0.0.1", listen_ip="10.0.3.28")
        self.assertFalse(health.snapshot("root@pve.example", "one", runner)[
            "checks"]["postgresql_bind_matches_guest_ip"])

    def test_wrong_pve_node_does_not_probe_guests(self):
        runner, calls = mocked_runner(node="unknown")
        self.assertFalse(health.snapshot("root@pve.example", "one", runner)["service_health_observed"])
        self.assertEqual(calls, [("hostname",)])

    def test_incomplete_or_stopped_inventory_is_fail_closed(self):
        for content in (INVENTORY.replace("running", "stopped", 1),
                        INVENTORY.replace("102 vitrial-prod-obj", "102 wrong-obj"),
                        INVENTORY + "101 vitrial-prod-db running\n",
                        INVENTORY.replace("111 opnsense", "111 wrong-gateway"),
                        INVENTORY.replace("111 opnsense             running", "111 opnsense             stopped"),
                        ""):
            runner, calls = mocked_runner(inventory=content)
            self.assertFalse(health.snapshot("root@pve.example", "one", runner)["checks"]["vm_inventory"])
            self.assertEqual(len(calls), 2)

    def test_guest_payload_rejects_errors_partial_or_malformed(self):
        for payload in (None, "", "{}", response("healthy", code=1),
                        json.dumps({"exited": 0, "exitcode": 0, "out-data": "healthy"}),
                        json.dumps({"exited": 1, "exitcode": 0, "out-data": 2})):
            self.assertIsNone(health.guest_stdout(payload))

    def test_unrecognized_health_result_is_denied(self):
        runner, _ = mocked_runner(api="starting", db="activating")
        result = health.snapshot("root@pve.example", "one", runner)
        self.assertFalse(result["service_health_observed"])

    def test_target_validation_disallows_shell_metacharacters(self):
        for target in ("root@pve.example;id", "pve.example", "root@pve.example\nx", ""):
            with self.assertRaises(ValueError):
                health.snapshot(target, "one")
        with self.assertRaises(ValueError):
            health.snapshot("root@pve.example", "one;id")

    def test_missing_remote_response_is_hold(self):
        runner, _ = mocked_runner(node="")
        self.assertFalse(health.snapshot("root@pve.example", "one", runner)["checks"]["node_identity"])


if __name__ == "__main__":
    unittest.main()