# VM101 isolated recovery — live admission follow-up (2026-10-17)

**Decision: HOLD. No restore, VM creation, PostgreSQL restart, database migration or production deployment.**

## New live Fleet Commander checks

- Proxmox node `one`, PVE 9.2.4: six running QEMU guests and 20 LXC containers; VM9101 and VM9102 unallocated at inspection.
- Fresh `qm`/Proxmox directory-storage preflight on VMID 9101 returned `capacity_shape_review_pass=true`, with **1,189,266,214,912** bytes available against **412,316,860,416** bytes minimum (two 160 GiB allocations plus 64 GiB headroom). It returned `restore_authorized=false` and deliberate exit 2. This capacity reading expires after five minutes and must be repeated before any approval-controlled work.
- Local Proxmox storage `local` is directory-backed at `/var/lib/vz`; backup storage `remote_nfs_backups` remains distinct and NFS-backed. No disk was reserved or copied.
- Existing read-only three-guest dependency witness returned **12 HOLD reasons**: VM100 unhealthy, stale database `10.77.0.10` and S3 `10.77.0.11` targets, PostgreSQL17/main DOWN with old listener and old API `pg_hba`, PG control state `in production` while stopped, basebackup failed, unproven NFS transport protection, off-host PITR/object restore and public ingress.
- Current guest private addresses remain VM100 `10.0.3.27`, VM101 `10.0.3.28`, VM102 `10.0.3.29`. **Do not change those production settings while Gate A is blocked.**

## Read-only quarantine clone audit

`scripts/audit_vm101_clone_quarantine_readonly.py` checks the *post-creation, pre-first-boot* VM9101 QEMU configuration without emitting raw possibly sensitive guest configuration. It denies inherited production NICs, cloud-init, credentials, passthrough devices, extra drives, startup hooks, autostart, source disk references or reused hardware identity. A passing *configuration shape* would still **never authorize a restore, boot, SQL inspection or production repair**.

To check the existing state from an authorized operator host, run:

```sh
python3 scripts/audit_vm101_clone_quarantine_readonly.py --proxmox-host root@<designated-proxmox-host>
```

Expected result today: VM9101 does not exist; the audit returns `HOLD`, exit **2**, without creating anything. A zero-NIC VM cannot be proven until a correctly approved clone is created. This read-only QEMU configuration check is **not** a storage path realpath inspection, hypervisor runtime argument inspection, guest-local network inspection or independent network security audit; those separate safeguards are mandatory before first boot.

## Admission and deployment order

1. Independent DBA/data custodian must approve exact Oct17 VM101 backup SHA-256 `49b05b89aef9005bc93887ef128d12c00a2d9f9b54ca8809edc56cb331a856f8`, expected tenant/customer scope, backup provenance, at-rest protection, RTO/RPO and rollback operator/window.
2. Authorized infrastructure owner must reserve/recheck nonproduction VMID, actual separate writable disk, zero-NIC/no-route initial boot, boot-disabled configuration, and no production disk reference. Archive `vzdump-qemu-101-2026_10_17-05_01_05.vma.zst` passed SHA-256, zstd and VMA structural verification, **not** PostgreSQL restore.
3. After signoffs, validate a separate offline clone's PostgreSQL crash recovery/WAL, `pg_amcheck`, schema/migrations, tenant/customer/job/quote counts, backup custody, and read-only application consistency. Archive may predate recent business changes even if structurally valid.
4. Separately require authorized maintenance/rollback for VM101 listener/HBA, VM100 DB/S3 environment, RustFS restore, OPNsense/TLS/ingress ownership and two-iPhone signed acceptance.
5. Never reroute public `.139` without checking VM150 K3s address ownership; never treat synthetic quotation checks as manufacturer/pricebook authority.

**Explicitly forbidden in this phase:** `qmrestore`, `qm create/start/set`, production `pg_ctl` or `pg_resetwal`, Alembic upgrade, public NAT/TLS changes, secret rotation, TestFlight publication. Code/test review on a draft PR is permitted; production authorization remains false.