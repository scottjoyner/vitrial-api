# Vitrial production recovery checkpoint — 2026-10-27

**Read-only evidence only. This document is not permission to mutate production.**

## Verified state (Fleet Commander on x1-370 → trusted SSH → Proxmox `one`)

- Proxmox host: live and authenticated; VM100 API, VM101 DB, VM102 object storage, VM111 OPNsense, and unrelated VM150 are all running.
- VM100 Docker `vitrial-api-production-api-1` is **unhealthy**, despite its old private-Caddy HTTPS `/health` passing.
- VM101 PostgreSQL `17/main` is **down** and `postgresql@17-main` is **failed**. Guest interface `eth0=10.0.3.28/24`; active included configuration line remains `listen_addresses = '10.77.0.10'`.
- VM102 RustFS and Caddy containers are up; this is **not** authenticated S3 read/write durability proof.
- VM111 OPNsense owns the public `.142` address. Its current PF rules still redirect Vitrial 80/443 for **`.139`**, which belongs to unrelated VM150. Preserve VM150.
- Public HTTPS `vitrial.38-143-56-142.sslip.io:443` rejects the requested SNI (`unrecognized_name`). VM100 private Caddy accepts the **old** `.139` hostname with valid HTTPS `/health` 200, but rejects the **new** `.142` SNI. This proves TWO routing/hostname configurations need review, not merely iOS URL replacement.
- Updated `scripts/audit_prod_vm_health_readonly.py` detects DB bind drift and exits with `release_authorized=false`, `gate=HOLD`. Eleven network-free tests passed and the current live read-only audit returned exit 2.

## October 27 verified VM101 backup custody (read-only, production unchanged)

A newer NFS archive was found through the Proxmox storage inventory:
`remote_nfs_backups:backup/vzdump-qemu-101-2026_10_27-05_06_48.vma.zst`
(611,157,962 bytes). Source SHA-256:
`ebd1c2f44afd9f3307f2263cda0a154171e56b2982af7f3836f7beb7e57e418f`.

Source archive passed `zstd -tq` and streaming `zstd -dc | vma verify -`
with exit 0. A separate encrypted, strict-host-key SSH transfer copied the
**exact same archive** to x1-370's private ext4 custody directory,
`/home/scott/.local/share/vitrial-recovery-custody/20261027/`.
The destination is mode 0700 and the file and receipt mode 0400; size,
SHA-256 and independent zstd validation passed. The copy did **not** go
onto shared plaintext NAS. The x1 local SSD provides a different
physical host, **not** offsite immutability or a proven PostgreSQL
recovery-point objective.

The earlier protected October 3/11/23 copies remain intact. No VM101 restart,
guest configuration mutation, data rewrite or customer-data API request
occurred. This archive was subsequently independently booted and SQL-validated
as described below. Production release remains **HOLD**.

## October 27 isolated PostgreSQL restore — authenticated SQL verified

Restored the SHA-verified archive to **new VM9148** on Proxmox `one`;
the restore did **not** overwrite production VM101 or prior scratch images.
Before boot VM9148 was verified stopped, `onboot=0`, with **no `netN`
or `ipconfigN` devices**. After a longer second boot, its guest agent
responded; guest IPv4 enumeration reported **only loopback
`127.0.0.1/8`**, not a LAN interface.

A full **read-only** ext4 `e2fsck -fn` on the restored disk returned
exit **0** and `global/pg_control` was present. PostgreSQL 17 was
started **only in VM9148**, using
`listen_addresses=127.0.0.1`, `archive_mode=off`, and
`archive_command=/bin/false`. Authenticated local PostgreSQL reads
confirmed:

- Public schema: **19 tables**, comprising `alembic_version` and
  **18 application tables**.
- Exact all-table census: **18/18 business tables contained zero rows**;
  `alembic_version` contained one row with revision `0007`.
- `pg_database_size('vitrial') = 8574643` bytes.
- Active listener exactly `127.0.0.1`; `archive_mode=off`;
  `pg_is_in_recovery()=false`.
- `pg_amcheck --database=vitrial --jobs=2 --install-missing`
  exited **0**; `pg_dump --schema-only --no-owner --no-privileges`
  exited **0** and wrote a 25,756-byte local scratch report.

PostgreSQL was stopped cleanly with `pg_ctl -m fast -w stop`. VM9148
was then shut down and verified **stopped, onboot=0, no NIC**.
The copied source archive and its private x1 custody receipt were unchanged.

This strengthens the repeated **empty pilot database** observation across
October 3, 11, 23 and 27 restored evidence. It does **not** establish
whether real customer data exists in a different authoritative database,
or verify continuous/offsite immutable WAL custody or S3 object integrity.
No permission to rewrite, purge, migrate, or repoint production is implied.

## VM100 private-route audit and VM102 authenticated S3 witness (October 27)

