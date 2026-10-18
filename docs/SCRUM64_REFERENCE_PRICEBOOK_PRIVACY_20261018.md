# SCRUM-64: Reference pricing response privacy and cross-company regression

**Date:** 2026-10-18. **Status:** tested local feature branch; no production authorization.

## Risk and bounded scope

Existing reference-data API GET routes can return company-specific supplier
materials, catalog metadata and full price-book unit amounts but did not
explicitly prevent intermediary/browser caching. A successful publication POST
also returns the full published payload. The response security envelope must
be consistent, even if the client or reverse proxy changes.

The app already scopes all publication lookups with the authenticated
organization's composite DB identity. This slice preserves that authority,
does not invent a supplier pricing permission, and does not reclassify the
company's commercial contract. **Whether restricted same-company members
should have price-book read access requires a separate approved role matrix.**

## Implementation

- `app/reference_routes.py` introduces one common response helper that adds
  `Cache-Control: private, no-store` and
  `X-Content-Type-Options: nosniff` to successful authenticated reference-data
  manifest/current/list/direct-publication GETs and publication POST.
- No change to immutable publication hashing, predecessor/version linkage,
  source cost, currency, tenant selection, or `pricing.manage` /
  `catalog.manage` publication-write enforcement.
- No production migrations or data repair.

## Adversarial test

`tests/test_scrum64_reference_privacy_postgres.py` invokes the real FastAPI
ASGI handlers against freshly migrated **disposable PostgreSQL 17** with
Alembic `0008`, two synthetic authenticated organizations, real DB sessions,
and unique session/member identities.

- Both tenants publish a `price_book` with the **same publication ID and
  version ID**, but different synthetic COP amounts and SHA-256 digests.
- Direct fetch, filtered list, current-price selection, and manifest return
  only the actor's company publication. Forged `organizationID` query params
  cannot switch tenants. A company-unique catalog publication is 404 to the
  other tenant and absent from its list/current/manifest.
- Successful GET and POST responses have `private, no-store` and `nosniff`.
- Member without `pricing.manage` is denied a publication POST (403);
  anonymous manifest is 401; revoked bearer cannot fetch current publications.
- The baseline price book is **unconfigured and non-authoritative**. All
  prices used here are intentionally synthetic, not Villa Camila/supplier data.
- Exact test fixtures remove only their synthetic orgs, memberships, sessions,
  users and publications from the disposable DB.

## Execution and release gates

Initial full environment-enabled test checkpoint: **481 passed, 1 skipped**.
All externally dependent failures and the final pin/loopback smoke must be
reported separately if they change. The sole skip is not counted as passing.
The canonical `astrix-group-accounts/server-api` remote has been inaccessible
for authenticated writes from the fleet, so local acceptance does not equal
canonical merge/hosted CI.

Release order remains:
1. SCRUM-48/59/64/47/62/63: complete negative authorization and revocation
   coverage across customers, projects, items, quotations, evidence and
   reference authority; review price-book READ permission for scoped seats.
2. SCRUM-44/50/51/52/60/61/66/68: pin the **16 approved finished-product
   templates** separately from the **20 Villa Camila quotation rows**;
   reconcile exact source-backed COP values with authoritative catalog, BOM
   and price-book publication versions before quote signoff.
3. SCRUM-29/39/40/41/42/27/30/31: obtain real independent backup custody/
   DBA/infra/release approvals; isolate and validate exact VM101 PostgreSQL
   restore and WAL/customer data, then separately authorize reversible
   private endpoints, object storage and ingress release.
4. SCRUM-49/57/65/69/43/33/37/38: physical two/three-member iOS acceptance,
   exact signed TestFlight build, offline/relaunch and connected sync evidence.

**Non-goals:** public ingress, production API/DB/container rollout, PostgreSQL
restart, Proxmox VM9101 creation, customer-data archive copy or TestFlight
promotion. These remain **HOLD**.
