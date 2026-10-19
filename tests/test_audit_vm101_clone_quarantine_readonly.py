"""Negative tests: VM101 quarantine audit must never grant a restore."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_vm101_clone_quarantine_readonly.py"
spec = importlib.util.spec_from_file_location("vm101_clone_isolation", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SOURCE = "name: vitrial-prod-db\nsmbios1: uuid=source\nvmgenid: original\n"
CLONE = ("name: vitrial-pg-recovery-quarantine\nonboot: 0\nboot: order=scsi0\n"
         "scsi0: local:9101/vm-9101-disk-0.qcow2,size=160G\n"
         "smbios1: uuid=separate\nvmgenid: separate\n")
QEMU = ("VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n"
        "100 api running 8192 80 1\n101 db running 8192 160 2\n"
        "102 obj running 8192 500 3\n105 other running 8192 32 4\n"
        "111 edge running 8192 32 5\n150 k3s running 32768 200 6\n"
        "9101 quarantine stopped 8192 160 0\n")
LXC = "VMID Status Lock Name\n106 running placeholder\n"


def audit(clone=CLONE, source=SOURCE, status="status: stopped", qemu=QEMU, lxc=LXC):
    return module.assess(source, clone, status, qemu, lxc)


class QuarantineTests(unittest.TestCase):
    def test_safe_config_is_still_not_restoration_authority(self):
        x = audit()
        self.assertTrue(x["technical_isolation_shape_pass"])
        self.assertEqual(x["gate"], "HOLD")
        for key in ("restore_authorized", "production_authorized", "database_recovery_proven"):
            self.assertFalse(x[key])
        self.assertEqual(x["executable_commands"], [])

    def test_missing_vm_denied(self):
        self.assertFalse(audit(qemu=QEMU.replace("9101 quarantine stopped 8192 160 0\n", ""))["technical_isolation_shape_pass"])

    def test_any_network_adapter_denied(self):
        for field in ("net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
                      "net1: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr4",
                      "args: -netdev user,id=net0"):
            with self.subTest(field=field):
                self.assertFalse(audit(clone=CLONE+field+"\n")["technical_isolation_shape_pass"])

    def test_inherited_and_unsafe_devices_denied(self):
        for field in ("ide2: local:cloudinit,media=cdrom", "ipconfig0: ip=dhcp",
                      "sshkeys: ssh-rsa", "hookscript: local:snippets/x.pl",
                      "hostpci0: 01:00", "startup: order=10", "lock: backup",
                      "scsi1: local:101/vm-101-disk-0.qcow2,size=160G"):
            with self.subTest(field=field):
                self.assertFalse(audit(clone=CLONE+field+"\n")["technical_isolation_shape_pass"])

    def test_boot_and_onboot_strict(self):
        for clone in (CLONE.replace("onboot: 0", "onboot: 1"),
                      CLONE.replace("onboot: 0\n", ""),
                      CLONE.replace("boot: order=scsi0", "boot: order=net0")):
            self.assertFalse(audit(clone=clone)["technical_isolation_shape_pass"])

    def test_wrong_disks_denied(self):
        for clone in (CLONE.replace("local:9101/", "local:101/"),
                      CLONE.replace("size=160G", "size=80G"),
                      CLONE.replace("vm-9101-disk-0", "vm-101-disk-0")):
            self.assertFalse(audit(clone=clone)["technical_isolation_shape_pass"])

    def test_reused_guest_identity_denied(self):
        self.assertFalse(audit(clone=CLONE.replace("uuid=separate", "uuid=source"))["technical_isolation_shape_pass"])
        self.assertFalse(audit(clone=CLONE.replace("vmgenid: separate", "vmgenid: original"))["technical_isolation_shape_pass"])

    def test_running_clone_denied(self):
        self.assertFalse(audit(status="status: running")["technical_isolation_shape_pass"])

    def test_unknown_config_and_wrong_name_denied(self):
        self.assertFalse(audit(clone=CLONE+"unused0: local:other-disk\n")["technical_isolation_shape_pass"])
        self.assertFalse(audit(clone=CLONE.replace("vitrial-pg-recovery-quarantine",
                                                    "vitrial-prod-db"))["technical_isolation_shape_pass"])

    def test_malformed_or_duplicate_config_denied(self):
        for clone in (CLONE+"onboot: 0\n", CLONE+"untrusted\n", CLONE+"[snapshot]\n"):
            self.assertFalse(audit(clone=clone)["technical_isolation_shape_pass"])

    def test_qemu_lxc_conflict_denied(self):
        self.assertFalse(audit(lxc=LXC+"9101 stopped recovery\n")["technical_isolation_shape_pass"])

    def test_ssh_target_validation(self):
        with self.assertRaises(ValueError):
            module.remote("root@host; touch /tmp/unsafe", ["qm", "list"])

    def test_no_mutating_proxmox_calls(self):
        source = PATH.read_text()
        for forbidden in ('"qm", "create"', '"qm", "start"', '"qm", "set"',
                          '"qmrestore"', '"pct", "create"', '"pg_resetwal"'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
