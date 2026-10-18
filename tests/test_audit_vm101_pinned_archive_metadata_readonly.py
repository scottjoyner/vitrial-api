"""Fail-closed archive metadata admission and production inheritance checks."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0,str(BASE))
spec=importlib.util.spec_from_file_location("archive",BASE/"audit_vm101_pinned_archive_metadata_readonly.py")
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
GOOD={"nfs4_mount_confirmed":True,"archive_file_regular":True,
 "compressed_bytes":555108969,"archive_link_count":1,
 "archive_other_read_bit_set":True,
 "vm_name_matches":True,"source_disk_matches":True,
 "scsi_capacity_matches":True,"single_expected_vma_device":True,
 "vma_header_and_config_present":True,
 "unknown_directive_count":0,"duplicate_directive_count":0,
 "inherited_network_count":2,"inherited_onboot":True,
 "inherited_startup_policy":True,"inherited_bootstrap_settings":True,
 "inherited_hardware_identity":True,"inherited_cloudinit_media":False}

class ArchiveMetadataTests(unittest.TestCase):
    def test_pinned_archive_shape_does_not_authorize_boot(self):
        r=mod.assess(GOOD,"one")
        self.assertTrue(r["technical_archive_metadata_pass"])
        self.assertEqual(r["inherited_production_nic_count"],2)
        self.assertTrue(r["clone_config_scrubbing_required"])
        self.assertFalse(r["archived_config_safe_to_boot"])
        self.assertTrue(r["archive_file_metadata_read_now"])
        self.assertTrue(r["archive_other_read_bit_set"])
        self.assertFalse(r["nfs_access_controls_independently_verified"])
        self.assertFalse(r["archive_permissions_custodian_approved"])
        for key in ("archive_full_sha256_rechecked_now",
                    "archive_compressed_payload_verified_now",
                    "archive_postgres_consistency_verified",
                    "disk_blocks_extracted","restore_authorized",
                    "production_authorized"):
            self.assertFalse(r[key])
        self.assertEqual(r["gate"],"HOLD")
        self.assertEqual(r["executable_commands"],[])
    def test_unexpected_file_and_vma_metadata_denied(self):
        cases=(("nfs4_mount_confirmed",False),("archive_file_regular",False),
               ("compressed_bytes",555108968),("compressed_bytes",True),
               ("archive_link_count",2),("vm_name_matches",False),
               ("source_disk_matches",False),("scsi_capacity_matches",False),
               ("single_expected_vma_device",False),
               ("vma_header_and_config_present",False),
               ("unknown_directive_count",1),("duplicate_directive_count",1))
        for key,value in cases:
            with self.subTest(field=key):
                r=mod.assess({**GOOD,key:value},"one")
                self.assertFalse(r["technical_archive_metadata_pass"])
                self.assertFalse(r["archived_config_safe_to_boot"])
    def test_production_network_and_identity_inheritance_detected(self):
        cases=(("inherited_network_count",0),("inherited_network_count",1),
               ("inherited_network_count",True),("inherited_onboot",False),
               ("inherited_startup_policy",False),
               ("inherited_bootstrap_settings",False),
               ("inherited_hardware_identity",False))
        for key,value in cases:
            with self.subTest(field=key):
                r=mod.assess({**GOOD,key:value},"one")
                self.assertFalse(r["technical_archive_metadata_pass"])
                self.assertTrue(r["clone_config_scrubbing_required"])
    def test_unexpected_types_fields_or_host_fail_closed(self):
        transforms=(lambda d:d.pop("compressed_bytes"),
                    lambda d:d.update({"db_password":"private"}),
                    lambda d:d.update({"inherited_onboot":"true"}),
                    lambda d:d.update({"unknown_directive_count":False}),
                    lambda d:d.update({"inherited_network_count":"2"}))
        for transform in transforms:
            d=GOOD.copy();transform(d)
            result=mod.assess(d,"one")
            self.assertFalse(result["technical_archive_metadata_pass"])
            self.assertNotIn("private",str(result))
        self.assertFalse(mod.assess(GOOD,"wrong-node")["technical_archive_metadata_pass"])
        self.assertFalse(mod.assess(None,"one")["technical_archive_metadata_pass"])
    def test_no_restore_or_boot_and_no_unavailable_remote(self):
        with patch.object(mod,"remote",side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):mod.capture("root@offline")
        source=(BASE/"audit_vm101_pinned_archive_metadata_readonly.py").read_text()
        for bad in ("qmrestore","vma extract","pg_resetwal","initdb",
                    "systemctl restart"):
            self.assertNotIn(bad,source)

if __name__=="__main__":
    unittest.main()
