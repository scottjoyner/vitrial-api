---
kind: candidate
recordKind: continuity
category: goal-set
seed_type: goal-set
name: vitrial-api-state-2026-10-03
description: "server-api at 4865052 (+ uncommitted Python fix set): VITR-V008/-009/-010/-011/-013 all fixed and covered, VITR-V004 corrected to Not fixed. BEAM VitrialSync gains admission steps 5-8 and pull visibility resolution (+302 tests, 529 `test \"` blocks + 3 doctests = 532). Nothing in the Python half was executed — no sqlalchemy, no reachable PostgreSQL."
agent: nez
date: 2026-10-03
tasks: []
projects: [vitrial-api]
repos: [vitrial-api]
---

## State

`/home/nez/Projects/server-api` (GitHub `astrix-group-accounts/server-api`) @ `4865052`,
with an **uncommitted** fix set on top: `app/reference_data.py`, `app/ownership.py`,
`app/evidence_gc.py`, `app/storage.py`, the tracker, and a concurrent BEAM tree under
`beam/apps/vitrial_sync/`. Two agents are working this tree at once; see *Concurrent
ownership* below before staging anything.

## Five defects closed, one corrected

| ID | What it was | Now |
|---|---|---|
| `VITR-V008` | four reference-data GETs call `ensure_baseline_publications`, which does check-then-insert with no lock / `ON CONFLICT` / retry; under READ COMMITTED the loser's INSERT raises an unhandled 23505 → 500 | `_seed_baseline_publications` split out; `ensure_baseline_publications` catches `IntegrityError`, rolls back, re-checks (`app/reference_data.py:417-477`). Race half only |
| `VITR-V009` | `authorize_record`'s `delivery_execution` branch is unreachable in production (sole caller sits in the `else`-arm of the `DELIVERY_ENTITY_TYPE` dispatch at `app/sync_service.py:347-364`) and had **drifted** from the live gate — it skipped the `delivery.manage` capability check on tombstones and permitted tombstoning a known execution, which the live copy refuses | branch and its orphan helper `_current_quotation_id` deleted; delivery now falls through to the closing `raise` and the generic gate **fails closed** (`app/ownership.py:750-762`) |
| `VITR-V010` | `not_before` written once at queue time and read only in the selection predicate, so both retain paths (still-referenced skip, `StorageError`) reselected the row on the very next run forever; `attempts`/`last_error` write-only | `_reschedule` at both sites (`app/evidence_gc.py:40`, called `:192`, `:211`), 300 s doubling capped at 3600 s. `attempts` now has a reader |
| `VITR-V011` | collector selects `FOR UPDATE SKIP LOCKED` once then `commit()`s four times *inside* the row loop, releasing locks on rows not yet reached; because `expire_on_commit=False` the stale objects survive, and the UPDATE path's rowcount check raises `StaleDataError` uncaught, aborting the run | one transaction for the whole run, `SAVEPOINT` per row via `db.begin_nested()`, single `commit()` at `app/evidence_gc.py:217` (`app/evidence_gc.py:118-217`) |
| `VITR-V013` | `S3ObjectStore.put_verified` publishes with `copy_object`, then a failing staging delete fell into the broad handler, was swallowed by a bare `except: pass` retry and re-raised as `StorageError` → `put_blob` 503s at `app/evidence.py:160`, *before* the `EvidenceBlob` row and *before* the GC row: a durable, digest-verified object at its final key with nothing referencing it and nothing to reclaim it | post-publish staging delete is best-effort and logs `storage.staging_delete_failed` at WARNING (`app/storage.py:234-245`) |

`VITR-V004` is **still open** and the tracker was wrong about it. `33ccee4` self-describes
as "the first step of the VITR-V004 fix" and its diff only splits out `_list_publications`
— all four GET helpers still seed and `commit()`. A GET that writes is still a GET that
writes; moving the seed to org creation is a design decision (what happens to orgs that
predate it), not a drive-by.

## What was NOT run (this is the load-bearing caveat)

**No Python test was executed in this session, and no defect fix above was verified by
execution.** Every Python finding and every fix is static reading plus a formula replay.

