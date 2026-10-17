#!/usr/bin/env python3
"""Read-only, metadata-only witness of the pinned October 17 VM101 VMA archive.

Inspects the fixed archive's file identity plus VMA config/index; does not
extract disk blocks, compute full checksum, allocate disks or authorize boot.
Only sanitized classifiers leave the Proxmox host.
"""
from __future__ import annotations

import argparse
import json
from audit_vm101_clone_quarantine_readonly import remote
from audit_vm101_restore_precreation_readonly import PINNED_ARCHIVE, PINNED_SHA256

ARCHIVE = "/mnt/nfs_arrg/ProxmoxBackups/dump/" + PINNED_ARCHIVE
COMPRESSED_BYTES = 555108969
LOGICAL_BYTES = 160 * 1024**3

# Fixed path: do not accept user-supplied guest/network/storage locations.
# zstdcat is terminated when VMA metadata is available, rather than
# materializing/extracting the compressed production database volume.
REMOTE_CODE = r"""
import json,os,re,signal,stat,subprocess
archive='/mnt/nfs_arrg/ProxmoxBackups/dump/vzdump-qemu-101-2026_10_17-05_01_05.vma.zst'
root='/mnt/nfs_arrg/ProxmoxBackups/dump'
source='remote_nfs_backups:backup/vzdump-qemu-101-2026_10_17-05_01_05.vma.zst'
mounts=open('/proc/self/mountinfo',encoding='utf8').read().splitlines()
mounted=any(x.split()[4]=='/mnt/nfs_arrg' and ' - nfs4 ' in x for x in mounts)
if not mounted or os.path.realpath(root)!=root or os.path.realpath(archive)!=archive:
 raise RuntimeError('Unexpected archive storage path/mount')
st=os.lstat(archive)
if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:
 raise RuntimeError('Archive not an independent regular file')
p=subprocess.run(['pvesm','path',source],capture_output=True,text=True,timeout=5)
if p.returncode or p.stdout.strip()!=archive:raise RuntimeError('PVE volume resolves to wrong location')
def read_vma(mode):
 z=None
 try:
  z=subprocess.Popen(['zstdcat',archive],stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL,start_new_session=True)
  p=subprocess.run(['vma',mode,'-'],stdin=z.stdout,stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL,timeout=9,check=False)
  z.stdout.close()
  if p.returncode or len(p.stdout)>65536:raise RuntimeError('Bad VMA metadata')
  return p.stdout.decode('utf8')
 finally:
  if z is not None:
   try:os.killpg(z.pid,signal.SIGTERM)
   except ProcessLookupError:pass
   try:z.wait(timeout=2)
   except subprocess.TimeoutExpired:
    os.killpg(z.pid,signal.SIGKILL);z.wait(timeout=2)
config=read_vma('config')
index=read_vma('list')
allowed=frozenset(('agent','balloon','bios','boot','ciuser','cores','cpu',
 'ide2','ipconfig0','memory','meta','name','nameserver','net0','net1',
 'onboot','ostype','scsi0','scsihw','searchdomain','serial0','smbios1',
 'sockets','sshkeys','startup','tags','vga','vmgenid'))
fields={};unknown=0;duplicates=0
for line in config.splitlines():
 m=re.fullmatch(r'([a-z][a-z0-9]*):\s*(.*)',line)
 if not m:continue
 key,value=m.groups()
 if key in fields:duplicates+=1
 fields[key]=value
 if key not in allowed:unknown+=1
devices=re.findall(r'(?m)^DEV: dev_id=\d+ size: (\d+) devname: ([^\s]+)$',index)
num_dev=len(re.findall(r'(?m)^DEV:',index))
header_match=re.search(r'(?m)^CFG: size: \d+ name: qemu-server\.conf$',index)
# No config values, MACs, hardware IDs, usernames or cloud-init content exported.
print(json.dumps({
 'nfs4_mount_confirmed':mounted,
 'archive_file_regular':True,
 'compressed_bytes':st.st_size,
 'archive_link_count':st.st_nlink,
 'archive_other_read_bit_set':bool(st.st_mode & stat.S_IROTH),
 'vm_name_matches':fields.get('name')=='vitrial-prod-db',
 'source_disk_matches':fields.get('scsi0','').split(',')[0]=='local:101/vm-101-disk-0.qcow2',
 'scsi_capacity_matches':'size=160G' in fields.get('scsi0','').split(','),
 'single_expected_vma_device':num_dev==1 and devices==[('171798691840','drive-scsi0')],
 'vma_header_and_config_present':header_match is not None and 'CTIME:' in index,
 'unknown_directive_count':unknown,
 'duplicate_directive_count':duplicates,
 'inherited_network_count':sum(k.startswith('net') and k[3:].isdigit() for k in fields),
 'inherited_onboot':fields.get('onboot')=='1',
 'inherited_startup_policy':'startup' in fields,
 'inherited_bootstrap_settings':any(k in fields for k in
    ('ciuser','sshkeys','ipconfig0','nameserver','searchdomain')),
 'inherited_hardware_identity':any(k in fields for k in
    ('smbios1','vmgenid','serial0')),
 'inherited_cloudinit_media':any('cloudinit' in fields.get(k,'') for k in ('ide2',)),
},sort_keys=True))
"""

