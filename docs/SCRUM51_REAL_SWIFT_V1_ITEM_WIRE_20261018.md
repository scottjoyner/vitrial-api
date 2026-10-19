# SCRUM-51 — actual Swift V1 Item draft wire, live API/PG contract

**October 18, 2026 EDT. Synthetic, loopback, non-production acceptance.**

## Independent payload origin and actual execution

A portable Swift 6.3.3 XCTest from the separately committed iOS research
branch emits an **outgoing** `SyncBatch` using the app's actual
`SyncRecord` and `JSONEncoder`, with `Data` encoded as base64 JSON.
The fixture is intentionally synthetic: one Item, a pending-review
`jobItemDraft`, distinct manually entered L legs (124.25/163.75 cm),
height 200.5 cm, no rectangular width and no supplier prices.

The fixture is kept **outside Git** at
`/home/scott/agent-sprints/20261018/vitrial-scrum51-v1-crosswire/synthetic-swift-item-sync-batch.json`.
Do not embed private Villa Camila quotations, credentials or source documents
in this fixture.

`tests/test_scrum51_swift_v1_wire_postgres.py` runs only when both
`POSTGRES_INTEGRATION=1` and
`VITRIAL_SYNTHETIC_SWIFT_ITEM_V1_BATCH_FILE` are supplied. The test:
- Migrates a freshly disposable loopback-only PostgreSQL 17 to Alembic 0008.
- Creates one disposable tenant with one authenticated, company-scoped session,
  Customer/Project/ProjectSector and real canonical ownership.
- Passes the **untouched Swift-generated outgoing batch** through the real
  FastAPI `POST /api/v1/sync/push` handler and PostgreSQL transaction.
- Confirms server acceptance, idempotent replay of the same mutation ID,
  durable database Item with distinct L legs, revisioned
  `GET /api/v1/sync/pull` and anonymous access denial.
- Sends four *new mutation IDs*, each with a tampered base64 Item payload,
  and confirms rejection of falsely approved templates, missing second leg,
  injected rectangular `width`, and forged nested Project identity.
  No declined mutation is allowed to update persisted Item or revision.

## Actual security finding and remediation

The initial Swift payload worked, but the adversarial test discovered that
the prior Python `validate_job_item_draft()` accepted a **flattened
rectangular `width` on an `l_shape` Item**, even though the iOS
`CreateItemRequest` correctly refused it. The root cause was independent
server-side draft validation of the nested `opening` only.

The shared backend validator now checks that, when present, the older flat
Item `name` and `quantity` agree with the nested draft, and that flat
`height` and rectangular `width` are finite, unambiguous centimeter
dimensions exactly matching the manual draft. Corner/L drafts must have
**no** flat rectangular width. The same authority is used for both V1 and
V2 Item create/edit authorization. Older Items **without** `jobItemDraft`
are unchanged, as are unmeasured drafts and otherwise valid tenant
permissions.

`tests/test_scrum51_finished_item_draft.py` also includes unconditional
synthetic regression controls for inconsistent names, quantities, units,
invalid values, malformed dimension dictionaries, and corner/L flat-width
injection. The new cross-language HTTP test is opt-in and intentionally
skips without the generated fixture; it must not be counted as passing in
CI environments where the fixture is unavailable.

## Deployment boundary and release plan

Full local Python tests with disposable PG, private approved-as-unapproved
historical fixture, and generated Swift wire: **548 passed, 1 skipped**.
Known schema/deprecation warnings are not proof of hosted CI readiness.
No customer records, signed quotations, real company pricing, production
ingress, database backups or VM101 services were touched.

Next: authorized maintainer imports/rebases the separately verified
backend and iOS branches into `astrix-group-accounts` canonical repos;
runs exact-head hosted CI; builds the app with the Apple SDK; checks
authenticated iPhone V1 push, device/offline persistence, replay/conflict,
two scoped companies and TestFlight build identity. This does **not**
authorize canonical merge, VM101 recovery, TestFlight, source approval
or production deployment. **Release remains HOLD.**

## Pinned-implementation loopback TCP stage

Fleet Commander started the **clean code commit**
`410fb353a73f72f472f27fe513f2594af3ca3421` with Uvicorn bound only
to `127.0.0.1`. It used PostgreSQL 17 at a **temporary loopback Docker
binding**, Alembic 0008, and a new synthetic member session and company.
An actual TCP request to the temporary API returned:
`/health=200`, `/ready=200`, Swift V1 push `200`, revisioned V1
pull `200`, exact two-leg preservation, and refusal of a synthetic
L-shaped mutation with an injected old-style `width`. The Uvicorn
process stopped and the synthetic tenant and all its created rows were
deleted afterward. Disposable PostgreSQL itself was retained only for
the final exact-head test and must then be stopped/removed.

This confirms HTTP-level staging, **not** Apple runtime's authenticated
network route from an actual iPhone or a canonical upstream production
deployment. VM101, customer records, supplier data and TestFlight remain HOLD.
