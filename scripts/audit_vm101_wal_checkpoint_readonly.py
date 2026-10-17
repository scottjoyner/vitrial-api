#!/usr/bin/env python3
"""Fail-closed metadata-only PostgreSQL 17 WAL checkpoint-readability witness.

Never starts PostgreSQL, writes WAL, opens NFS, streams WAL content, or
asserts recoverability. pg_waldump decodes at most one record per location.
"""
from __future__ import annotations
import argparse
import json
from audit_vm101_clone_quarantine_readonly import config_fields,remote

EXPECTED="00000001000000000000001A"
REDO="0/1A000060"
CHECKPOINT="0/1A0000B8"
GIB=1024**3
GUEST_CODE=r'''
import json,os,re,stat,subprocess
base='/var/lib/postgresql/17/main';wal=base+'/pg_wal'
def ctl():
 p=subprocess.run(['/usr/lib/postgresql/17/bin/pg_controldata',base],capture_output=True,text=True,timeout=4)
 if p.returncode:raise RuntimeError('control read failed')
 return {k.strip():v.strip() for line in p.stdout.splitlines() for k,sep,v in [line.partition(':')] if sep}
a=ctl()
keys=("Database cluster state","Latest checkpoint location", "Latest checkpoint's REDO location", "Latest checkpoint's REDO WAL file","Latest checkpoint's TimeLineID","Bytes per WAL segment", "Data page checksum version")
if not all(k in a for k in keys):raise RuntimeError('missing control field')
state=subprocess.run(['systemctl','is-active','postgresql@17-main.service'],capture_output=True,text=True,timeout=4).stdout.strip()
segments=[];unexpected=[]
for e in os.scandir(wal):
 if re.fullmatch('[0-9A-F]{24}',e.name):
  st=e.stat(follow_symlinks=False)
  if not stat.S_ISREG(st.st_mode):raise RuntimeError('not a regular segment')
  segments.append((e.name,st.st_size))
 elif re.fullmatch('[0-9A-F]{24}\\.partial',e.name):unexpected.append('partial')
 if len(segments)+len(unexpected)>512:raise RuntimeError('too many segments')
segments.sort();names=[n for n,_ in segments]
contiguous=(all(int(names[i+1][8:],16)==int(names[i][8:],16)+1 for i in range(len(names)-1)) if names else False)
valid_lengths=bool(segments) and all(size==16*1024*1024 for _,size in segments)
locations={}
for key,point in [('redo',a["Latest checkpoint's REDO location"]),('checkpoint',a['Latest checkpoint location'])]:
 if not re.fullmatch(r'[0-9A-F]+/[0-9A-F]+',point):raise RuntimeError('bad WAL LSN')
 try:
  p=subprocess.run(['/usr/lib/postgresql/17/bin/pg_waldump','-p',wal,'-s',point,'-n','1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
  locations[key]=p.returncode==0
 except subprocess.TimeoutExpired:locations[key]=False
b=ctl()
print(json.dumps({
 'control_stable': all(a[k]==b[k] for k in keys),
 'postgres_stopped':state in ('failed','inactive'),
 'control_state':a['Database cluster state'],
 'redo_lsn_exact':a["Latest checkpoint's REDO location"]=='0/1A000060',
 'checkpoint_lsn_exact':a['Latest checkpoint location']=='0/1A0000B8',
 'redo_wal_file_exact':a["Latest checkpoint's REDO WAL file"]=='00000001000000000000001A',
 'timeline_one':a["Latest checkpoint's TimeLineID"]=='1',
 'wal_segment_bytes':int(a['Bytes per WAL segment']),
 'wal_segment_count':len(segments),
 'wal_sequence_contiguous':contiguous,
 'wal_segments_regular_16mib':valid_lengths,
 'redo_segment_present':a["Latest checkpoint's REDO WAL file"] in names,
 'wal_timeline_consistent':bool(names) and all(n[:8]=='00000001' for n in names),
 'unexpected_partial_segment':bool(unexpected),
 'redo_record_readable':locations['redo'],
 'checkpoint_record_readable':locations['checkpoint'],
 'data_page_checksums_disabled':a['Data page checksum version']=='0',
},sort_keys=True))
'''

