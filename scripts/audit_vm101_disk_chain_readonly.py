#!/usr/bin/env python3
"""Read-only QEMU image-chain and file-custody witness; never authorizes restores."""
from __future__ import annotations
import argparse
import json
from audit_vm101_clone_quarantine_readonly import config_fields, remote

GIB = 1024**3
TARGETS = {
    "source": (101, "qcow2", "vm-101-disk-0.qcow2"),
    "clone-qcow2": (9101, "qcow2", "vm-9101-disk-0.qcow2"),
    "clone-raw": (9101, "raw", "vm-9101-disk-0.raw"),
}

# Fixed paths only. This probe neither follows guest-provided paths nor writes.
REMOTE_PROBE = r"""
import json,os,pathlib,stat,subprocess,sys
vmid,fmt,filename=sys.argv[1:]
assert (vmid,fmt,filename) in (
 ('101','qcow2','vm-101-disk-0.qcow2'),
 ('9101','qcow2','vm-9101-disk-0.qcow2'),
 ('9101','raw','vm-9101-disk-0.raw'))
base=pathlib.Path('/var/lib/vz/images')
p=base/vmid/filename
for folder in (base,base/vmid):
 s=folder.lstat()
 if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:
  raise RuntimeError('Unsafe parent directory')
s=p.lstat()
if (not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_nlink!=1
    or s.st_mode&0o022 or os.path.realpath(p)!=str(p)):
 raise RuntimeError('Unsafe disk ownership, link or file type')
cmd=['qemu-img','info','-f',fmt,'--output=json']
if vmid=='101':cmd.append('--force-share')
cmd.append(str(p))
out=subprocess.run(cmd,capture_output=True,text=True,timeout=9,check=False)
if out.returncode or len(out.stdout)>65536: raise RuntimeError('No bounded image metadata')
info=json.loads(out.stdout)
print(json.dumps({'stat':{'regular':True,'owner_root':True,
 'nlink':s.st_nlink,'unsafe_write_bit':False,'file_size':s.st_size},
 'image':info},sort_keys=True))
"""


def assess(raw_config: str, status: str, evidence: dict,
           target: str, config_after: str | None = None,
           status_after: str | None = None) -> dict:
    holds = []
    capacity = None
    try:
        vmid, fmt, filename = TARGETS[target]
        fields = config_fields(raw_config)
        if config_after is not None and config_after != raw_config:
            holds.append("Configuration changed during metadata read")
        if status_after is not None and status_after != status:
            holds.append("QEMU status changed during metadata read")
        if fields.get("name") != ("vitrial-prod-db" if vmid == 101 else "vitrial-pg-recovery-quarantine"):
            holds.append("Unexpected guest name")
        expected_status = "status: running" if vmid == 101 else "status: stopped"
        if status.strip() != expected_status:
            holds.append("Guest running/stopped state mismatch")
        disk = fields.get("scsi0", "").split(",")
        if not disk or disk[0] != f"local:{vmid}/{filename}" or disk.count("size=160G") != 1:
            holds.append("QEMU configuration does not match exact expected disk")
        if not isinstance(evidence, dict) or set(evidence) != {"stat", "image"}:
            raise ValueError("Missing evidence sections")
        fs, meta = evidence["stat"], evidence["image"]
        if not isinstance(fs,dict) or not isinstance(meta,dict):
            raise ValueError("Metadata records invalid")
        if (fs.get("regular") is not True or fs.get("owner_root") is not True or
            fs.get("unsafe_write_bit") is not False or
            type(fs.get("nlink")) is not int or fs["nlink"]!=1 or
            type(fs.get("file_size")) is not int or fs["file_size"]<=0):
            holds.append("Unsafe file ownership, type or link count")
        if (meta.get("format") != fmt or type(meta.get("virtual-size")) is not int or
            meta["virtual-size"] != 160*GIB):
            holds.append("Disk format or capacity mismatch")
        else:
            capacity = meta["virtual-size"]
        expected_path=f"/var/lib/vz/images/{vmid}/{filename}"
        if meta.get("filename") != expected_path:
            holds.append("Actual disk path differs from expected image")
        if any(meta.get(k) is not None for k in (
            "backing-filename","full-backing-filename","backing-filename-format",
            "backing-image","data-file","data-file-raw")):
            holds.append("External backing image or data file present")
        if meta.get("dirty-flag") is not False or meta.get("encrypted",False):
            holds.append("Dirty header or encrypted image")
        if meta.get("snapshots"):
            holds.append("Unexpected internal snapshots")
        detail=meta.get("format-specific")
        if fmt=="qcow2":
            if (not isinstance(detail,dict) or detail.get("type")!="qcow2" or
                not isinstance(detail.get("data"),dict)):
                holds.append("Missing QCOW2 header details")
            elif (detail["data"].get("corrupt") is not False or
                  any(k in detail["data"] for k in
                      ("data-file","backing-filename","external-data-file","external-data-file-name"))):
                holds.append("QCOW2 corruption or external data reference")
        elif type(fs.get("file_size")) is int and fs["file_size"]!=160*GIB:
            holds.append("Raw image physical length mismatch")
        children=meta.get("children",[])
        if children:
            if (not isinstance(children,list) or len(children)!=1 or
                not isinstance(children[0],dict) or children[0].get("name")!="file" or
                not isinstance(children[0].get("info"),dict) or
                children[0]["info"].get("format")!="file" or
                children[0]["info"].get("filename")!=expected_path or
                children[0]["info"].get("children")):
                holds.append("Unexpected nested image child")
    except (KeyError,TypeError,ValueError,AttributeError):
        holds.append("Malformed, absent or ambiguous disk metadata")
    return {
        "gate":"HOLD", "target":target if target in TARGETS else "invalid",
        "technical_disk_shape_pass":not holds,
        "technical_holds":sorted(set(holds)),
        "virtual_capacity_bytes":capacity,
        "image_data_verified":False, "pg_recovery_verified":False,
        "restore_authorized":False, "production_authorized":False,
        "executable_commands":[],
    }


def capture(host: str, target: str) -> dict:
    vmid, fmt, filename = TARGETS[target]
    before=remote(host,["qm","config",str(vmid)])
    state=remote(host,["qm","status",str(vmid)])
    if vmid==9101 and state.strip()!="status: stopped":
        raise ValueError("Clone must remain stopped")
    raw=remote(host,["python3","-c",REMOTE_PROBE,str(vmid),fmt,filename])
    data=json.loads(raw)
    after=remote(host,["qm","config",str(vmid)])
    after_state=remote(host,["qm","status",str(vmid)])
    return assess(before,state,data,target,after,after_state)


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--proxmox-host",required=True)
    p.add_argument("--target",choices=sorted(TARGETS),required=True)
    args=p.parse_args()
    try:
        result=capture(args.proxmox_host,args.target)
    except (ValueError,RuntimeError,OSError,TypeError,json.JSONDecodeError):
        result={"gate":"HOLD","target":args.target,"technical_disk_shape_pass":False,
                "technical_holds":["Read-only disk metadata unavailable"],
                "restore_authorized":False,"production_authorized":False,
                "executable_commands":[]}
    print(json.dumps(result,indent=2,sort_keys=True))
    return 2


if __name__=="__main__":
    raise SystemExit(main())
