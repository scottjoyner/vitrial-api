# SCRUM-64 — actual FastAPI scan pagination separates Item and audit

**2026-10-19 EDT. Fleet Commander x1-370, disposable PostgreSQL 17. RELEASE HOLD.**

## Exact execution

`tests/test_scrum64_naturally_split_item_audit_postgres.py` generates
two separate **authenticated, real V1 API mutations** from the previously
verified synthetic Swift `JSONEncoder` batch: one Item and one matching,
provenance-consistent immutable `item_audit_event`. It uses the exact Swift
base64 Data payloads, actor, session ID and company. It does not forge,
repair or bypass customer audit history.

In the interval, the test inserts **500 synthetic non-deliverable
`sync_change_log` entries** in its own freshly migrated, disposable
PostgreSQL tenant. These entries refer to nonexistent synthetic customers
and are **test-only**: they simulate intervening unrelated changes and
force the real API's unchanged `MAX_SYNC_PULL_SCAN_CHANGES=500` boundary.

Then the genuine FastAPI `GET /api/v1/sync/pull?cursor=seq:0` returns
only the Item at its server sequence and an advancing cursor. A second
genuine API pull using exactly that cursor returns only the Item's
immutable audit. A third pull returns no records with a stable cursor.
Both authenticated writes were accepted; anonymous pull returned 401.

In this isolated first database run, the actual returned cursors were
`seq:500` and `seq:502`. They depend on the database-wide sequence
and must not be assumed fixed in a later run. The test asserts the
actual strict progression rather than hard-coding them.

## Cross-language custody chain

The two **untouched raw FastAPI response bodies** were saved outside Git,
with 0600 mode, under:

`/home/scott/agent-sprints/20261019/vitrial-scrum64-real-server-split/`

The companion iOS portable XCTest in the next iOS research branch reads
those files into the real `SyncBatch` decoder, uses the actual previously
committed `SyncAuditedPullWindow.collect` (no test-crafted splitting),
applies the combined records to `LocalProjectRepository`, and confirms
both the Item and matching audit survive cold repository reopening.

This is genuine server pagination induced by **synthetic database filler**
and real Swift/Python protocol bytes, not evidence of normal production
traffic, an actual iPhone HTTP request, an Apple Xcode UI build, independent
audit signing, or a production release.

## Complete regression and safety

The complete backend pytest suite passed **553 tests, 1 skipped, 5 known
warnings** with disposable PostgreSQL 17, Swift synthetic fixtures and
private historical source fixture enabled before commit. The additive
test intentionally changes no production API code, schema or ownership
behavior. Every synthetic created tenant, member, project, Item, audit,
change-log entry and session is deleted in a `finally` cleanup.

The server scans only the existing bounded 500 change-log rows and uses
its normal org/visibility and cursor checks. No live database or VM101
backup was accessed, and no real customer catalog or price was exported.

Remaining HOLD: canonical repository access/review/hosted exact-head CI,
authorized Xcode/iOS17+ build and physical owner/Alex XR iOS18.7.9
offline-sync acceptance, independently approved 16 Villa Camila templates
and 20 quotation lines, and independently approved VM101 PostgreSQL
restore/WAL plus production release authorization.