FIELDS={"control_stable","postgres_stopped","control_state","redo_lsn_exact",
 "checkpoint_lsn_exact","redo_wal_file_exact","timeline_one","wal_segment_bytes",
 "wal_segment_count","wal_sequence_contiguous","wal_segments_regular_16mib",
 "redo_segment_present","wal_timeline_consistent","unexpected_partial_segment",
 "redo_record_readable","checkpoint_record_readable","data_page_checksums_disabled"}
BOOL_FIELDS=FIELDS-{"control_state","wal_segment_bytes","wal_segment_count"}
REQUIRED_TRUE=BOOL_FIELDS-{"unexpected_partial_segment","data_page_checksums_disabled"}


def assess(data:dict,hostname:str,vm_config:str)->dict:
    holds=[]
    technical=False
    checksum_status="unverified"
    try:
        if hostname.strip()!="one" or config_fields(vm_config).get("name")!="vitrial-prod-db":
            holds.append("Source host or VM identity changed")
        if not isinstance(data,dict) or set(data)!=FIELDS or any(type(data[x]) is not bool for x in BOOL_FIELDS):
            raise ValueError("Wrong metadata shape or types")
        if type(data["wal_segment_bytes"]) is not int or data["wal_segment_bytes"]!=16*1024*1024:
            holds.append("Unexpected WAL segment configuration")
        if type(data["wal_segment_count"]) is not int or not 1<=data["wal_segment_count"]<=512:
            holds.append("Unexpected WAL segment inventory size")
        if data["control_state"]!="in production":
            holds.append("Control-file state drift; review prior baseline")
        if not all(data[k] for k in REQUIRED_TRUE):
            holds.append("Checkpoint decode or WAL segment continuity unproven")
        if data["unexpected_partial_segment"]:
            holds.append("Partial WAL segment requires independent review")
        checksum_status="disabled" if data["data_page_checksums_disabled"] else "enabled_unverified"
        technical=not holds
    except (ValueError,KeyError,TypeError,AttributeError):
        holds.append("Malformed or missing checkpoint metadata")
    return {"schema":"vitrial.vm101.wal-checkpoint-readonly/v1","gate":"HOLD",
        "technical_wal_metadata_pass":technical,
        "technical_holds":sorted(set(holds)),
        "wal_segment_count":data.get("wal_segment_count") if isinstance(data,dict) and type(data.get("wal_segment_count")) is int else None,
        "data_page_checksums":checksum_status,
        "checkpoint_record_sampled_only":True,
        "whole_wal_stream_verified":False,"full_replay_verified":False,
        "tenant_data_verified":False,"wal_archival_independently_proven":False,
        "restore_authorized":False,"production_authorized":False,
        "executable_commands":[]}


def capture(host:str)->dict:
    hostname=remote(host,["hostname"])
    first=remote(host,["qm","config","101"])
    packet=json.loads(remote(host,["qm","guest","exec","101","--","python3","-c",GUEST_CODE]))
    if (not isinstance(packet,dict) or type(packet.get("exitcode")) is not int or
        packet.get("exitcode")!=0 or not isinstance(packet.get("out-data"),str) or
        len(packet["out-data"])>8192):
        raise ValueError("Guest WAL metadata probe unavailable")
    data=json.loads(packet["out-data"])
    second=remote(host,["qm","config","101"])
    if second!=first:raise ValueError("VM configuration changed during WAL probe")
    return assess(data,hostname,first)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxmox-host",required=True)
    args=parser.parse_args()
    try: result=capture(args.proxmox_host)
    except (ValueError,RuntimeError,TypeError,OSError,json.JSONDecodeError):
        result={"gate":"HOLD","technical_wal_metadata_pass":False,
          "technical_holds":["Bounded WAL metadata inspection unavailable"],
          "whole_wal_stream_verified":False,"full_replay_verified":False,
          "restore_authorized":False,"production_authorized":False,
          "executable_commands":[]}
    print(json.dumps(result,sort_keys=True,indent=2))
    return 2

if __name__=="__main__":raise SystemExit(main())
