# Vitrial SCRUM-52/68 — deterministic COP estimation acceptance

**October 18, 2026. Code only / disposable PostgreSQL 17. Production HOLD.**

## Source custody and status

The existing private fixture `villa_camila_quote_001.json` is retained
**outside this repository** at
`/home/scott/git/vitrial-scrum50-staging/contracts/fixtures/private/`.
Its full SHA-256 is pinned in the optional private pytest test; the committed
source code, tests and Git bundle contain **no private quotation line amounts,
customer contacts, or reusable vendor prices**. The fixture is marked
`private-company-acceptance-only`, `publicationStatus=not-approved`,
document Cotización No. 001, dated September 16, 2026.

The existing local historical verifier validates **16 templates distinct
from 20 quotation rows**, 16 group subtotals, exact source-row commercial
amounts, quantity >1, fractional centimeters and true two-leg corner/L
measurements. Its recorded grand total is **COP 64,245,000**. This is
internal acceptance against a prepared fixture, **not independently signed
source-PDF custody, not customer final approval, and not an authorization to
publish a global catalog or pricebook**. Confirm source byte chain and obtain
customer/estimator review before declaring SCRUM-52 done.

## Incremental API contract (backward compatible)

`app/quotation_estimate.py` exposes `reconcile_quotation_cop` and
`format_cop_es_co`. The canonical `authorize_record` quotation branch in
`app/ownership.py` calls reconciliation **before any canonical Item lookup or
mutation** for both V1 and V2 sync. No new database tables or migrations.

Previously accepted quotations with only Item references and no detailed
money fields remain valid. For quotations opting into COP arithmetic:

- The parent quotation carries `currency="COP"`, `locale="es-CO"`,
  `commercialTotalCOP` and `groupSubtotalsCOP` (JSON object keyed by
  decimal source group number).
- **Every** line carries a unique `id`, canonical `itemID`,
  positive integer `quantity`, non-negative integer `unitPriceCOP`,
  exact integer `lineTotalCOP` and a positive bounded integer
  `sourceGroup`. The server rejects nonintegers, booleans, negative
  amounts, partial pricing, duplicate line IDs and incorrect arithmetic.
- Group subtotals must exactly match the sum of their constituent lines.
  The grand total must match all item line totals. Values and line counts
  are explicitly bounded to avoid arbitrarily large input processing.
- The service does **not** infer costs from geometry, supplier materials,
  price books, BOMs, glass cuts, inventory or warehouse calculations.
  All prices/quantities are entered by users in their own quotation.
- Existing lifecycle/provenance enforcement still locks commercial content
  after sent/approved and requires separate revision for changes. It
  does not mean a draft has commercial signoff.

The COP formatter returns es-CO examples like `$ 64.245.000`, without
floating-point arithmetic or locale-dependent OS libraries.

## Tested evidence and open gates

- Public synthetic unit test exercises 20 lines grouped into 16 headings,
  quantity 3 for one line, source/group/total reconciliation, and 21
  adversarial failure modes. It does **not** encode any actual customer
  quotation line prices.
- Optional private test, enabled by
  `VITRIAL_PRIVATE_VILLA_CAMILA_FIXTURE`, verifies SHA-pinned fixture,
  16/20 cardinality, **64,245,000 COP** and a falsified multi-quantity
  case; no private input is committed.
- Disposable PostgreSQL 17 / Alembic `0008` integration creates one
  synthetic company, Customer, Job/Project, Sector, **20 canonical Items**,
  pushes the corresponding COP quotation via V2, verifies durable
  quote/pull, and refuses wrong subtotal or changed quantity.
- First complete cross-feature suite with disposable PostgreSQL:
  **507 passed, 1 skipped**. Any later exact-commit rerun is reported in
  Jira; one skip is not counted as successful acceptance.
- The canonical `astrix-group-accounts/server-api` GitHub connection has
  previously returned 404 / authentication failure. Commit locally, verify
  private portable bundle and request **canonical review + hosted CI** rather
  than claiming a public upstream merge or live deployment.
- Stage only a temporary loopback TCP API with synthetic PostgreSQL;
  validate health, readiness and authentication, stop it, remove the
  disposable database. **VM101 PostgreSQL crash recovery, backup custody,
  independent DBA/data owner approval and actual production API release
  are still HOLD**.

## Remaining critical-path work

1. **SCRUM-50 → SCRUM-51 → SCRUM-52/68:** obtain signed template review,
   preserve all 16 Spanish template identities/version/source metadata,
   finish text-first Item CRUD/material descriptors and user-entered
   measurements; prove a *physical iOS* 20-line quote and relaunch.
2. **SCRUM-47/48/59/62/63/64:** integrate company membership and all
   cross-tenant negative tests (Job/Item/Quote/catalog/price/evidence);
   verify revoked/scoped users and immutable sent/approved revisions.
3. **SCRUM-44/60/61/66:** company approved BOM/material catalog provenance
   pinned to exact publication digests; never convert historic job unit
   quotes into manufacturer or supplier price books.
4. **SCRUM-29/39/40/41/42:** separately signed authorization for networkless
   VM9101 restore of the exact selected backup and verifiable PG/WAL/customer
   record consistency before private DB/API, object store, TLS, ingress and
   rollback readiness.
5. **SCRUM-49/57/65/69:** signed iOS TestFlight exact-build physical
   two/three-user acceptance, offline drafts, sync conflicts and customer
   walkthrough. No production or TestFlight rollout before these gates.
