# SCRUM-64: Multi-company Item mutation, stale revision and revocation checks

**October 19, 2026 EDT. Isolated PostgreSQL17 acceptance via Fleet Commander
on x1-370; not a production deployment, canonical merge or TestFlight release.**

## What was exercised

`tests/test_scrum64_swift_item_multitenant_conflict_postgres.py` uses the
exact synthetic Swift-generated V1 `SyncBatch` Item payload already
validated against the Python API. The test changes only explicitly
identified synthetic fields (clientMutationID, revision and Item location)
and keeps the pending-review, two-leg L-shaped Item draft intact.

It bootstraps **three separately authenticated users**: one owner and one
editor in Company Alpha, and an owner in independent Company Beta. Both
companies have separately seeded canonical Customer/Project/Sector rows,
deliberately with the **same IDs**. The first mutations also deliberately
have the **same client mutation ID** and identical Item IDs across those
companies. PostgreSQL's authoritative organization scoping must keep
their revisions, document content, and mutation receipts independent.

The test then exercises:
1. Independent company acceptance/pull of the same ID, with different
   synthetic Item location content; an outsider cannot request another
   tenant's view using an extra organization query parameter.
2. Attempted cross-tenant write with an explicitly forged
   `organizationID` in the Item payload: denied.
3. A concurrent user's **stale** offline Item edit using a superseded
   baseServerRevision: rejected. Its mutation ID remains spent after
   retry, and a different request reusing that same ID is rejected.
   The current owner's newer Item stays untouched.
4. Explicit rebase using a new mutation ID and the latest confirmed
   revision: accepted. This does not assert that conflicts may be
   silently overwritten; a real UI must still show the user the diff.
5. Revocation of the editor session: subsequent pull and push both
   return 401 while the owner session remains valid.
6. Two actual simultaneous HTTP push requests with the **same** current
   base revision: exactly **one accepted and one rejected**; the stored
   Item revision advances by precisely one, and the latest Item content
   is one of the two submitted values. Beta's Item is unchanged.

This is actual HTTP ASGI routing, DB ownership, PostgreSQL transactions,
organization row locks and mutation receipts—not a mock algorithm.
Synthetic company/user/session data are deleted in `finally`. Tests
do not read, move or overwrite real customer data, supplier prices,
VM101 backups or unrelated fleet containers.

## Additional sync observation

A V1 pull with `cursor=seq:0` can deliver multiple change-log
records for a previously updated Item, with distinct server-sequence
record IDs. The current implementation resolves against the latest
entity payload for a historical change, and therefore repeated
current-state payloads may be observed on a full-history pull.
This test deliberately selects the highest actual sequence for
**current state**, validates sequence ordering, and does not freeze
a duplicated-current-content behavior as a desired protocol contract.

Before treating the change log as an immutable event history or
deriving per-event audit history from Item records, complete a
separate design review of historical payload/version semantics,
pagination and snapshot behavior. Item audit events are a distinct
append-only entity and remain mandatory for durable iOS sync import.

## Verification, limits, and integration

Three independent focused runs passed on freshly generated synthetic
company identities. The entire backend Python test suite on an
isolated PostgreSQL 17 instance passed **550 tests**, with one skipped
and five known warnings before commit. No application code was
changed in this slice; it is a real regression acceptance harness.
The exact commit and private Git bundle will be recorded in Jira.

Independent pending release gates remain:
- Canonical `astrix-group-accounts/server-api` permission,
  reviewed integration of existing SCRUM-47/48/64/52/51 changes and
  hosted exact-head CI.
- Canonical iOS branch integration, authorized Mac/Xcode build and
  real owner + Alex XR iOS18.7.9 authenticated offline/online sync.
- Separate owner-approved original source and customer acceptance of
  all 16 Villa Camila templates and the 20-line COP quotation.
- VM101 database backup/WAL/recovery and production release authority,
  with no restore, network cutover, or customer data migration here.

**Production API / TestFlight / catalog promotion: HOLD.**
