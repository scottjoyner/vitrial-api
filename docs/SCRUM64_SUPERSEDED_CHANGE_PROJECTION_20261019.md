# SCRUM-64 — current-revision sync pull projection

**2026-10-19; research/isolated PostgreSQL only. Production and TestFlight HOLD.**

## Root cause and narrow fix

The V1/V2 sync pull endpoint uses a global PostgreSQL
`sync_change_log.sequence` cursor scoped to the authenticated company.
Until this change, it loaded the **current** `sync_entities` payload for
*each* historical change row. An Item created at sequence 1 and edited at
sequence 3 could thus appear twice in a pull from `seq:0`, **both**
carrying today's state and revision but one mislabeled with the **old
clientMutationID** and sequence 1. That is a potentially false offline
acknowledgment, especially while different editors have pending mutations.

A minimal read-only projection correction in `pull_since()` consumes
older change-log positions whenever
`change.server_revision != current_entity.server_revision`, without
delivering a mislabeled new state under old mutation identity. The
current revision is delivered **only** with its own sequence/mutation
metadata, as long as the existing tenant visibility and claim checks
admit it. A row skipped as superseded advances the cursor like any
other undeliverable old change. The code does **not** rewrite, truncate,
backfill, drop or re-sign existing journal records. No migration,
business lifecycle policy, authentication, or production service change.

**Contract limit:** this remains a *current-state projection*. It is
not an immutable event stream and does not supply historical Item
snapshots. In particular, do not use generic Item sync records as
historical manufacturing approvals, tamper-proof audit history or
customer quotation snapshots. `item_audit_event` is a separately
required append-only entity: its current revision is unchanged and it
remains delivered through the same authorization gate. Snapshot/pull
atomicity and append-only audit source custody require independent
release validation.

## Tests and cursor safety

- The existing synthetic Swift-generated Item V1 fixture is used by
  `test_scrum64_swift_item_multitenant_conflict_postgres.py` with two
  companies and concurrent owner/editor edits. It now explicitly checks
  that a full-history V1 pull emits **one current Item record** whose
  client mutation ID belongs to the new edit, not the superseded edit.
- `test_scrum51_item_draft_postgres.py` checks the same current-state
  behavior on **V2** after an edit and confirms company B receives only
  its own Item without Company A's additional hardware component.
- `test_sync_pull_cursor_postgres.py` inserts a real change-log with
  four revisions for one customer; only the latest state appears with
  its matching mutation ID and sequence. A second scenario inserts
  **501 revisions** and confirms an initial fully superseded
  `MAX_SYNC_PULL_SCAN_CHANGES=500` page returns zero records with an
  advanced cursor, and the following page yields the current revision.
- Portable iOS `SyncPullPagingTests` independently checks an empty
  `seq:500` page advances, `seq:501` delivers the latest Item, and
  a subsequent repeated cursor safely terminates. No iOS app runtime
  feature change is needed.
- Entire isolated PG17/Alembic 0008 backend suite before commit:
  **552 passed / 1 skipped / 5 known warnings** with synthetic Swift
  V1 and Item+audit fixture and private source fixture enabled.
  The count is not a canonical hosted CI or physical device witness.

## Release boundary

On `x1-370` the code was tested only against a disposable PostgreSQL
container bound to loopback. No live customer API, VM101 recovery,
Proxmox object storage, canonical tenant, public domain, real supplier
price or TestFlight build was changed. The canonical repository
`astrix-group-accounts/server-api` was checked and remains inaccessible
through the connected GitHub tool (404); do not substitute the readable
mirror as the canonical repository.

Required next gates: canonical repo maintainer import, code review and
hosted exact-head CI; authorized Mac Xcode simulator/device and
owner/Alex XR iOS18.7.9 scoped two-editor acceptance; independently
reviewed original Villa Camila 16 template definitions and 20 quote
rows; approved networkless VM101 backup/WAL PostgreSQL recovery.
**Production and TestFlight remain HOLD.**
