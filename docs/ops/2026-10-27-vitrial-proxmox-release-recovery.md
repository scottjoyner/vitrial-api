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