From authenticated Proxmox QGA on VM100, the running API container's
Docker inspect data was passed directly into the **redacted, read-only**
`audit_vitrial_private_dependencies.py` and
`plan_vm100_private_routes.py`. No secret values were printed or
written to a planning artifact.

| Endpoint setting | Observed VM100 runtime | Intended route |
|---|---|---|
| `DATABASE_URL` | `10.77.0.10:5432` | `10.0.3.28:5432` (VM101) |
| `S3_ENDPOINT_URL` | `10.77.0.11:9000` | `10.0.3.29:9000` (VM102) |

Both existing routes failed the current private-network match. The planner
validated one container with **34** environment entries and proposed
changing **only those two endpoint settings**. It returned
`PATCH_REQUIRED`, not approval to re-create the service. Its private
credential-preserving candidate was deliberately not serialized.

VM102 guest `eth0` independently confirmed `10.0.3.29/24`. A
credentialed, **read-only** boto3 `HeadBucket` probe was then run
*inside the existing VM100 application container*, replacing only the
in-memory S3 client endpoint with `http://10.0.3.29:9000`, using that
container's existing environment credentials without displaying them.
The selected bucket returned **HTTP 200**. No bucket/object list or data,
writes, or restart occurred. This proves authenticated access at the
**intended private S3 address**, not persisted live configuration,
object contents, restart durability, or full API readiness.

**Coordinated cutover dependency:** secure VM101 WAL custody and
approved PostgreSQL listener/archiver repair precede VM100 dependency
recreation; after VM100 DB and S3 links are corrected, independently
review VM100 Caddy hostname and VM111 OPNsense multi-tenant HTTPS routing.
Do not try a one-variable partial cutover or modify the unrelated
VM150/old `.139` public service. Keep `release_authorized=false`.

## S3 current and historical version integrity witness

The same VM100 container and existing S3 credentials were used **in memory**
against VM102 private `http://10.0.3.29:9000` for a strictly
read-only, bounded inventory. A complete `ListObjectsV2` page reported
**one current object of 52 bytes** (`IsTruncated=false`).
`HeadObject` and `GetObject` for that current object succeeded; length
and returned content's MD5 matched the object's 32-hex ETag.

A complete `ListObjectVersions` page reported **six data versions of
282 bytes in total** plus **five delete markers**
(`IsTruncated=false`). All six historical versions were read back
by explicit VersionId; each byte length and MD5/ETag check passed.
No object keys, payload bytes, bucket identities, API secrets or credentials
were printed. No S3 write/delete, replay, or version changes occurred.

This is **bounded object integrity proof for the currently visible
versioned bucket**, not independent immutable custody, full S3 API
recovery, approval to delete historical versions, or evidence that no
separate customer-data store exists. Preserve all six versions and
delete markers; changing the endpoint alone would not prove ownership
or satisfy production readiness.

## October 27 live WAL byte-level verification and remaining custody hold

All observations below were **read-only** on production VM101 while
PostgreSQL 17 remained stopped. The restored VM9148 remained separate.

- Local archive: 21 complete 16-MiB segments, hexadecimal suffixes
  `06`–`1A`. Guest-mounted NFS mirror: 26 complete 16-MiB
  segments `01`–`1A`. All **21 overlapping local and NFS files
  matched SHA-256 byte for byte**. Segment `1A` also matched the
  PostgreSQL data directory for a full three-way hash comparison.
- The *production data directory* contains five 16-MiB WAL files
  `1A`–`1E`. Suffixes `1B`–`1E` were **not present in either
  standalone archive**. All four latest files `1B`, `1C`,
  `1D`, `1E` matched the exact SHA-256 source digests recorded
  in the October 24 private custody receipt; each retained consistent
  inode, size and modification-time metadata during reading.
- A PostgreSQL 17 `pg_waldump` read-only scan beginning at
  `0/1A000000` emitted six WAL records, then stopped at
  `0/1B000060` with an invalid-record-length/zero-filled-tail
  diagnostic. That is **not** a full WAL replay or an approved RPO.
  Do not infer data corruption solely from the tail diagnostic.
- The Oct27 full VM101 `vzdump` backup has an independently checked
  copy on x1. An additional attempt to examine four WAL segments
  independently in isolated VM9148 did **not** complete: its guest
  agent never became ready; an offline read-only qcow2 mount attempt
  could not resolve the partition device. VM9148 was stopped, with
  `onboot=0`, no network interface, and the temporary NBD attachment
  verified disconnected. Do not claim that those backup-embedded WAL
  bytes were independently read or compared.
- Public trusted TLS at
  `https://vitrial.38-143-56-142.sslip.io/ready` still fails
  SNI negotiation (`tlsv1 unrecognized name`; HTTP status 000).
  Read-only guest inspection of VM111's active nginx configuration
  files found **zero `sslip.io` references**. No change was made
  to VM111, its other TLS sites, VM100 Caddy or VM150.

