# SCRUM-64 — Cross-company top-level tenant claims and sync regression

**State:** engineering branch ready for canonical review, not production.
**Scope:** server-owned organization boundary on existing SCRUM-47/48 company
membership pilot, V1/V2 sync reads/writes, adversarial overlapping Customer ID.
All users/data in this test are synthetic; production VM101/VM100 untouched.

## Problem

A client could submit a canonical Customer/Project/Item/Quotation sync payload
with a top-level `organizationID` that did not match the authenticated
membership. Existing database composite primary keys correctly held data under
the authenticated organization, but the mismatched marker could be persisted
inside the JSON payload and shown on later pulls. That creates a misleading
tenant identity inside otherwise company-scoped data.

## Change and contract

- New `_require_matching_tenant_claim` is invoked from shared `authorize_record`
  before entity-type dispatch. V1 and V2 push both use that boundary.
- If a payload includes any top-level `organizationID`, `organizationId`,
  or `organization_id`, each claim must be an exact string match to the
  authenticated `principal.organization_id`; nonstrings or conflicting aliases
  are rejected. The server does not use a payload marker to choose the tenant.
- Older payloads with no organization marker continue to work. This change
  does not inspect arbitrary nested business metadata or alter canonical
  composite keys, historical data, or database migrations.
- Rejection consumes the specified client mutation ID with the existing
  idempotence/rejection receipt; clients must use a **new** mutation ID after
  correcting a rejected request. No cross-company entity or price data is
  returned on rejection.

## Fleet Commander test evidence

- Source: `app/ownership.py`
- Regression: `tests/test_scrum64_tenant_claims_postgres.py`
- Python unit suite without external dependencies: **395 passed, 67 skipped**.
  The skipped tests are not reported as successful integration.
- Actual disposable PostgreSQL 17, on **127.0.0.1 only**, migrated to Alembic
  revision `0008`: focused tenant suite **12 passed, 0 skipped**.
- Two separately authenticated synthetic companies have **the same** customer
  entity ID. Tenant A cannot persist a payload marked as company B, while both
  companies can independently create and pull their own records. V1/V2 pulls
  contain exactly their own marker and no other company's payload. A forged
  query `organizationID` has no effect. A second hostile update cannot
  replace the other company's record. An unauthenticated V2 pull is denied.
- API deployment, if performed, is only a short-lived loopback Uvicorn process
  using the disposable database and synthetic credentials; stop it after
  acceptance. Do not deploy or migrate Proxmox API/DB.

## Remaining critical path

1. **SCRUM-64 / SCRUM-48:** complete negative matrix for all canonical entity
   types (Customer, Job, Sector, Item, Item child, Quotation, Delivery),
   sync V1 and V2, reference catalog/price publications, evidence binaries,
   offline tenant switching, role/capability and revocation. This slice
   verifies only overlapping Customer ID and top-level tenant claims.
2. **SCRUM-50/51/52 + SCRUM-44:** use 16 approved finished-product templates,
   20 actual Villa Camila quotation rows, and verify COP 64,245,000 with frozen
   publication and pricing authority; never synthesize customer acceptance.
3. **SCRUM-40/41/42:** independently authorized VM101 isolated restore/WAL and
   tenant records acceptance, then separate reversible VM100 private endpoint,
   object store, ingress and production API deployment. The recovery HOLD is
   unaffected by this synthetic tenancy test.
4. **SCRUM-49/57/65/69:** two human accounts, real iOS devices, offline/online
   persistence and exact TestFlight build acceptance.

The canonical `astrix-group-accounts/server-api` remote was not writeable
from the connected Fleet Commander session during the previous checkpoint.
Do not equate a local branch and test evidence with an approved upstream PR or
hosted exact-head CI. A reviewed integration/merge and independent operator
production signoff are still required.
