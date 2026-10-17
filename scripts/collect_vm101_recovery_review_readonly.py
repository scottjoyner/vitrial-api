#!/usr/bin/env python3
"""Read-only, non-authorizing synthesis of VM101 restoration gate evidence."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

import audit_vm101_clone_quarantine_readonly as quarantine
import audit_vm101_disk_chain_readonly as disk
import audit_vm101_restore_precreation_readonly as precreation
import diagnose_vm101_postgres_startup_readonly as startup
import verify_vm101_approval_receipts as approval


def inspect_quarantine(host):
    qemu=quarantine.remote(host,["qm","list"])
    lxc=quarantine.remote(host,["pct","list"])
    source=quarantine.remote(host,["qm","config","101"])
    candidate=quarantine.remote(host,["qm","config","9101"])
    status=quarantine.remote(host,["qm","status","9101"])
    return quarantine.assess(source,candidate,status,qemu,lxc)


def component(worker,field,authorize="restore_authorized"):
    try:
        value=worker()
        if (not isinstance(value,dict) or value.get("gate")!="HOLD" or
            value.get(authorize) is not False or
            value.get("production_authorized") is not False or
            value.get("executable_commands")!=[] or
            type(value.get(field)) is not bool):
            raise ValueError("Untrusted or incomplete component")
        holds=value.get("technical_holds",value.get("hold_reasons",
                         value.get("sanitized_hold_categories")))
        if not isinstance(holds,list) or not all(isinstance(x,str) for x in holds):
            raise ValueError("Missing normalized reasons")
        if value[field] and holds and field!="technical_diagnostic_complete":
            raise ValueError("Passing component conflicts with reported hold reasons")
        result={"pass":value[field],"unavailable":False,"finding_count":len(holds)}
        if field=="technical_diagnostic_complete":
            if (value.get("root_cause_proven") is not False or
                value.get("postgresql_recovery_proven") is not False or
                value.get("service_restarted") is not False or
                value.get("failure_hypothesis") not in
                ("unverified","legacy_address_bind_failure_supported")):
                raise ValueError("Diagnostic asserted unauthorized state/proof")
            result["failure_hypothesis"]=value["failure_hypothesis"]
        return result
    except (ValueError,TypeError,RuntimeError,OSError,KeyError,AttributeError):
        return {"pass":False,"unavailable":True,"finding_count":None}


def read_approval(bundle,keys):
    return approval.assess(approval.safe_json_file(bundle),approval.safe_json_file(keys))

def collect(host,bundle=None,keys=None,now=None,probes=None):
    if (bundle is None)!=(keys is None):
        raise ValueError("Both private approval inputs required")
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timezone aware clock required")
    funcs=probes if probes is not None else {
        "precreation":lambda:precreation.capture(host),
        "source_disk":lambda:disk.capture(host,"source"),
        "postgres_startup":lambda:startup.capture(host),
        "clone_quarantine":lambda:inspect_quarantine(host),
        "clone_disk":lambda:disk.capture(host,"clone-qcow2"),
    }
    expected={"precreation":"technical_precreation_pass",
              "source_disk":"technical_disk_shape_pass",
              "postgres_startup":"technical_diagnostic_complete",
              "clone_quarantine":"technical_isolation_shape_pass",
              "clone_disk":"technical_disk_shape_pass"}
    if set(funcs)!=set(expected):
        raise ValueError("Incomplete probe set")
    checks={name:component(funcs[name],field) for name,field in expected.items()}
    checks["signed_approvals"]=(
        component(lambda:read_approval(bundle,keys),"crypto_approval_reviewable",
                  "restoration_authorized") if bundle is not None else
        {"pass":False,"unavailable":True,"finding_count":None})
    timestamp=now.astimezone(timezone.utc)
    result={
        "schema":"vitrial.vm101.recovery-review/v1",
        "node":"one", "source_vmid":101, "candidate_vmid":9101,
        "collected_at_utc":timestamp.isoformat(timespec="seconds"),
        "expires_at_utc":(timestamp+timedelta(minutes=5)).isoformat(timespec="seconds"),
        "checks":checks,
        "all_technical_checks_pass":all(x["pass"] for x in checks.values()),
        "unverified_production_recovery":True,
        "unverified_approval_authority":True,
        "unverified_replay_prevention":True,
        "backup_data_recovery_verified":False,
        "gate":"HOLD","restore_authorized":False,
        "production_authorized":False,"executable_commands":[],
        "checksum_is_not_a_signature":True,
    }
    blob=json.dumps(result,sort_keys=True,separators=(",",":")).encode("utf8")
    result["evidence_sha256"]=hashlib.sha256(blob).hexdigest()
    return result

def store_private(path,report):
    parent=path.parent
    info=parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or
        stat.S_IMODE(info.st_mode)&0o077 or parent.resolve()!=parent):
        raise ValueError("Private owner-controlled evidence parent required")
    content=(json.dumps(report,sort_keys=True,indent=2)+"\n").encode("utf8")
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|
               getattr(os,"O_NOFOLLOW",0),0o600)
    try:
        with os.fdopen(fd,"wb",closefd=False) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    finally:
        os.close(fd)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--proxmox-host",required=True)
    p.add_argument("--approval-bundle",type=Path)
    p.add_argument("--trust-keys",type=Path)
    p.add_argument("--output",type=Path)
    a=p.parse_args()
    try:
        report=collect(a.proxmox_host,a.approval_bundle,a.trust_keys)
        if a.output is not None:
            store_private(a.output,report)
    except (ValueError,RuntimeError,OSError,TypeError):
        report={"schema":"vitrial.vm101.recovery-review/v1",
                "gate":"HOLD","all_technical_checks_pass":False,
                "report_error":"Complete evidence unavailable or unsafe output destination",
                "restore_authorized":False,"production_authorized":False,
                "executable_commands":[]}
    print(json.dumps(report,sort_keys=True,indent=2))
    return 2

if __name__=="__main__":
    raise SystemExit(main())
