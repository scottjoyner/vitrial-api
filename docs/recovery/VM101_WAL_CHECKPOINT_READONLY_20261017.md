# VM101 PostgreSQL 17 WAL checkpoint probe — read-only

**October 17, 2026. Overall recovery gate: HOLD.** This checks only the
currently mounted, stopped production VM101 data directory using existing
PostgreSQL 17 binaries. It does NOT open or verify the October 17 VMA.zst
backup. That archive may contain different data/WAL than this live disk.

Run from the pinned recovery branch on x1-370 via Fleet Commander:

    python3 scripts/audit_vm101_wal_checkpoint_readonly.py --proxmox-host root@38.143.56.138

The tool executes bounded guest-agent read-only queries and converts raw data
to small allowlisted booleans and counts **inside VM101**. It does not export
WAL records, database identifiers, record payload, customer rows, secrets,
journal text or any raw PostgreSQL config. It neither starts PostgreSQL nor
writes/repairs data files.

## Actual observed current VM101 state

- `pg_controldata`: control-file state `in production` despite failed/stopped
  PostgreSQL 17/main service, requiring later validated crash/WAL recovery.
- Latest checkpoint location: `0/1A0000B8`.
- Latest checkpoint REDO location: `0/1A000060`, WAL file
  `00000001000000000000001A`, timeline 1.
- Five consecutive 16 MiB local WAL segments, `...1A` through `...1E`.
  No nonstandard-length segments observed; 2 `.done` archive-status files
  and 0 `.ready` markers at the time of the separate metadata inventory.
- PostgreSQL 17 `pg_waldump` decoded **one WAL record starting from each of
  the two checkpoint-related locations** with exit code 0 and no stderr.
  Its output was discarded inside the guest, not copied out.
- **Extended bounded WAL-prefix probe (October 17, ~20:55 UTC):**
  `pg_waldump --quiet` from REDO `0/1A000060` decoded **five records**;
  requesting the sixth returned `invalid record length` with `got 0`
  at `0/1B000060`. WAL content and raw errors were discarded or
  classified inside the guest. This **can be normal zero-filled end-of-
  written-WAL padding** inside a preallocated segment. It does not prove
  a clean end, full WAL validity, or a recoverable transaction stream.
  Five 16 MiB allocated WAL files do NOT imply 80 MiB of valid records.
- `Data page checksum version: 0`: page checksums disabled. Detecting
  storage-level page corruption therefore requires other validation methods.

These support the specific claim **checkpoint locations are initially
decodable from the current source's local WAL**. They do NOT establish that
all WAL records decode; that all required WAL exists at the target recovery
point; that checkpoint replay will succeed; that backup + WAL match; that
data pages or PostgreSQL catalogs are correct; or that customer/tenant
records can be recovered and verified.

## Required next acceptance

1. Collect actual four-role independent authorization, trusted signing-key
   and customer-data custody evidence for isolated VM9101 validation.
2. Refresh the five-minute Proxmox/precreation/disk audit; prohibit guest
   NICs, boot-on-creation, shared production backing disks and NFS writes.
3. Under approved custody, independently restore the **exact** October 17
   archive to networkless isolated disk/VM; inspect its **own**
   `pg_controldata` and complete WAL segments (not these live-source files).
4. Prove a full bounded PostgreSQL crash recovery on the isolated restored
   copy; verify all expected tenant/customer/quote/job records against a
   separately approved baseline, SQL integrity, recovery point and RTO/RPO.
5. Independently review the byte-exact October 9/17 PostgreSQL listener/HBA
   cutover and rollback packet, TLS policy and authorized maintenance
   window before editing or restarting production VM101.
6. Validate API DB/S3 endpoints, object recovery, public ingress and user
   authorization separately before removing the production deployment HOLD.

The script always reports `gate=HOLD` and exits 2, even when the bounded
WAL prefix, metadata and checkpoint records pass. `wal_first_boundary`
is descriptive only: `wal_end_of_written_data_proven` remains false. `full_replay_verified`,
`whole_wal_stream_verified`, `tenant_data_verified` and both production
and restore authorization remain false. No automation executes a restore.
