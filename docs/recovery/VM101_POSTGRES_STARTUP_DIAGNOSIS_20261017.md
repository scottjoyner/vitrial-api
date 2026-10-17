# VM101 October 6 PostgreSQL startup failure — read-only incident reconstruction

**October 17 2026. Status: HOLD.** The new read-only Fleet Commander witness
classifies PostgreSQL's historical systemd journal *inside VM101*, returning
only allowlisted booleans and state classifications, not raw logs or secrets.

Run:

    python3 scripts/diagnose_vm101_postgres_startup_readonly.py --proxmox-host root@38.143.56.138

On live PVE node one, VM101's guest agent reported:
- PostgreSQL 17/main systemd unit `failed`, `Result=protocol`, `Type=forking`;
  last inactive transition October 6 at 21:05 UTC; no TCP 5432 listener.
- Current PostgreSQL listener configuration still refers to obsolete
  `10.77.0.10`, not VM101's current `10.0.3.28`.
- Bounded **historical journal window October 6 20:30–October 7 00:00 UTC**
  had 14 lines, including categorized address-bind failure, missing PID-file,
  and systemd protocol/startup failure markers. This is incident context,
  not proof of current file-system or WAL integrity.
- PostgreSQL control-file state read while the cluster was stopped remains
  `in production`, so PostgreSQL crash recovery/WAL continuity is unresolved.
- Historical journal did not contain known matching WAL, disk-space or
  configuration-parser diagnostic phrases. **Absence of matched phrases
  is not evidence that those systems are healthy.**

**Narrowed hypothesis:** the old listener address likely caused PostgreSQL
startup to fail before systemd could observe the expected PID file. The
diagnostic emits `legacy_address_bind_failure_supported`; it deliberately
sets `root_cause_proven=false`, `postgresql_recovery_proven=false`,
`production_authorized=false`, and `gate=HOLD` (exit code 2).

## Correct next operator sequence

1. Obtain authentic independent DBA/customer data-custodian approval for the
   exact Oct17 archive, isolated no-NIC VM9101 data placement and rollback.
2. Re-run the five-minute precreation audit and source QCOW2 chain audit.
3. Under approved isolation, recover the backup to a physically separate,
   independently auditable VM9101 disk. Preboot audit must reject inherited
   NICs, guest identity, cloud-init or production backing files. Inspect actual
   QEMU runtime and guest loopback interfaces before any database operation.
4. Prove PostgreSQL crash recovery using its actual data and WAL timeline;
   verify tenant/customer/job/quote data and the DBA's accepted recovery
   point. Never substitute a `pg_resetwal`, `initdb`, synthetic customer
   fixture, or force-start in production.
5. **Only after independent production maintenance authorization**, render
   byte-exact rollback/diff from newly verified VM101 config hashes; consider
   the narrow listener change to `10.0.3.28` and API HBA `10.0.3.27/32`,
   retaining SCRAM and avoiding broad network CIDRs. Independently test on
   the approved isolated clone before applying any production change.
6. Repair VM100's stale DB/S3 endpoint settings and confirm authenticated
   readiness, object durability and ingress only within a separate approved
   deployment window. TestFlight promotion remains separately gated.

No repair commands are generated or executed by the diagnostic. A failed
PostgreSQL service while the QEMU VM is running does not make the archived
VM101 backup recoverable or production-ready.
