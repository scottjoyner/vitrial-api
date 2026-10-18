"""Adversarial read-only image header, backing-chain and custody tests."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
BASE=Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0,str(BASE))
spec=importlib.util.spec_from_file_location("diskmeta",BASE/"audit_vm101_disk_chain_readonly.py")
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
GIB=1024**3
IMG={"filename":"/var/lib/vz/images/101/vm-101-disk-0.qcow2",
     "format":"qcow2","virtual-size":160*GIB,"dirty-flag":False,
     "format-specific":{"type":"qcow2","data":{"corrupt":False}},
     "children":[{"name":"file","info":{"filename":"/var/lib/vz/images/101/vm-101-disk-0.qcow2",
                                          "format":"file","children":[]}}]}
FS={"regular":True,"owner_root":True,"unsafe_write_bit":False,
    "nlink":1,"file_size":160*GIB}
CFG="name: vitrial-prod-db\nonboot: 1\nscsi0: local:101/vm-101-disk-0.qcow2,size=160G\n"

def review(change=None,target="source",cfg=CFG,status="status: running",after=None):
    e={"stat":deepcopy(FS),"image":deepcopy(IMG)}
    if target!="source":
        fmt="qcow2" if target=="clone-qcow2" else "raw"
        name=f"vm-9101-disk-0.{fmt}"
        e["image"]["filename"]=f"/var/lib/vz/images/9101/{name}"
        e["image"]["format"]=fmt
        e["image"]["children"][0]["info"]["filename"]=e["image"]["filename"]
        if fmt=="raw":e["image"].pop("format-specific")
        cfg=f"name: vitrial-pg-recovery-quarantine\nonboot: 0\nscsi0: local:9101/{name},size=160G\n"
        status="status: stopped"
    if change:change(e)
    return mod.assess(cfg,status,e,target,cfg if after is None else after,status)

class DiskChainTests(unittest.TestCase):
    def test_source_pass_remains_hold(self):
        r=review()
        self.assertTrue(r["technical_disk_shape_pass"])
        self.assertFalse(r["restore_authorized"])
        self.assertFalse(r["pg_recovery_verified"])
        self.assertEqual(r["gate"],"HOLD")
        self.assertEqual(r["executable_commands"],[])
    def test_expected_clone_shapes(self):
        for target in ("clone-qcow2","clone-raw"):
            with self.subTest(target=target):
                r=review(target=target)
                self.assertTrue(r["technical_disk_shape_pass"])
                self.assertFalse(r["restore_authorized"])
    def test_external_backing_and_data_files_rejected(self):
        for k in ("backing-filename","full-backing-filename","backing-filename-format",
                  "backing-image","data-file","data-file-raw"):
            with self.subTest(k=k):
                self.assertFalse(review(lambda e:e["image"].update({k:"/outside"}))["technical_disk_shape_pass"])
        for k in ("data-file","backing-filename","external-data-file","external-data-file-name"):
            with self.subTest(nested=k):
                self.assertFalse(review(lambda e:e["image"]["format-specific"]["data"].update({k:"/tmp/evil"}))["technical_disk_shape_pass"])
    def test_header_drift_and_snapshots_rejected(self):
        for k,v in (("format","raw"),("virtual-size",160*GIB-1),
                    ("virtual-size",True),("filename","/tmp/disk"),
                    ("dirty-flag",True),("encrypted",True),
                    ("snapshots",[{"id":"before"}])):
            with self.subTest(k=k):
                self.assertFalse(review(lambda e:e["image"].update({k:v}))["technical_disk_shape_pass"])
    def test_corruption_or_nested_children_rejected(self):
        mutations=(lambda e:e["image"]["format-specific"]["data"].update({"corrupt":True}),
                   lambda e:e["image"]["children"].append({"name":"backing","info":{}}),
                   lambda e:e["image"]["children"][0]["info"].update({"filename":"/outside"}),
                   lambda e:e["image"]["children"][0]["info"].update({"children":[{"name":"backing"}]}),
                   lambda e:e["image"]["format-specific"].update({"type":"raw"}))
        for f in mutations:
            with self.subTest(case=str(f)):
                self.assertFalse(review(f)["technical_disk_shape_pass"])
    def test_link_and_file_custody(self):
        for k,v in (("regular",False),("owner_root",False),("unsafe_write_bit",True),
                    ("nlink",2),("file_size",0),("file_size",True)):
            with self.subTest(k=k):
                self.assertFalse(review(lambda e:e["stat"].update({k:v}))["technical_disk_shape_pass"])
    def test_raw_size_mismatch(self):
        self.assertFalse(review(lambda e:e["stat"].update({"file_size":64*GIB}),target="clone-raw")["technical_disk_shape_pass"])
    def test_vm_state_config_and_target_guards(self):
        self.assertFalse(review(status="status: stopped")["technical_disk_shape_pass"])
        self.assertFalse(review(cfg=CFG.replace("101/vm-101","102/vm-102"))["technical_disk_shape_pass"])
        self.assertFalse(review(after=CFG+"net0: virtio=00:00:00:00:00:01")["technical_disk_shape_pass"])
        self.assertFalse(mod.assess(CFG,"status: stopped",{"stat":FS,"image":IMG},"clone-qcow2")["technical_disk_shape_pass"])
    def test_denies_mutation_commands(self):
        code=(BASE/"audit_vm101_disk_chain_readonly.py").read_text()
        for bad in ('"qmrestore"','"qm","start"','"qm","create"','"qemu-img","rebase"'):
            self.assertNotIn(bad,code)
        with patch.object(mod,"remote",side_effect=RuntimeError("unreachable")):
            with self.assertRaises(RuntimeError):mod.capture("root@invalid","source")
if __name__=="__main__":unittest.main()
