# SCRUM-64 — Legacy tenant-claim quarantine and composable integration tests

**Status:** scoped local code change and disposable PostgreSQL acceptance. Not a
canonical merge or production deployment. Build on the SCRUM-47/48/64 security
branch; preserve its existing review and physical acceptance gates.

## Finding

The V1/V2 inbound tenant-claim guard added in SCRUM-64 rejects conflicting
`organizationID`, `organizationId` and `organization_id` in new mutations.
However, legacy `sync_entities.payload_json` rows created before that guard
could retain an invalid tenant marker, and `pull_since` would serialize that
payload to clients even though the canonical database record belongs to the
authenticated company. Database composite keys alone therefore did not
guarantee trustworthy tenant identity **inside** the payload.

## Implementation

- `app/tenant_claims.py`: pure, exact, case-sensitive validator for the three
  known optional top-level tenant aliases. It refuses mismatches and
  non-dictionary payloads; legitimate records can omit all tenant hints.
- `app/ownership.py`: delegates existing incoming sync authorization to that
  shared predicate and continues to reject mismatches before any customer,
  project, item or quotation ownership plan is applied.
- `app/sync_service.py`: before emitting a visible `SyncEntity`, V1 and V2
  now check its stored top-level tenant hints. If mismatched or malformed,
  **withhold that record**, advance past its change-log position (like an
  undeliverable record), and emit only hashed/categorized observability fields.
  Do not include raw payload contents, customer names or identifiers in
  operational log events.
- A rejected historical record is **not deleted, overwritten or migrated** by
  a read request. Database/source records remain intact for an independently
  approved cleanup or provenance investigation.
- Tombstones with `payload_json=None` retain their existing empty-payload
  semantics. The cursor still advances, and a later good record in the same
  tenant's page remains deliverable.

## Regression tests and scope

- `tests/test_scrum64_historical_pull_quarantine_postgres.py` seeds
  deliberately invalid **historical** JSON directly into a disposable migrated
  PostgreSQL 17 database: the test creates two separate authenticated
  organizations and overlapping canonical customer IDs; each organization's
  V1/V2 pull is checked for tenant isolation. Three tenant hint aliases are
  exercised independently. A subsequent safe record must still arrive.
  The poisoned source row must remain unchanged in PostgreSQL.
- `tests/test_scrum64_tenant_claims_postgres.py` continues to cover forged
  V1/V2 mutations, cross-company overwrite attempts, omitted legacy hints,
  and unauthorized requests.
- `tests/test_sync_pull_cursor_postgres.py` had an unrelated **global table
  cleanup** that destroyed other tests' tenant data and collided with foreign
  keys. Its cleanup is now scoped to a test-only organization and user. This
  test previously hardcoded global BIGSERIAL sequence values, colliding with
  legitimate records inserted by other suites; it now uses PostgreSQL-generated
  sequence values and asserts relative cursor bounds. Previously unawaited
  async `flush` calls are also corrected.
- Full dependency-free Python suite: **395 passed, 85 skipped** (externally
  dependent tests are not counted as passing). Combined real PostgreSQL suite
  covering historical guard, new writes, company membership, scope visibility
  and cursor pagination: **81 passed, 0 skipped** on PostgreSQL 17 with Alembic
  `0008`, bound to loopback using synthetic credentials. Real TCP loopback
  smoke and exact final commit review must be recorded separately before
  calling the branch staged.

## Open release gates and execution order

1. **SCRUM-64 / 48 / 59:** complete negative authorization matrix across
   Customer, Job/Project, Item/Sector, Quotation, price/reference catalog,
   evidence bytes, tenant switching and revoked sessions. This slice protects
   historical sync payload tenant hints, **not every business field**.
2. **SCRUM-46/47/62/63:** integrate tenant bootstrap/invitation/member role
   checks into the canonical `astrix-group-accounts/server-api` repository.
   Full source review and exact-head hosted CI are mandatory; the canonical
   GitHub connection was not writeable on the last checkpoint.
3. **SCRUM-44/50/51/52/60/61/66/68:** align 16 approved finished-item templates
   and 20 source-backed Villa Camila quotation rows with frozen supplier BOM,
   price publications and COP reconciliation; do not invent materials/prices.
4. **SCRUM-29/39/40/41/42:** independently approved VM101 PostgreSQL VMA restore
   and WAL + customer-data recovery on an isolated, networkless VM9101; then
   reversible private endpoint, RustFS, TLS and observability cutovers under
   separate release review. The production database is still NOT verified.
5. **SCRUM-49/57/65/69:** exact signed iOS/TestFlight build and independently
   authenticated two/three-seat physical acceptance with offline sync proof.

**No production release authority** is inferred from a successful synthetic
PostgreSQL run, staged Uvicorn process, draft PR, Git bundle, or generic user
request to continue. Keep production NO-GO/HOLD until independent approvals.

## Exact implementation smoke — observed October 18

- Initial implementation commit: `d85fada523934c002009055279c64cf422c42a39`.
- A temporary Uvicorn instance ran that exact clean commit **only on the x1-370
  loopback interface** against the disposable PostgreSQL 17 database (Alembic
  `0008`), and was stopped after the one-shot smoke test.
- Actual TCP results: `/health=200`, `/ready=200`, anonymous V1 sync=401,
  anonymous V2 sync=401. The staging API was confirmed stopped.
- The combined real-PostgreSQL authorization/sync suite finished with
  **81 passed / 0 skipped**, including historical-data quarantine, tenant
  mutation denial, company seats and scoped sync cursor pagination.
- During combination testing, legacy cursor tests were found to delete
  *all* tenants' data and to hardcode globally unique change-log sequences.
  Both test-only defects were corrected and the combined suite rerun green.
- A full local no-integration run yielded **395 passed / 85 skipped**.
- Canonical upstream access is **unavailable**: SSH-less Git HTTPS remote
  cannot authenticate and the connected GitHub integration returns HTTP 404
  for `astrix-group-accounts/server-api` as of this checkpoint. No upstream PR,
  canonical hosted CI or production API deploy is claimed.