- `python -m pytest tests/test_delivery_execution_ownership.py -q` → collection ERROR,
  `ModuleNotFoundError: No module named 'sqlalchemy'`. No venv in this workspace.
- `psql -U vitrial -d vitrial` → `FATAL: Peer authentication failed for user "vitrial"`;
  `psql postgresql://vitrial:vitrial@localhost:5432/vitrial` → `FATAL: Ident authentication
  failed`. `docker ps` → `No such file or directory`.
- The durable PostgreSQL cluster and venv named in the 2026-10-02 seed
  (`~/.pgdata/vitrial` on port 5435, `~/.venvs/vitrial-api`) were **not** reachable from
  this session's environment. Do not assume they still exist; re-establish before trusting
  any green suite.
- Every affected file is additionally gated on `POSTGRES_INTEGRATION=1` or `S3_INTEGRATION=1`.

The BEAM guards **were** run here and all passed: `scripts/check_beam_scaffold.py` exit 0,
`scripts/check_beam_orphans.py` exit 0 ("no dead modules: every lib module is either
reachable or has a test"), `scripts/validate_backend_contract.py` exit 0 ("PASS"),
`python -m compileall app scripts tests` exit 0, and `elixirc -o /tmp/vsbuild` over
`beam/apps/vitrial_sync/lib` exit 0 — with no warning mentioning `prefetch`. The two
`elixirc` warnings that do fire are in `lib/vitrial_sync/page.ex:171,191` (a
`%PageBuilder{builder | ...}` struct-update on a `dynamic()`), pre-existing and untouched
by this work.

## BEAM waves — `beam/apps/vitrial_sync`

**Admission steps 5-8** (`sync_service.py:223-463` mirrored in Elixir), +280 tests.
Five named decisions implemented as pure functions over the validated record — item-delete
guard (S-58), lifecycle (S-59), delivery authority (S-60) with its four handoffs, generic
authorization + ownership plan (S-61), evidence GC (S-62) — wired into `Admission.decide/5`
as one ordered pipeline through `VitrialSync.Authority` (`admission.ex:311-320`). Each
returns a token from the closed rejection vocabulary rather than an exception class name.
Ten new test files.

**Pull visibility resolution** (`pull_prefetch.py:116-314` ported), +22 tests.
`VitrialSync.Prefetch` with `load_page_context/3` (selection over already-loaded canonical
rows, reproducing the reference's population order and key shapes — the
`delivery_execution` project child and the two dependent sector/item → project hops),
`resolve_visible/3` and `resolve_delivery_visible/2`, written branch-for-branch against
`record_is_visible` (`app/ownership.py:942-1014`) and `delivery_execution_is_visible`
(`app/delivery_execution.py:868-896`) with the deliberate `deleted_at` asymmetry preserved
in **both** directions.

**Test count: 532** = 529 `test "` blocks + 3 doctests, counted by grep over
`beam/apps/vitrial_sync/test/`. **Unverified by execution** — `mix test` was not run. The
prefetch module was type-checked by compiling `prefetch_test.exs` with
`Code.compile_file/1` against the built beams with only `:ex_unit` started, which checks
the module without running a single test. Per-file: delivery 58, lifecycle 62, ownership 59,
prefetch 22, authority 20, canonical 20, value 20, decimal 14, evidence_gc 10,
item_delete_guard 9, rejection_code 8.

## What to do next

1. **Re-establish the environment and run the Python suite before anything is trusted.**
   `DATABASE_NULL_POOL=true`, migrations applied, `python -m pytest` (never the `pytest`
   console script). The five fixes above are unverified by execution until this happens.
2. Decide `VITR-V004` — a GET that writes. The design question is what happens to orgs
   created before the seed moved to org creation.
3. `evidence_gc_backlog` is registered at `app/metrics.py:485` and **never set**, so the
   GC backlog is invisible. One line. It belongs with whatever give-up policy is chosen for
   `VITR-V010` — give-up is deliberately not implemented, because abandoning a row means
   never reclaiming that object, which is an operator's call.
4. `VITR-V011`'s fix has a real trade-off, now that it is fixed and thus now that it
   matters: the run holds its row locks *and* its org locks until the end, so a slow object
   store holds them longer and an upload to that org waits longer. One transaction per row
   restores that at the cost of re-selecting per row and a `dry_run` that never reschedules.
5. `.staging/` orphans remain only reapable by a bucket lifecycle rule, and none is
   configured in this repo. Nothing in `app/`, `scripts/` or `deploy/` reconciles them; the
   `storage.staging_delete_failed` log line is the only handle.

## Do not touch without a deliberate decision

- **`resolve_visible`/`resolve_delivery_visible` must stay line-for-line twins** of
  `record_is_visible`/`delivery_execution_is_visible`. The Elixir port reproduces the
  `deleted_at` asymmetry in both directions — pull visibility deliberately omits
  `deletedAt` (absence is never evidence; a tombstone must be delivered), while
  `delivery_execution` checks `deleted_at` at every hop. Different rule, different question.
- **`authorize_record` must hold no `delivery_execution` branch.** The gap is the point:
  it fails closed, so a break in the `sync_service` dispatch denies delivery writes instead
  of authorizing them against a stale second gate.
- **`pull_prefetch.py` must stay the twin of the Elixir `Prefetch`.**
- Evidence PUT intentionally has no middleware ceiling; `evidence_max_bytes` (100 MiB)
  bounds it while streaming.
- Internal admin routes stay `include_in_schema=False`.

## Burned lessons from this session

- **A whole second authorization gate survived 11 green tests.** The tests called
  `authorize_record` directly; production dispatches delivery to
  `authorize_delivery_execution` first and never reaches the branch. It was not merely
  dead, it had *drifted* — fewer checks than the live gate. The general form: **a test that
  calls a function directly tests that function, not that the function runs.** Assert the
  reachability property instead (AST check that every call site sits in the dispatch's
  `else`-arm; plus a string-literal check that the branch was not quietly re-added).
- **Read-only columns hide their own absence.** `attempts`/`last_error` were written at two
  sites, declared in the model and in the migration, and read by nothing. The repo said so
  itself — at HEAD `4865052`, `tests/test_evidence_gc_postgres.py` carried the note "that
  `attempts` is never READ (no backoff, no give-up) is the open half of VITR-V010 and is
  deliberately not asserted here" (`git show HEAD:tests/test_evidence_gc_postgres.py:255`;
  the note is gone from the fixed file). Grep for a **read**, not a write, when asking
  whether a counter does anything.
