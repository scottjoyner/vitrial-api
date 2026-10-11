# SCRUM-44 v0.2.2 — Backend Source-Authority Acceptance Audit
**Audited:** this worktree at `6949b1b`, read-only, 2026-10-11. **Scope:** the seven named files plus the schema types they depend on. No source, config, contract, migration, or database change was made; the calculator was read, not rebuilt.
## 1. Implemented and tested at this HEAD
**Calculator — pure, fail-closed, no I/O** (`app/configurator_component_calculator.py`)
- Immutable publication pins are mandatory; empty catalog/rule version IDs raise (`:168-169`).
- `inputSHA256` is a canonical sorted non-ASCII-escaping SHA-256 over the whole normalized input (`:170-173`); it moves on same-version edits (test `:82-85`) and is per-item (test `:73-79`).
- A client rulebook is impossible by construction: `trusted_catalog`/`trusted_rules`/`override_authorized` are keyword-only and the HTTP layer never fills them from the body (`:152-167`, routes `:175-183`).
- Fail-closed arithmetic: missing/ambiguous rule (`:209-211`), absent/inactive reference (`:195-197`), item-type and system incompatibility (`:198-203`), zero applicable sections (`:217-219`), non-integral `each`/`panel` (`:233-235`), `>1_000_000` bound (`:236-238`).
- **Zero fabrication is structural:** any issue empties the component list outright (`:265`); `ready` is exactly `not issues` (`:147-149`).
- Override needs authorization *and* an 8-char reason (`:119-122`, `:227-230`, `:252-256`).
**Route — authenticated, org-scoped, read-only** (`app/configurator_component_routes.py`; registered `app/main.py:21,51`)
- `POST /api/v1/items/{item_id}/component-calculation/preview`, `operationId=previewItemComponents` (`:143-147`); bearer security proven against generated OpenAPI (test `:207-214`).
- Ordering is the security property: path/payload Item ID mismatch → 400 **before** any DB read (`:154-155`, test `:125-133`); `require_item_access` → 403 **before** the catalog is read (`:157-160`, test `:137-149`).
- Publications resolve only as `(principal.organization_id, publication_id)` and must match declared kind, stored SHA-256 and `authoritative: true` (`:60-75`); any failure is 409 — never a 500, never a substitution (tests `:153-175`).
- Response carries both source digests, snapshots of *selected* entries only, and the nested receipt (`:186-198`). No `db.add`/`commit` or quotation/production entity is reachable from this module.
**Migrated-Postgres HTTP proof** (`tests/test_scrum44_postgres_http_integration.py:35-175`, gated on `POSTGRES_INTEGRATION=1`)
- Real bearer token and real session: 401 unauthenticated (`:124`), 200 authenticated (`:126-136`), 409 digest mismatch (`:137-138`), 403 foreign Item (`:139-142`), 422 forged `trustedRules` (`:143-144`), 401 after revocation (`:155-159`).
- Read-only is measured, not asserted: zero `SyncEntity`/`SyncMutation`/`SyncChangeLog` rows for the org (`:146-151`).
## 2. Four separations that must not be collapsed
| Axis | What exists here | What is **not** here |
|---|---|---|
| Mirror vs Astrix canonical | This Python service in an isolated worktree | Astrix canonical API/authorization; iOS is pinned to V1 SHA `74822ac9` |
| Draft vs released | Route registered, **not** enabled in production/TestFlight (`docs/SCRUM44_COMPONENT_CALCULATION_AUTHORITY.md:3`) | Any release, Gate A–F approval, or live traffic |
| Synthetic Postgres vs real | A migrated database with fixtures authored in-test | Vendor evidence, production migration, real hardware |
| Test quantity vs approved | `fixture-only-two-per-slider` (`tests/test_configurator_component_calculator.py:29-35`) | A signed System 7-44 rule; the doc calls it test data (`AUTHORITY.md:43-45`) |
## 3. Real remaining dependencies (blocking; none is a code defect at this HEAD)
1. **Approved vendor catalog + rule publication.** None exists; bundled baselines are `authoritative: false` (`app/reference_data.py:141,220,235,296`) and are refused at `routes:73-74`. Owner action, not code.
2. **`item.components.override` is absent from the served vocabulary.** The route requires it (`routes:182`), but `Capability` (`app/schemas.py:18-27`) omits it and both membership writers validate `list[Capability]` (`app/admin.py:34,166`). In production **every** override therefore fails closed and the authorized path is test-only — the test hand-builds the `frozenset` (`tests/test_configurator_component_routes.py:17-23,190-197`). Granting it also requires editing `scripts/validate_backend_contract.py:12-16`, or `tests/test_contract_vocabulary_drift.py:34` fails: a V1 contract decision, not a patch.
3. **Scope to confirm:** glass/linear quantities are **not** derived from geometry. `widthMM`/`heightMM`/`glassPanels`/`additionalVanoSegments`/`openingDirection`/`sectionWidthProvenance`/`measurementStage` feed lineage only; `hardwareIDs`/`accessoryIDs` are uniqueness-checked and otherwise unused (`:72-73,89-91`). `quantityPerOccurrence` is the sole quantity source, so m/m² derivation and cut sizes are unimplemented rather than deferred.
4. **Guard coverage for the new path.** `scripts/check_guard_prefixes.py:26-36` enumerates only the auth/reference/sync routers, and no declared prefix in `app/request_size.py:77-82` or `app/rate_limit.py:64-66` covers this path. Bounds are pydantic-only. Register it before any release decision.
5. **Rejection codes.** This route raises `HTTPException` with prose; `app/rejection_codes.py` does not cover it (VITR-V006 rule). Needed before external monitoring.
6. **Stale doc.** `docs/SCRUM44_COMPONENT_CALCULATION_AUTHORITY.md:84-87` still points at `/home/scott/git/wt-vitrial-api-scrum44-20261010` and predates the migrated-Postgres test added in `6949b1b`.
## 4. Minimum cross-repo request/response fixture test (proposal — not implemented here)
Add `contracts/scrum44/component-calculation-preview.request.json` + `.response.json` in the established `request`/`canonicalMaterial`/`sha256` shape of `contracts/reference/reference-publication-canonical-v1.json` — **not** under `contracts/backend/v1/`, which is pinned to iOS SHA `74822ac9`. One pure test, `tests/test_scrum44_preview_fixtures.py`: (1) `ComponentCalculationPreviewRequest.model_validate` accepts the request fixture; (2) run `routes._catalog`/`_rules` over the fixture publication and `calculate_components`, then assert `ComponentCalculationPreviewResponse.model_validate` reproduces the response fixture exactly on `inputSHA256`, both digests, `quantity` and `sourceRuleID`; (3) assert the fixture publication digest equals its stored `sha256`, pinning API and iOS to one canonical material; (4) assert a negative twin (missing rule) yields `components == []` with non-empty `issues`. No DB, no network — same cost as the calculator tests; iOS decodes the same two files in its Swift tests.
## 5. Owner / gate matrix
| Gate | Owner | Evidence required |
|---|---|---|
| Vendor catalog + rule publication signed | Business technical owner | Published `catalog` + `compatibility_rules`, `authoritative: true`, independent SHA-256s |
| Capability vocabulary (`item.components.override`) | API owner + iOS contract owner | V1 contract change, or explicit "overrides remain disabled" |
| Canonical authorization model | Astrix API owner | Canonical spec; this mirror is not evidence of it |
| iOS digest/binding + device round trip | iOS owner + QA | Swift fixture parity (§4), simulator and physical save/reopen/preview/recalculate |
| Quote/manufacturing authority | API + commercial owner | Confirmation that no quotation or production write derives from preview output |
| Release | Release owner | Gates A–F; green draft CI is not a release |
## 6. Verification performed
`python3 -m pytest tests/test_configurator_component_calculator.py -q` → **12 passed** (pure in-memory; no DB, no network). `tests/test_configurator_component_routes.py` was **not** executed: this worktree has no virtualenv and `sqlalchemy`/`alembic`/`pytest-asyncio` are absent, and installing them needs network. The Postgres test was not run by design (no database, no migrations, no config change).