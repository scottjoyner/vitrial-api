#!/usr/bin/env python3
"""Read-only, non-authorizing synthesis of VM101 restoration gate evidence."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import time
from pathlib import Path
import stat

import audit_vm101_clone_quarantine_readonly as quarantine
import audit_vm101_disk_chain_readonly as disk
import audit_vm101_restore_precreation_readonly as precreation
import diagnose_vm101_postgres_startup_readonly as startup
import audit_vm101_wal_checkpoint_readonly as wal
import audit_vm101_pinned_archive_metadata_readonly as archive
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
        if field=="technical_archive_metadata_pass":
            if (value.get("archived_config_safe_to_boot") is not False or
                value.get("clone_config_scrubbing_required") is not True or
                value.get("archive_full_sha256_rechecked_now") is not False or
                value.get("archive_compressed_payload_verified_now") is not False or
                value.get("archive_postgres_consistency_verified") is not False or
                value.get("disk_blocks_extracted") is not False or
                value.get("nfs_access_controls_independently_verified") is not False or
                value.get("archive_permissions_custodian_approved") is not False or
                value.get("archive_other_read_bit_set") is not True or
                type(value.get("inherited_production_nic_count")) is not int or
                value["inherited_production_nic_count"]!=2):
                raise ValueError("Archive metadata must not authorize unsafe boot")
            result["inherited_production_nic_count"]=value["inherited_production_nic_count"]
            result["requires_config_scrub"]=True
            result["archive_other_read_bit_set"]=True
            result["archive_custody_permission_review_required"]=True
        if field=="technical_wal_metadata_pass":
            if (value.get("whole_wal_stream_verified") is not False or
                value.get("full_replay_verified") is not False or
                value.get("tenant_data_verified") is not False or
                value.get("wal_archival_independently_proven") is not False or
                value.get("wal_end_of_written_data_proven") is not False or
                value.get("checkpoint_record_sampled_only") is not True or
                type(value.get("wal_readable_prefix_records")) is not int or
                not 0<=value["wal_readable_prefix_records"]<=16 or
                value.get("wal_first_boundary") not in
                ("zero_length_record_boundary","not_observed_within_16",
                 "unclassified_failure")):
                raise ValueError("WAL sample must not be mislabeled as full recovery")
            result["wal_readable_prefix_records"]=value["wal_readable_prefix_records"]
            result["wal_first_boundary"]=value["wal_first_boundary"]
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

def collect(host,bundle=None,keys=None,now=None,probes=None,monotonic=None):
    if (bundle is None)!=(keys is None):
        raise ValueError("Both private approval inputs required")
    now=now if now is not None else datetime.now(timezone.utc)
    if not isinstance(now,datetime) or now.tzinfo is None:
        raise ValueError("Timezone aware clock required")
    # The five-minute validity begins BEFORE the first remote query. Each
    # subsequent sample is bounded against this same monotonic deadline.
    monotonic=monotonic or time.monotonic
    start_tick=monotonic()
    if type(start_tick) not in (int,float) or not math.isfinite(start_tick):
        raise ValueError("Invalid monotonic clock")
    funcs=probes if probes is not None else {
        "precreation":lambda:precreation.capture(host),
        "archive_metadata":lambda:archive.capture(host),
        "source_disk":lambda:disk.capture(host,"source"),
        "postgres_startup":lambda:startup.capture(host),
        "wal_checkpoint":lambda:wal.capture(host),
        "clone_quarantine":lambda:inspect_quarantine(host),
        "clone_disk":lambda:disk.capture(host,"clone-qcow2"),
    }
    expected={"precreation":"technical_precreation_pass",
              "archive_metadata":"technical_archive_metadata_pass",
              "source_disk":"technical_disk_shape_pass",
              "postgres_startup":"technical_diagnostic_complete",
              "wal_checkpoint":"technical_wal_metadata_pass",
              "clone_quarantine":"technical_isolation_shape_pass",
              "clone_disk":"technical_disk_shape_pass"}
    if set(funcs)!=set(expected):
        raise ValueError("Incomplete probe set")
    def elapsed_seconds():
        tick=monotonic()
        if type(tick) not in (int,float) or not math.isfinite(tick):
            raise ValueError("Monotonic clock became invalid")
        delta=tick-start_tick
        if delta<0:
            raise ValueError("Monotonic clock moved backwards")
        return delta

    def unavailable():
        return {"pass":False,"unavailable":True,"finding_count":None}

    checks={}
    expired=False
    for name,field in expected.items():
        if expired or elapsed_seconds()>=300:
            expired=True
            checks[name]=unavailable()
            continue
        checks[name]=component(funcs[name],field)
        if elapsed_seconds()>=300:
            expired=True
            checks[name]=unavailable()

    if expired or elapsed_seconds()>=300:
        expired=True
        checks["signed_approvals"]=unavailable()
    else:
        checks["signed_approvals"]=(
            component(lambda:read_approval(bundle,keys),"crypto_approval_reviewable",
                      "restoration_authorized") if bundle is not None else unavailable())
        if elapsed_seconds()>=300:
            expired=True
            checks["signed_approvals"]=unavailable()
    duration=elapsed_seconds()
    if duration>=300:
        expired=True
    if expired:
        # Earlier samples have also aged out; never emit old PASS results in
        # what appears to be a fresh single-snapshot report.
        checks={name:unavailable() for name in (*expected,"signed_approvals")}
    timestamp=now.astimezone(timezone.utc)
    result={
        "schema":"vitrial.vm101.recovery-review/v1",
        "node":"one", "source_vmid":101, "candidate_vmid":9101,
        "collected_at_utc":timestamp.isoformat(timespec="seconds"),
        "expires_at_utc":(timestamp+timedelta(minutes=5)).isoformat(timespec="seconds"),
        "collection_duration_ms":int(duration*1000),
        "evidence_window_exceeded":expired,
        "validity_begins_at_first_probe":True,
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
