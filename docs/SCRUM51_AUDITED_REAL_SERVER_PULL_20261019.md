# SCRUM-51 — genuine server-pull response and Item-audit custody

**2026-10-19 EDT. Synthetic, disposable PostgreSQL 17 only; production HOLD.**

This slice takes the existing Swift-generated **outgoing** V1 Item draft
mutation, pushes it into canonical-shaped FastAPI code using a disposable
PostgreSQL database at Alembic 0008, and captures the **actual** authenticated
V1 pull JSON response for a separate Linux Swift decoder test. All fixtures
are under owner-only `/home/scott/agent-sprints/20261019/vitrial-scrum51-server-pull/`
outside source control, never containing real customer quotation lines.

## Discovered required provenance

The initial backend pull returned one legitimate L-shaped Item with two
separate legs; Swift's `SyncBatch` and `RemoteSyncApplication` decoded
and applied it to an in-memory graph. Durable `LocalProjectRepository`
rightly rejected that single-record response with `missingItemAudit`.
This is an **audit-integrity gate**, not a reason to synthesize a false audit
or loosen the repository's protection.

The actual app's `LocalSyncBatchBuilder` separately exports
`snapshot.itemAuditEvents`, including provenance. A new opt-in Swift test
generates an **additional** synthetic Item-created audit event with the
matching actor, company, membership, session and authorization revision.
The Python server test bootstraps those exact synthetic identities in
PostgreSQL, submits the **Swift-encoded two-record V1 batch**, and checks
that both Item and audit are accepted and returned in the real V1 pull.
The expected audit's `after` image exactly matches the Item. The same
JSON bytes are then replayed into the portable Swift tests to confirm that
both survive durable local repository writes and cold reopening.

The Python test previously assumed the server cursor for a tenant began at
`seq:1`; that is incorrect. The PostgreSQL change-log sequence is global
across synthetic test tenants. The test now verifies monotone numeric cursor
and server-assigned positive revisions without inventing per-company
sequence numbers. No cursor contract has been altered in application code.

## Executed evidence

- `tests/test_scrum51_swift_v1_wire_postgres.py` now contains two real
  PostgreSQL/FastAPI acceptance cases: an Item-only V1 round trip (valid for
  API but intentionally insufficient for offline audited import), and
  an authenticated **Item plus session-consistent synthetic audit**
  creation/return case. No customer data, signed customer documents or
  supplier prices are present.
- The new test refuses incorrect session provenance, mismatched audit
  snapshots and missing item history through the existing production
  validators; no audit bypass was added.
- Optional test-only artifact paths:
  `VITRIAL_SYNTHETIC_API_V1_PULL_WIRE_FILE` and
  `VITRIAL_SYNTHETIC_AUDITED_API_V1_PULL_WIRE_FILE`.
  Files are created without overwriting, in owner-only directories, at
  restrictive 0600 mode; they are **not** committed to Git.
- Full Python suite with disposable PG17, complete Swift fixtures and the
  private Villa Camila historical acceptance fixture: **549 passed,
  1 skipped, 5 warnings** before commit. Test-only/fixture-skipped cases
  must not be counted as successful production acceptance.

The test-generated audit event has synthetic, self-consistent provenance.
It does not supply independently controlled human approval, original
Villa Camila document custody, canonical hosted CI or signed TestFlight
authority. Import requires authorized canonical `astrix-group-accounts`
maintainer review, Mac/Xcode exact-head build, and owner/Alex physical
offline sync acceptance before release. VM101 PostgreSQL recovery, public
ingress, catalog publication and customer pricing remain on **HOLD**.
