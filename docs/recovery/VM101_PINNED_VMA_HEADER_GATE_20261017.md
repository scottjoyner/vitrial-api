# VM101 October 17 VMA header and inherited-configuration gate

**Production recovery remains HOLD.** This is a bounded, read-only inspection
of the fixed compressed archive already selected for independent review:

`remote_nfs_backups:backup/vzdump-qemu-101-2026_10_17-05_01_05.vma.zst`

Run from the pinned PR branch on x1-370:

    python3 scripts/audit_vm101_pinned_archive_metadata_readonly.py --proxmox-host root@38.143.56.138

The tool validates the Proxmox storage locator, mounted NFS4 path, regular
archive file, compressed size, and the VMA **index and VM configuration header**.
It stops the streaming decompressor after each metadata query. It never
extracts a disk, reads guest PostgreSQL files from the backup, creates VM9101,
starts VMs, changes storage, or publishes any raw archived guest configuration.
Only sanitized classifications leave the hypervisor.

## Observed on October 17, 2026

| Evidence | Observation |
| --- | --- |
| Compressed archive | 555,108,969 bytes; regular file; link count 1 |
| NFS backup mount and storage mapping | Pinned location matches |
| Embedded guest | `vitrial-prod-db`, source VM101 |
| Embedded disk index | Exactly one `drive-scsi0`, logical size 160 GiB |
| Inherited production NICs | **Two** |
| Inherited automatic boot and startup | **Present** |
| Inherited bootstrap/cloud-init-related settings | **Present** |
| Inherited hardware identifiers | **Present** |
| Archive file mode | **0644**; other-read permission bit set |
| Original archived config safe to boot as a recovery guest | **No** |
| Current checksum/full compressed payload verification | **Not performed** |
| PostgreSQL replay, WAL and tenant data recovery | **Not verified** |

A passing `technical_archive_metadata_pass` means only that the **expected
archive identity, metadata structure and inheritance baseline** were found.
It does **not** mean the archived production VM configuration is safe, that
data is restorable, or that the pinned historical SHA-256 was rechecked.
The previous SHA-256 pin is
`49b05b89aef9005bc93887ef128d12c00a2d9f9b54ca8809edc56cb331a856f8`.

### Two mandatory independent controls

**Recovery isolation:** The original VMA config contains production NICs,
auto-start, bootstrap settings and hardware identity. Any separately authorized
restore must explicitly construct and audit a **networkless VM9101** with
`onboot=0`, no production media/backing references, no bootstrap accounts,
and new guest identity. Verify its QEMU runtime and interfaces independently
*before* starting PostgreSQL. Never simply boot the archive's original config.

**Backup custody:** File mode 0644 is a real filesystem permission observation,
not proof that the archive is accessible or inaccessible to any particular
remote principal. NFS export ACLs, server-side controls, NFS AUTH_SYS
transport implications, audit logging, and whether the backup is approved for
custodial restoration require independent infrastructure and data-owner review.
Do not publish raw archived configuration or credentials to PR/Jira/CI.

The CLI emits `gate=HOLD`, `archived_config_safe_to_boot=false`,
`clone_config_scrubbing_required=true`, `restore_authorized=false`,
`production_authorized=false`, no executable commands, and exit 2
**even when the metadata is correct**. No customer-data restore is performed.