- **A retry that swallows its own cleanup makes a success look like a failure.**
  `VITR-V013`'s staging-delete retry ran inside `except Exception: pass` and then
  re-raised, so the object was durably published while the client got a 503. Best-effort
  cleanup must be scoped *after* the point of no return, and be narrow enough that
  pre-publish failures still raise.
- **Do not write a status row before the evidence supports the word "Fixed."** `VITR-V004`
  sat marked Fixed against a commit whose own message says it was "the first step" of that
  fix. Check the commit's diff, not its number.

## Concurrent ownership (2026-10-03)

Two agents, one tree, no branches — both commit to `main`.

- **Python/agent work:** `app/reference_data.py`, `app/ownership.py`, `app/evidence_gc.py`,
  `app/storage.py`, `tests/test_evidence_gc_postgres.py`, `tests/test_object_storage.py`,
  `tests/test_delivery_authorization_dispatch.py` (new),
  `tests/test_reference_baseline_seed_concurrency_postgres.py` (new),
  deleted `tests/test_delivery_execution_ownership.py`, `trackers/vitrial-api-TRACKER.md`,
  `memory-seeds/`.
- **BEAM work (a parallel agent):** everything under `beam/apps/vitrial_sync/lib/` and
  `/test/` — `vitrial_sync.ex`, `admission.ex`, and the new `authority.ex`, `canonical.ex`,
  `decimal.ex`, `delivery.ex` + `delivery/`, `evidence_gc.ex`, `item_delete_guard.ex`,
  `lifecycle.ex`, `ownership.ex`, `prefetch.ex`, `provenance.ex`, `rejection_code.ex`,
  `value.ex`, plus 11 new `*_test.exs`.

The `git add -A` guidance in the 2026-10-02 seed ("commit your own files only") is
**superseded**: this tree is shared and concurrent, so a selective `git add` silently drops
the other agent's work. Stage everything and name the foreign work in the commit message
instead.
