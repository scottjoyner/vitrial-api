# Vitrial engineering recovery and customer pilot — open-issue dependency plan

**Reviewed:** October 17–18, 2026. **Owner:** engineering/release review.
**Production gate:** HOLD until independently attested VM101 disaster recovery, tenant security, storage, ingress and exact-device release acceptance. This plan does not authorize copying customer data, applying migrations to VM101, deploying API to production, or publishing TestFlight.

## Current evidence and non-negotiable boundaries

- Production VM101 QEMU reports running, but PostgreSQL 17/main remains failed. The obsolete `10.77.0.10` listener is the leading diagnosis, not a verified healthy database. VM100 DB and object endpoints also need repair; do not treat CI's disposable PostgreSQL/S3 tests as production acceptance.
- Selected October 17 `vzdump-qemu-101-2026_10_17-05_01_05.vma.zst` has confirmed VMA header and 160 GiB logical disk metadata; full PostgreSQL recovery, backup chain and WAL/tenant integrity remain unproven. The archived VM config inherits two NICs, autostart, bootstrap and hardware IDs: never boot it unchanged.
- Five allocated WAL segments are **not** 80 MiB of valid WAL. A bounded `pg_waldump` pass decoded five records and then encountered a zero-length boundary; only isolated replay can prove recovery.
- Data custodian, DBA, Proxmox/storage owner, and release reviewer must independently authenticate the exact backup, time window and isolated recovery plan. Existing cryptographic receipt verifier does not by itself establish out-of-band trust-root custody or durable replay protection.
- The API demo must ultimately prove a company with multiple independent members sharing permitted customers/jobs while denying cross-company access. Offline iOS flows must still function without a reachable API.

## Ordered critical path and grouped issues

| Phase | Blocking issues | Work and tangible evidence | Ready-to-exit definition |
|---|---|---|---|
| 0 — recovery custody | SCRUM-29, SCRUM-39, SCRUM-40, GitHub PR #77 | Pin exact archive checksum and custody; obtain four independent approvals; prepare networkless VM9101 on separate local disk only under that authorization; actual PostgreSQL recovery/WAL, tenant/customer, RPO/RTO audit | Signed custody/restore evidence reviewed externally; isolated guest produces independent PG/data proof |
| 1 — private infrastructure | SCRUM-41, SCRUM-42, SCRUM-27, SCRUM-30, SCRUM-31, GitHub #28/#30 | After phase 0, review exact-file-hash rollback packet for listener `10.0.3.28` and API `10.0.3.27/32`, SCRAM/TLS; repair VM100 private DB/S3 endpoints; prove RustFS durability, OPNsense public TLS, metrics and negative network clients | Fresh authenticated readiness + restoration, rollback and independent operator approval; no public promotion beforehand |
| 2 — enterprise authorization | SCRUM-48, SCRUM-59, SCRUM-46, SCRUM-47, SCRUM-62, SCRUM-63, SCRUM-64, GitHub #28 | Company provisioning, membership/roles, seat enforcement, hard tenant scoping in every route and sync, cross-tenant negative matrix | Two companies, three distinct seats; unauthorized customer/item/quote reads and mutations denied; migration/rollback in disposable PostgreSQL |
| 3 — catalog + quote domain | SCRUM-44, SCRUM-61, SCRUM-50, SCRUM-51, SCRUM-52, SCRUM-60, SCRUM-66, SCRUM-68, GitHub #64/#65 | Approved sixteen finished-item templates separate from twenty quotation rows; company catalog/BOM revisions; deterministic text-first item geometry and editable materials; reconcile COP totals and supplier/pricebook provenance | Pin source/price authority and assembly snapshot/version hashes; golden Villa Camila quote totals match source; no accidental inventory semantics |
| 4 — synchronized device experience | SCRUM-49, SCRUM-54, SCRUM-65, SCRUM-67, SCRUM-34, SCRUM-35, SCRUM-53, SCRUM-19, SCRUM-22 | Two authenticated devices and users share permitted customer→job→item lifecycle; non-LiDAR width/height manual entry; iOS offline drafts/sync conflicts; client-safe endpoint selection and home | Actual two-device evidence with independent accounts, offline/relaunch proof, retry and idempotence; no cross-tenant leakage |
| 5 — exact release signoff | SCRUM-57, SCRUM-69, SCRUM-37, SCRUM-43, SCRUM-33, SCRUM-38, SCRUM-25 | Repaired macOS signing/runner; exact signed uploaded physical build; three real seats and customer walkthrough; API/ingress release evidence | Independent owner/client physical iOS acceptance, precise build number, rollback target, no unresolved P0 blockers |
| Follow-on | SCRUM-55, SCRUM-56, SCRUM-58, SCRUM-36, GitHub #2 | More sectors/currencies; paid tier billing; richer AR/design preview after manual happy path | Scope explicitly approved; may not block the minimum text-first pilot except for a customer-confirmed requirement |

Older quotation lifecycle issues SCRUM-1/2/3/4/5/6/7/8/9/10/11/12/13/14/15/16/17/18/20/21/23/24/32 are retained as their corresponding domain's regression-contract backlog, rather than silently declared complete. Account for them in the relevant feature owner reviews. GitHub issues #64/#65 remain open until manufacturing/price-book authority and revisioned canonical snapshots are proven.

## Parallel tracks and what can ship

**Safe now:** Python and Swift code-only changes, deterministic fixtures, disposable container integration tests, hosted CI, private read-only fleet status/evidence probes, and documentation. Publish draft PRs with exact commit/test evidence; do not mark customer acceptance merely because synthetic tests pass.

**Can deploy only to approved disposable development/staging:** tenant/BOM/auth changes with isolated databases and synthetic data after independent test results and stage operator approval. Absolutely no production migration, customer archive restore, live pricebook replacement or public ingress alteration.

**Must not deploy to production yet:** any VM101/VM100 configuration, PostgreSQL start/repair, backup read/write/extraction for customer data, RustFS cutover, credentials/entitlements change, production API container or external TestFlight build. The critical path is an independently approved, full isolated restore plus gate-by-gate functional/user acceptance.

## Acceptance and rollback contract

- Each PR references Jira keys, a precise base/head SHA, changed contracts, adversarial authorization tests, reproducible pinned dependencies, and actual CI status.
- Approval documents must be obtained from human owners and verified against independent trust roots and replay ledger; do not synthesize receipts or equate a user's generic "continue" with these approvals.
- Refuse stale observations: the five-minute recovery evidence window starts **before** the first remote probe and is invalidated on timeout or clock drift.
- Stage deployment uses a restricted, read-only, manually invoked x1-370 probe with a pinned Git commit. Store summaries in an operator-owned 0700 directory with 0600 exclusive files; never publish raw PostgreSQL, backup configuration, account credentials or customer records.
- If any gate fails or a source hash/VMID/identity changes: stop, preserve exact source state and file hashes, never auto-retry mutations, and escalate to the custodian. The rollback packet is proposal-only until a real authorized maintenance window.
- Release decision needs separate technical, DBA/custody, security, customer and iOS physical-device signoffs; no single CI flag or synthetic fixture is sufficient.

**Immediate next engineering slices:** (1) ship fail-closed snapshot freshness hardening, (2) complete negative cross-company authorization tests on the tenant branch, (3) make the Villa Camila source-backed fixture immutable and test COP totals, (4) obtain actual isolated recovery authorization before any database/data-plane deployment.
