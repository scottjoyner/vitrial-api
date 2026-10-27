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
