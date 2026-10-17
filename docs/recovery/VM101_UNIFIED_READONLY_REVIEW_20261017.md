# VM101 unified read-only recovery review

**Purpose:** a point-in-time, non-authorizing snapshot of the existing precreation,
source disk, recovery clone quarantine, clone disk-chain and signed-approval gates.
This tool does not issue QEMU/PG commands that change state. It does not traverse
backup archive contents or attempt WAL replay.

Run from an authorized x1-370 Fleet Commander session using the pinned PR branch:

    python3 scripts/collect_vm101_recovery_review_readonly.py --proxmox-host root@38.143.56.138

To preserve a local protected review record, use a pre-existing private 0700
directory owned by the invoking operator:

    umask 077
    python3 scripts/collect_vm101_recovery_review_readonly.py \
      --proxmox-host root@38.143.56.138 \
      --output /home/scott/agent-sprints/20261017/vm101-recovery-private/review-unique.json

Output files are exclusively created with mode 0600. An existing file, a
symlinked destination, or an unsafe output parent fails closed. Do not publish
raw private evidence or owner-trust records to GitHub, Jira, or CI.

If independently signed approval documents become available, review their
authenticity and custody *outside* this tool, then use the optional
--approval-bundle and --trust-keys arguments together. Never generate keys or
claim human signatures from synthetic test fixtures.

The CLI deliberately exits 2 and prints gate=HOLD in every situation, including
when every individual technical check passes. Its SHA-256 checksum only detects
accidental modifications to the JSON summary; it is **not** an authenticity
signature, a nonce replay ledger, or independent operator approval. The report
expires in five minutes and must be re-collected before a separately authorized
maintenance window.

Expected October 17 observed state:

- precreation PASS: source identity, candidate VMID available, private local
  image storage and space budget as of the read-only snapshot.
- source_disk PASS: VM101 QCOW2 metadata has matching format, capacity and
  no reported external backing/data file; no whole-file content validation.
- postgres_startup diagnostic INSPECTION PASS: bounded October 6 journal
  markers support an obsolete listener-address binding failure hypothesis;
  See `VM101_POSTGRES_STARTUP_DIAGNOSIS_20261017.md`. This does not mean
  the PostgreSQL service is healthy or that root cause is independently proven.
- clone_quarantine UNKNOWN/FAIL: VM9101 does not exist.
- clone_disk UNKNOWN/FAIL: independent VM9101 disk does not exist.
- signed_approvals UNKNOWN/FAIL: no authentic four-role human approval bundle
  provided. The cryptographic review cannot itself prove trust-root custody.
- PostgreSQL recovery, WAL continuity, tenant/customer records and current
  production application dependencies: not proven by this tool.

**STOP conditions:** any stale or ambiguous snapshot, failed live query, changed
disk/source identity, unexpected clone state, missing authentic signoff, or new
data-custody concern blocks any restore. Never treat an API/CD/CI green result
as DBA/data-custodian permission to copy customer data. Production VM101
configuration, migrations, external ingress, API deployment and iOS/TestFlight
remain independent change gates.
