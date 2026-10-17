# VM101 recovery: pre-creation gate and authorized operator handoff — October 17, 2026

**Current decision: HOLD.** This checklist does not authorize a restore, production change, data release, or guest boot. It complements `VM101_LIVE_ADMISSION_FOLLOWUP_20261017.md` and the post-creation VM9101 quarantine audit.

## Automated zero-mutation checks

From an authorized Fleet Commander/OpenCode shell with the designated Proxmox SSH identity:

```sh
python3 scripts/audit_vm101_restore_precreation_readonly.py --proxmox-host root@<designated-proxmox-host>
```

The script reads only Proxmox cluster guest identities, VM101's QEMU configuration, local storage metadata and candidate volume listing, and a host-local directory presence/symlink predicate. It **does not** traverse the NFS backup mount, read the selected archive's bytes, inspect guest database records, change VM configuration, allocate disks, or grant authorization.

A correct healthy technical result has `technical_precreation_pass=true` **and** `gate=HOLD`, `restore_authorized=false`, with CLI exit **2**. The script's evidence is valid for no more than five minutes; repeat it immediately before a separately authorized change. A missing source, occupied VMID, drifting cluster inventory, wrong hypervisor, local storage below 384 GiB or ambiguous/symlinked candidate directory yields a technical hold as well.

## Backup and scope pinned for human review

- Source: VM101 / PostgreSQL 17/main, 160 GiB boot volume; **production remains authoritative**.
- Selected archive: `vzdump-qemu-101-2026_10_17-05_01_05.vma.zst` on `remote_nfs_backups`.
- Prior independently read archive SHA-256: `49b05b89aef9005bc93887ef128d12c00a2d9f9b54ca8809edc56cb331a856f8`.
- Prior `zstd --test` and native `vma verify` passed, but **no October 17 PostgreSQL crash-recovery or data-integrity evidence exists yet**.
- Intended temporary clone: VM9101; **zero NICs**, local independent writable disk, no cloud-init/password injection, no production disk attachment, distinct hardware identity, `onboot=0`, initially **stopped**.
- A successful snapshot restore from October 9 is historical evidence, **not** acceptance of the October 17 backup or RPO.

## Required independent approvals — presently missing

1. **Database owner:** exact archive identification, data recency and intended recovery point, WAL continuity concerns (`pg_controldata` previously said `in production` while cluster DOWN), approved read-only customer/tenant probes, and stop/rollback plan.
2. **Customer data custodian:** entitlement to duplicate the production data into local quarantined storage, approved physical/at-rest custody, access controls, retained copy handling and secure cleanup, and any applicable customer notification.
3. **Proxmox/storage owner:** verified destination volume isolation and headroom, credential limits, no host/guest network path, 9101 collision recheck, and a separate maintenance/window approval for VM creation and the first boot.
4. **Independent release reviewer:** receipt identity and authenticity, exact version/backup binding, and explicit denial of using the scratch result as authority to deploy production VM101, public networking, migrations or TestFlight.

Do not record a Jira comment, synthetic test result, successful SSH login, or AI-generated draft as a substitute for approval by a separately identified human owner. Keep sensitive SQL counts and raw recovery logs in private 0600 evidence; publish only sanitized dispositions.

## Mandatory post-creation / pre-first-boot gate (only after approvals)

```sh
python3 scripts/audit_vm101_clone_quarantine_readonly.py --proxmox-host root@<designated-proxmox-host>
```

The tool inspects QEMU configuration for inherited `net*`, cloud-init, passthrough/USB, other disks, startup hooks, autostart, or production disk references; however it **does not** prove the hypervisor's actual runtime network, physical storage path, image contents or guest loopback-only state. A passing shape still prints **HOLD**. After the authorized offline boot, the operator must independently inspect actual QEMU process arguments and guest network inventory before accessing sensitive PostgreSQL data.

## Later gates, not part of this authorization

- Cloned PostgreSQL 17 crash redo, timeline/WAL continuity, `pg_amcheck`, schema, tenant/customer/job/quote counts, and accepted RTO/RPO.
- DBA-authorized production VM101 listener/HBA edit and verified byte-exact rollback during a maintenance window; no `initdb`/`pg_resetwal` short cuts.
- API VM100 dependency targets, VM102 object durability, OPNsense ingress ownership (VM150 currently owns `.139`), TLS/S3 permissions and signed physical iOS acceptance.
- Release/quotation approval only with authoritative supplier pricebooks and manufacturing formulas; no synthetic BOM promotion.

**No executable restore commands are shipped here.** This is an evidence-gathering and approval handoff, not a production deployment plan.