FIELDS = {
    "nfs4_mount_confirmed", "archive_file_regular", "compressed_bytes",
    "archive_link_count", "archive_other_read_bit_set", "vm_name_matches", "source_disk_matches",
    "scsi_capacity_matches", "single_expected_vma_device",
    "vma_header_and_config_present", "unknown_directive_count",
    "duplicate_directive_count", "inherited_network_count",
    "inherited_onboot", "inherited_startup_policy",
    "inherited_bootstrap_settings", "inherited_hardware_identity",
    "inherited_cloudinit_media",
}
INHERITED_FLAGS = (
    "inherited_onboot", "inherited_startup_policy",
    "inherited_bootstrap_settings", "inherited_hardware_identity",
    "inherited_cloudinit_media",
)


def assess(value: dict, hostname: str) -> dict:
    holds = []
    inherited_network_count = None
    metadata_inspected = False
    try:
        if hostname.strip() != "one":
            holds.append("Proxmox host identity mismatch")
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError("Unknown or missing archive metadata")
        integer_fields = {"compressed_bytes", "archive_link_count",
                          "unknown_directive_count", "duplicate_directive_count",
                          "inherited_network_count"}
        if (any(type(value[k]) is not int for k in integer_fields)
                or any(type(value[k]) is not bool for k in FIELDS-integer_fields)):
            raise ValueError("Malformed archive metadata field")
        inherited_network_count = value["inherited_network_count"]
        metadata_inspected = True
        for key in ("nfs4_mount_confirmed", "archive_file_regular",
                    "vm_name_matches", "source_disk_matches",
                    "scsi_capacity_matches", "single_expected_vma_device",
                    "vma_header_and_config_present"):
            if not value[key]:
                holds.append("Pinned archive path, source VM or disk metadata mismatch")
        if (value["compressed_bytes"] != COMPRESSED_BYTES or
            value["archive_link_count"] != 1):
            holds.append("Archive file identity changed since pinned review")
        if value["unknown_directive_count"] or value["duplicate_directive_count"]:
            holds.append("Unexpected archived VM configuration directive")
        # The original backup contains original NICs, autostart, bootstrap and
        # identity. A fully scrubbed recovery VM must NEVER reuse this config.
        if inherited_network_count != 2 or not all(value[k] for k in INHERITED_FLAGS if k!="inherited_cloudinit_media"):
            holds.append("Pinned archive inheritance baseline differs from review")
    except (TypeError, ValueError, KeyError, AttributeError):
        holds.append("Malformed or unavailable archive metadata")
    return {
        "schema": "vitrial.vm101.pinned-archive-metadata/v1",
        "gate": "HOLD",
        "technical_archive_metadata_pass": not holds,
        "technical_holds": sorted(set(holds)),
        "pinned_archive": PINNED_ARCHIVE,
        "expected_archive_sha256_from_prior_review": PINNED_SHA256,
        "archive_file_metadata_read_now": metadata_inspected,
        "archive_other_read_bit_set": value.get("archive_other_read_bit_set") if isinstance(value,dict) and type(value.get("archive_other_read_bit_set")) is bool else None,
        "nfs_access_controls_independently_verified": False,
        "archive_permissions_custodian_approved": False,
        "archive_full_sha256_rechecked_now": False,
        "archive_compressed_payload_verified_now": False,
        "archive_postgres_consistency_verified": False,
        "inherited_production_nic_count": inherited_network_count,
        "archived_config_safe_to_boot": False,
        "clone_config_scrubbing_required": True,
        "disk_blocks_extracted": False,
        "production_authorized": False,
        "restore_authorized": False,
        "executable_commands": [],
    }


def capture(host: str) -> dict:
    node = remote(host, ["hostname"])
    raw = remote(host, ["python3", "-c", REMOTE_CODE])
    return assess(json.loads(raw), node)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--proxmox-host", required=True)
    args = p.parse_args()
    try:
        result = capture(args.proxmox_host)
    except (ValueError, RuntimeError, TypeError, OSError, json.JSONDecodeError):
        result = {
            "gate": "HOLD", "technical_archive_metadata_pass": False,
            "technical_holds": ["Pinned VMA archive metadata unavailable"],
            "archive_full_sha256_rechecked_now": False,
            "archive_postgres_consistency_verified": False,
            "archived_config_safe_to_boot": False,
            "restore_authorized": False, "production_authorized": False,
            "executable_commands": [],
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