### Restricted NFS WAL custody copies created after byte verification

After confirming production PostgreSQL was **down**, a strictly
additive custody operation ran *as the postgres user* on VM101.
Exactly the four 16-MiB source segments `1B`–`1E` were copied
into a new, separate guest-mounted NFS directory:

`/mnt/vitrial-backups/vitrial/postgres/wal-custody-hold-20261027`

Source hashes were verified against the October 24 custody receipt
**before** the new directory was created. Each copy used a new
exclusive `.partial` file, a byte-count/SHA-256 check, file fsync,
and an atomic rename into its final name. A private
`SHA256_CUSTODY.json` manifest was written and fsynced. The
directory was created with mode `0700`, and the four segment
files have mode `0600`.

A second independent read of the new NFS directory confirmed
**four of four** full 16,777,216-byte files match the source
SHA-256 receipt, no `.partial` files remain, and the restrictive
permissions persist. Total newly preserved WAL: **67,108,864
bytes (64 MiB)**. There was no change to the source WAL,
existing active WAL archives, PostgreSQL configuration, or
service process. A subsequent `pg_lsclusters` confirmed the
production 17/main cluster was still **down**; scratch VM9148
remained **stopped**.

These are **standalone backup-custody copies in a private NFS
folder, not completed PostgreSQL archive_command output**.
The NFS share is not independently immutable/offsite/encrypted
and a verified recovery-point replay is still unavailable.

**Status: local/offhost mirrored WAL bytes verified and the
four newer segments separately preserved on NFS, but immutable
offsite custody, approved replay/RPO and production recovery
authorization remain HOLD. Public ingress/Apple TestFlight
remain HOLD.**

## Second physical-host custody of the four newest WAL files

After verified VM101 NFS WAL backups, all four `1B`–`1E`
files were transferred over authenticated, strict-host-key SSH
**from Proxmox's NFS mount to x1-370's separate local SSD**.
They were *not* stored on the shared CIFS `/nas` mount.

Exact destination:
`/home/scott/.local/share/vitrial-recovery-custody/20261027/wal-1b-1e`

The destination is on x1's independent 4-TB local NVMe/ext4
mount. Each 16,777,216-byte incoming file used a unique partial
path, was SHA-256 checked against the previously recorded
live VM101 source receipt, and was only then renamed into its
final name. Directory mode is `0700`, WAL files and the
custody manifest are `0400`; the directory was fsynced.
A fresh, independent x1 readback reported **4/4 complete
SHA-256 matches**, 0 partials, and verified permissions.
Total copied bytes: **67,108,864 (64 MiB)**.

This materially improves resilience against loss of either
the VM101 host's live disk or the shared NFS server. It does
**not** establish a geographically independent/offsite copy,
WORM/immutable retention, an operator-approved encryption
recipient and recoverable key, or complete PostgreSQL PITR.
Live VM101 PostgreSQL remained down, VM9148 remained stopped,
and no source WAL/configuration was modified.

## PostgreSQL API authentication CIDR is a third required cutover file

VM100's guest `eth0` confirms the API client is
`10.0.3.27/24`. Read-only inspection of the actual VM101
`/etc/postgresql/17/main/pg_hba.conf` found the sole remote API
rule at line 133:

`host vitrial vitrial 10.77.0.2/32 scram-sha-256`

VM101 therefore would reject authenticated traffic from the API
even after repairing the listener and VM100 `DATABASE_URL`.
Do **not** use a wildcard CIDR; the narrowly scoped proposed
client rule is:

`host vitrial vitrial 10.0.3.27/32 scram-sha-256`

The new **read-only** `scripts/plan_vm101_hba_cidr.py`
checks for exactly one original rule, rejects broad/duplicate/
unrecognized rules, and produces SHA-256 digests of the unchanged
file and exact CIDR-only candidate. It neither writes the config
nor restarts PostgreSQL. **13 synthetic tests passed**.
Execution against live VM101 returned:

- `status=CANDIDATE_ONLY_HOLD`
- current SHA-256:
  `c160e6dc4f7188d6b90c530aa6d516355cdfdc4fa939a8b947ef7e1dff355c6d`
- proposed SHA-256:
  `a71a0b83f0c084c39c13a6f212765bcd5e9ee0c26c6d83e66f7057913aa97f75`
- `changed_rules=1`, `production_modified=false`,
  `postgres_restarted=false`, `release_authorized=false`.

Production PostgreSQL recovery now needs a **coordinated
three-file review**: listener, archived WAL command, and this
client CIDR, plus the two VM100 dependency endpoint changes.
All existing settings and rollback files must remain preserved.

## Pristine October 27 VM101 archive WAL witness (independent of started scratch)

