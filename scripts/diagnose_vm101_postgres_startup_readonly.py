#!/usr/bin/env python3
"""Read-only VM101 PostgreSQL startup diagnosis with sanitized incident markers.

Historical journal strings are classified INSIDE the guest; never copied to the
controller. This is a hypothesis witness, not recovery, restart or authorization.
"""
from __future__ import annotations
import argparse
import json
import re
from audit_vm101_clone_quarantine_readonly import config_fields,remote

UNIT="postgresql@17-main.service"
LEGACY="10.77.0.10"
CURRENT="10.0.3.28"
GUEST_CODE=r"""
import json,re,subprocess
def get(*argv):
    p=subprocess.run(argv,capture_output=True,text=True,timeout=7,check=False)
    if p.returncode or len(p.stdout)>100000:
        raise RuntimeError('bounded read-only guest query failed')
    return p.stdout
u=get('systemctl','show','postgresql@17-main.service',
      '-p','ActiveState','-p','Result','-p','Type','-p','SubState',
      '-p','InactiveEnterTimestamp')
data={}
for line in u.splitlines():
    k,sep,v=line.partition('=')
    if sep and k in ('ActiveState','Result','Type','SubState','InactiveEnterTimestamp'):
        if k in data:raise ValueError('duplicate unit property')
        data[k]=v
if len(data)!=5:raise ValueError('missing unit property')
conf=get('pg_conftool','17','main','show','listen_addresses')
ips=[x.strip().strip(chr(39)).strip(chr(34)) for x in
     conf.replace('listen_addresses =','').split(',')]
journal=get('journalctl','-u','postgresql@17-main.service',
            '--since','2026-10-06 20:30:00',
            '--until','2026-10-07 00:00:00',
            '--no-pager','-o','cat').lower()
port=get('ss','-lnt')
pg=get('/usr/lib/postgresql/17/bin/pg_controldata','/var/lib/postgresql/17/main')
state=re.search(r'^Database cluster state:\s*([^\n]+)$',pg,re.M)
print(json.dumps({
 'service_active':data['ActiveState']=='active',
 'service_failed':data['ActiveState']=='failed',
 'result_protocol':data['Result']=='protocol',
 'unit_forking':data['Type']=='forking',
 'inactive_since_oct6':('2026-10-06' in data['InactiveEnterTimestamp'] or
                         'Oct 06 2026' in data['InactiveEnterTimestamp'] or
                         'Oct 6 2026' in data['InactiveEnterTimestamp']),
 'postgres_port_listening':bool(re.search(r':5432\s',port)),
 'configured_legacy_listener':('10.77.0.10' in ips),
 'configured_current_listener':('10.0.3.28' in ips or '*' in ips),
 'journal_read_ok':True,
 'journal_window_oct6_count':len(journal.splitlines()),
 'journal_bind_failure':('cannot assign requested address' in journal or
                         'could not bind' in journal),
 'journal_pid_file_missing':('pid file' in journal or 'pidfile' in journal),
 'journal_protocol_failure':('protocol' in journal),
 'journal_wal_marker':bool(re.search(r'\bwal\b|redo|checkpoint',journal)),
 'journal_space_marker':('no space left on device' in journal),
 'pg_control_state':(state.group(1).strip() if state else 'unverified')
},sort_keys=True))
"""


KEYS={
    "service_active","service_failed","result_protocol","unit_forking",
    "inactive_since_oct6","postgres_port_listening",
    "configured_legacy_listener","configured_current_listener",
    "journal_read_ok","journal_window_oct6_count",
    "journal_bind_failure","journal_pid_file_missing",
    "journal_protocol_failure","journal_wal_marker","journal_space_marker",
    "pg_control_state",
}
BOOLEANS=KEYS-{"journal_window_oct6_count","pg_control_state"}
STATES={"in production","shut down","shut down in recovery","in archive recovery",
        "in crash recovery","starting up","unverified"}