An untouched VM101 `vzdump` archive was located on the Proxmox NFS
backup store at
`/mnt/nfs_arrg/ProxmoxBackups/dump/vzdump-qemu-101-2026_10_27-05_06_48.vma.zst`.
Its SHA-256 `ebd1c2f44afd9f3307f2263cda0a154171e56b2982af7f3836f7beb7e57e418f`
matched the independent existing x1 custody receipt. `zstd -dc |
vma list -` identified the expected VM101 160-GiB disk.

The original archive was decompressed with `vma extract` into a
new root-private forensic directory
`/var/lib/vz/vitrial-archive-pristine-20261027`.
Its extracted raw disk was sparsely stored (~1.7 GiB actual, 160
GiB logical). Root directory mode was `0700`, extracted files
`0600`. The GPT partition 1 filesystem was mounted on a
read-only loop attachment with `ro,noload,nosuid,nodev,noexec`
and the mount was unmounted. No guest boot, Postgres start, or
write to the archive or extracted filesystem occurred.

**All four** critical 16-MiB `pg_wal` segments extracted from
the *pristine backup*, `1B`–`1E`, had exact SHA-256 matches
to the earlier signed/source custody receipt:

- `1B`: `a2c72a608912747365fc0d99c2aca4836c8c9cd5706f5ee9fe323ef5ee0052d5`
- `1C`: `8a9aab5133c6c1e29ec1c245f4037a9735e59ffb2d58a5b6298b0d9ee9248c60`
- `1D`: `5eaca349b48024121dad28c02b274c0b03c6844b7cdedc2ba57dcf8644e12ece`
- `1E`: `8b8aac29ceb5afe6b40ac2b4379e2871916767bf8c463f7133949f5f20728e26`

This is stronger than the earlier running-scratch witness: after
the isolated VM9148 PostgreSQL startup, its WAL directory showed
`1C` with a changed digest, `1B` absent, and newly present
`1F`/`20`. The post-start scratch directory must **not** be
mistaken for pristine source backup bytes. Those differences
alone are not evidence of source data corruption; comparing the
intact, untouched source archive now proves the four WAL
segments were present and byte-identical **before** isolated startup.

**Remaining HOLD:** full PostgreSQL WAL replay to an approved
point, authoritative customer-data reconciliation, independently
immutable/offsite encrypted custody with recoverable keys, safe
production restart/rollback and controlled client routing. These
hash checks do not authorize live database restart or distribution.

## Review/repair order

1. Preserve guest configuration and identify a current independently restorable VM101 PostgreSQL backup, its storage owner, and rollback plan. Validate an **isolated** recovery copy before opening the running production data directory. Snapshot success alone is insufficient.
2. Following DBA/owner approval, repair VM101's stale `listen_addresses` to the **actual, reviewed** private interface (not an inherited `10.77.0.x` address). Review narrow `pg_hba.conf` permissions for only the API principal and correct subnet; never broaden access to all WAN clients. Start PostgreSQL only with backup/integrity/fencing checks satisfied. Verify `pg_lsclusters`, `pg_isready`, and authenticated least-privilege readback. Do not auto-replay failed writes.
3. Prove object-storage availability and authorized scoped S3 persistence/restart behavior. Mere container uptime is not sufficient.
4. After DB/S3 proof, verify exact deployed API image digest, schema compatibility, Docker health, and `/ready` HTTP 200 reporting `status=ready`, `database=ok`, `objectStorage=ok`; collect version and no-data-leak identity checks.
5. Review VM111 OPNsense nginx/ACME configuration and existing virtual hosts. Allocate a valid **publicly trusted** TLS certificate for the selected Vitrial hostname on `.142`; route only that SNI to the intended private API guest. Independently update VM100 Caddy's `API_HOST` or reviewed upstream SNI configuration. Preserve all other SNI sites and unrelated VM150 `.139` service. Do **not** blindly forward OPNsense's current management 443 listener to VM100.
6. Test outside-LAN and cellular trusted TLS plus `/health`, `/ready`, version, rejection of redirects or mixed HTTP and wrong-scope auth. Run the iOS TestFlight preflight with no disabled certificate validation.
7. Integrate iOS zero-URL changes on the **canonical** Astrix source, reconcile the prior reported `0.3.1(138)` build, verify the next unused build number through App Store Connect **read-only**, test exact signed device binary (including Alex's iPhone XR), then upload/assign only after approval gates.

## Rollback and change boundaries

Always retain previous OPNsense virtual-host config, firewall/NAT snapshot, source Caddy config, VM101 PostgreSQL config bytes/permissions, and exact API immutable image digest. Prove the rollback steps before touching shared ingress or a database. No production PostgreSQL, OPNsense, Caddy, VM150, Apple or customer-data mutation was executed while gathering this checkpoint.

A successful dry-run or passing isolated unit tests **cannot** change this document's HOLD status.