def assess(guest:dict,hostname:str,config:str) -> dict:
    holds=[]
    hypothesis="unverified"
    try:
        if (hostname.strip()!="one" or
                config_fields(config).get("name")!="vitrial-prod-db"):
            holds.append("Guest/host identity inconsistent")
        if not isinstance(guest,dict) or set(guest)!=KEYS:
            raise ValueError("Guest diagnostic schema invalid")
        if any(type(guest[k]) is not bool for k in BOOLEANS):
            raise ValueError("Guest diagnostic boolean malformed")
        count=guest["journal_window_oct6_count"]
        if type(count) is not int or not 0 <= count <= 2000:
            raise ValueError("Journal window line count invalid")
        if guest["pg_control_state"] not in STATES:
            raise ValueError("Unexpected PG control-state classification")
        if not guest["journal_read_ok"]:
            holds.append("Historic journal evidence unreadable")
        if guest["service_active"] or not guest["service_failed"]:
            holds.append("Database service state differs from failed baseline")
        if not guest["result_protocol"]:
            holds.append("Service no longer reports protocol failure")
        if (guest["service_failed"] and not guest["service_active"] and
            guest["result_protocol"] and guest["unit_forking"] and
            guest["inactive_since_oct6"] and guest["journal_read_ok"] and count>0 and
            guest["journal_protocol_failure"] and guest["journal_bind_failure"] and
            guest["journal_pid_file_missing"] and
            guest["configured_legacy_listener"] and
            not guest["configured_current_listener"] and
            not guest["postgres_port_listening"]):
            hypothesis="legacy_address_bind_failure_supported"
        else:
            holds.append("Listener bind failure hypothesis not fully supported")
        if guest["journal_wal_marker"] or guest["journal_space_marker"]:
            holds.append("Historical WAL or disk-space diagnostic marker present")
        if guest["pg_control_state"]=="in production" and guest["service_failed"]:
            holds.append("Control file may require crash/WAL recovery")
    except (ValueError,TypeError,KeyError,AttributeError):
        holds.append("Malformed or incomplete sanitized diagnostic")
    return {
       "schema":"vitrial.vm101.postgres-startup-diagnostic/v1",
       "gate":"HOLD",
       "technical_diagnostic_complete":not any("Malformed" in x or "inconsistent" in x for x in holds),
       "failure_hypothesis":hypothesis,
       "sanitized_hold_categories":sorted(set(holds)),
       "historical_journal_window":"2026-10-06T20:30:00Z/2026-10-07T00:00:00Z",
       "root_cause_proven":False,"postgresql_recovery_proven":False,
       "service_restarted":False,"restore_authorized":False,
       "production_authorized":False,"executable_commands":[],
    }


def capture(host:str) -> dict:
    node=remote(host,["hostname"])
    config=remote(host,["qm","config","101"])
    raw=remote(host,["qm","guest","exec","101","--","python3","-c",GUEST_CODE])
    packet=json.loads(raw)
    if (not isinstance(packet,dict) or type(packet.get("exitcode")) is not int
            or packet["exitcode"]!=0 or len(packet.get("out-data",""))>8192):
        raise ValueError("Guest diagnostic execution failed")
    witness=json.loads(packet["out-data"])
    config_after=remote(host,["qm","config","101"])
    if config!=config_after:
        raise ValueError("Production VM config drifted during diagnosis")
    return assess(witness,node,config)


def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--proxmox-host",required=True)
    a=p.parse_args()
    try:
        result=capture(a.proxmox_host)
    except (RuntimeError,ValueError,TypeError,OSError,json.JSONDecodeError):
        result={"gate":"HOLD","technical_diagnostic_complete":False,
                "failure_hypothesis":"unverified",
                "sanitized_hold_categories":["Read-only guest diagnostic unavailable"],
                "root_cause_proven":False,"postgresql_recovery_proven":False,
                "restore_authorized":False,"production_authorized":False,
                "executable_commands":[]}
    print(json.dumps(result,indent=2,sort_keys=True))
    return 2


if __name__=="__main__":
    raise SystemExit(main())
