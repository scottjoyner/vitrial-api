---
kind: candidate
recordKind: continuity
category: goal-set
seed_type: goal-set
name: vitrial-api-state-2026-10-03
description: "server-api at 342f9be, tree green: BEAM 555 tests via ./scripts/beam_gate.sh (exit 0), the differential harness committed and the gate image rebuilt so its oracle executes instead of skipping. ARCHITECTURE.md's module table was 14 modules stale and is now measured. VITR-V008/-009/-010/-011/-013 stand; several tracker line citations corrected against the files. No Python test has ever run in this workspace."
agent: nez
date: 2026-10-03
tasks: []
projects: [vitrial-api]
repos: [vitrial-api]
---

## State

`/home/nez/Projects/server-api` (GitHub `astrix-group-accounts/server-api`) @ `342f9be`, tree
green. `./scripts/beam_gate.sh` → `Result: 555 passed (3 doctests, 552 tests)`, **exit 0**
(format, `mix compile --warnings-as-errors`, `mix test`). Both Python BEAM guards exit 0:
`scripts/check_beam_scaffold.py` ("beam apps: 8  built: 1  declared scaffolds: 7" /
"scaffold registry is consistent"), `scripts/check_beam_orphans.py` ("elixir sources: 97
(lib 53, test 35)  modules: 60  reachable: 59" / "no dead modules"). Two agents work this
tree at once; see *Concurrent ownership* below before staging anything.

### The one-paragraph version of what this run changed

**The BEAM port gained the thing its own architecture document had been attributing its
parity numbers to since `8aa7604`, and the gate was rebuilt so that thing can actually
run.** `342f9be` landed `parity_oracle.py` (1,755 lines), `python_reference.ex`,
`python_source.ex`, `parity_test.exs`, `drift_test.exs` — and `beam/Dockerfile.gate`,
without which the harness skips itself. Test count 532 → 555. Waves **4c** (wire assembly)
and **4e** produced no files; 4e has no specification anywhere in the tree, so it is logged
as unbuilt rather than described.

### What was falsified by running it

Three separate green signals in this tree were measuring something narrower than the claim
attached to them. All three are now measured, and the corrections are in the records rather
than appended beside them:

1. **The gate skipped its own oracle.** The pinned `hexpm/elixir` image has no `python3`,
   so `mix test` reported `532 passed / 23 skipped` — green, with the half that proves the
   port correct never executing. Reproduced all three shapes here:
   `./scripts/beam_gate.sh` (gate image) → **555**; the same gate with
   `BEAM_IMAGE=<the bare pinned image>` → **549 passed (3 doctests, 546 tests), 6 skipped**,
   printing `[parity] differential suite SKIPPED: no python3 on PATH`; and `ci.yml`'s BEAM
   job shape (`-v "$PWD/beam:/work"`, bare image) → **532 passed (3 doctests, 529 tests),
   23 skipped**, printing both skip reasons. **`ci.yml` still runs that third shape, so the
   oracle does not run in CI** — pointing the job at `beam/Dockerfile.gate` is the open item.
2. **`ARCHITECTURE.md` was 14 modules and thousands of lines stale.** The file was last
   written at `8aa7604`, before waves 4a and 4b landed, and it said "2,332 lines across
   12 modules and 230 tests". A later in-tree draft corrected the arithmetic to 2,458
   lines and 552 tests and **still said 12 modules** — it had re-summed the same twelve.
   The truth measured by `find beam/apps/vitrial_sync/lib -name '*.ex' | xargs wc -l` is
   **8,992 lines across 26 modules**. The 14 missing ones are waves 4a and 4b —
   `ownership.ex` (1,666), `delivery/handoff.ex` (1,436), `delivery.ex`, `lifecycle.ex`,
   `canonical.ex`, `prefetch.ex`, `authority.ex`, `decimal.ex`, `value.ex`, `provenance.ex`,
   `rejection_code.ex`, `evidence_gc.ex`, `item_delete_guard.ex`, `vitrial_sync.ex`. Its §3
   also still listed 4a and 4b as work to do. A document whose headline number is a
   partial sum reads exactly like a correct one, and correcting the sum without correcting
   the row count is the same error twice.
3. **`scripts/beam_gate.sh`'s `BEAM_BASE_IMAGE_ONLY=1` does not force the bare image.**
   The header promises it "forces the bare pinned image … useful for proving the fallback
   path", but the variable is only read inside the build-on-demand block and `IMAGE` was
   already defaulted to the gate image above it. Run on a host where the gate image exists
   it silently keeps using it: measured, `BEAM_BASE_IMAGE_ONLY=1 ./scripts/beam_gate.sh`
   still reported `555 passed`. `BEAM_IMAGE=` is the override that works.

## The differential harness, and what it measured

`test/support/parity_oracle.py` cannot `import app.sync_service` (SQLAlchemy), so it
AST-lifts what it measures out of `app/` and executes it against the real
`app.schemas.SyncBatch`/`SyncRecord`. Every input the Elixir side tests is emitted by the
oracle as data — the suite never retypes a case, which is not tidiness: a prototype that
listed cases in Elixir produced nineteen false mismatches from the retyping alone.

Measured 2026-10-03 by running the oracle and reading its document:

| Surface | Result |
|---|---|
| admission decisions | **44 cases / 50 records**, every `{status, reason, result_revision}` identical, including the step-4 boundary |
| pull page wire sizes | **117 cases** (17 named + 100 seeded-random), `Page.batch_byte_size/2` byte-identical to real `SyncBatch.model_dump_json()` on every case. All three ceilings reached by a named case: `count_ceiling` 200 records / 42,020 B, `payload_ceiling` 1,050,246 B, `response_ceiling` 169 records / 2,247,982 B |
| payload decode | **169 cases** — 114 rejected by both estates, 42 accepted byte-identical, 13 divergences that are exactly the two documented narrowings (7 non-finite, 6 unpaired surrogates) |

These replace the 29/29, 100/100, 248/258 that `ARCHITECTURE.md` carried. **Those numbers
were unrepeatable and had to be deleted rather than updated:** no corpus in the tree could
produce them — the 258-case payload corpus existed only in the commit messages of `3d87468`
and `36f32b1`, describing a harness that was never committed. `ParityTest` now asserts all
seven of these figures (`@documented`) against each fresh oracle run, so the document and
the corpus cannot drift apart silently.

The harness found a defect **on the reference side**: `SyncRecord.payload` is typed `bytes`
with no UTF-8 check (`app/schemas.py:76`), so a non-UTF-8 payload validates and
`pull_page_accepts`'s `model_dump_json()` then raises `PydanticSerializationError` — an
unhandled 500 out of the pull loop. Pinned by the `non_utf8_payload` case, so a reference fix
that changes the failure's shape shows up here.

**On the wire format.** The reference's own docstring (`app/sync_service.py:71-72`) says the
page-fit predicate matters "once base64 and per-record framing are included". There is no
base64 layer — measured: a 1,000-byte payload of NULs serializes inside a 6,239-byte batch
as `\u0000` escapes, and invalid UTF-8 raises rather than being base64'd. What replaces it
is JSON string escaping, which *expands* — 1.12× for non-ASCII text, 6.24× for control bytes
— plus ~243 bytes of per-record framing for a typical JSON-object payload. The predicate is
still necessary and still measured on real bytes; the reason is framing plus escaping.

## Five defects closed, one corrected

Unchanged by this run — nothing here touched `app/`. Their line citations were re-measured
against the files and four were wrong; the corrected numbers are in
`trackers/vitrial-api-TRACKER.md` and are marked there as re-measured.

| ID | What it was | Now |
|---|---|---|
| `VITR-V008` | four reference-data GETs call `ensure_baseline_publications`, which does check-then-insert with no lock / `ON CONFLICT` / retry; under READ COMMITTED the loser's INSERT raises an unhandled 23505 → 500 | `_seed_baseline_publications` split out; `ensure_baseline_publications` catches `IntegrityError`, rolls back, re-checks (`app/reference_data.py:417-478`). Race half only |
| `VITR-V009` | `authorize_record`'s `delivery_execution` branch is unreachable in production (sole caller sits in the `else`-arm of the `DELIVERY_ENTITY_TYPE` dispatch) and had **drifted** from the live gate — it skipped the `delivery.manage` capability check on tombstones and permitted tombstoning a known execution, which the live copy refuses | branch and its orphan helper `_current_quotation_id` deleted; delivery now falls through to the closing `raise` at `app/ownership.py:847` and the generic gate **fails closed**, with the gap documented in place at `:835-846` |
| `VITR-V010` | `not_before` written once at queue time and read only in the selection predicate, so both retain paths reselected the row on the very next run forever; `attempts`/`last_error` write-only | `_reschedule` at both sites (`app/evidence_gc.py:40`, called `:192`, `:211`), 300 s doubling capped at 3600 s. `attempts` now has a reader |
| `VITR-V011` | collector selects `FOR UPDATE SKIP LOCKED` once then `commit()`s four times *inside* the row loop, releasing locks on rows not yet reached; the UPDATE path's rowcount check raises `StaleDataError` uncaught, aborting the run | one transaction for the whole run, `SAVEPOINT` per row via `db.begin_nested()` at `:144`, single `commit()` at `:217` (`:99-217`) |
| `VITR-V013` | `S3ObjectStore.put_verified` publishes with `copy_object`, then a failing staging delete fell into the broad handler, was swallowed by a bare `except: pass` retry and re-raised as `StorageError` → `put_blob` 503s at `app/evidence.py:160`, *before* the `EvidenceBlob` row and *before* the GC row: a durable, digest-verified object at its final key with nothing referencing it and nothing to reclaim it | post-publish staging delete is best-effort and logs `storage.staging_delete_failed` at WARNING (`app/storage.py:234-246`, log at `:240`) |

`VITR-V004` is **still open** and the tracker was wrong about it. `33ccee4` self-describes
as "the first step of the VITR-V004 fix" and its diff only splits out `_list_publications`
— all four GET helpers still seed and `commit()` (`app/reference_data.py:511,541,551,595`;
the fifth call site, `:485`, is the POST). A GET that writes is still a GET that writes;
moving the seed to org creation is a design decision (what happens to orgs that predate
it), not a drive-by.

## What was NOT run (this is the load-bearing caveat)

**No Python test was executed in this session, and no defect fix in the table above is
verified by execution.** Every Python finding and every fix is static reading plus a formula
replay. This is unchanged from the earlier state of this seed and is the single most
important thing to carry forward.

- `python -m pytest …` → collection ERROR, `ModuleNotFoundError` (no sqlalchemy, no boto3).
  No venv in this workspace.
- PostgreSQL unreachable from this session; `docker`/`podman`-backed services not started
  for it. The durable cluster and venv named in the 2026-10-02 seed (`~/.pgdata/vitrial`
  on port 5435, `~/.venvs/vitrial-api`) were **not** reachable. Do not assume they still
  exist; re-establish before trusting any green Python suite.
- Every affected file is additionally gated on `POSTGRES_INTEGRATION=1` or `S3_INTEGRATION=1`.

**What did run here, with the output**, so the BEAM half is executed rather than asserted:

| Check | Command | Result |
|---|---|---|
| BEAM gate | `./scripts/beam_gate.sh` | `555 passed (3 doctests, 552 tests)`, `beam gate: PASS`, exit 0 |
| Gate, bare image | `BEAM_IMAGE=<hexpm pinned> ./scripts/beam_gate.sh` | `549 passed (3 doctests, 546 tests), 6 skipped` |
| CI's job shape | `podman run -v "$PWD/beam:/work" -w /work <hexpm pinned> bash -lc 'mix compile --warnings-as-errors && mix format --check-formatted && mix test'` | `532 passed (3 doctests, 529 tests), 23 skipped`, both skip reasons printed |
| Scaffold guard | `python3 scripts/check_beam_scaffold.py` | `beam apps: 8  built: 1  declared scaffolds: 7` / consistent, exit 0 |
| Orphan guard | `python3 scripts/check_beam_orphans.py` | 97 sources, 60 modules, 59 reachable, no dead modules, exit 0 |
| Contract | `python3 scripts/validate_backend_contract.py` | `backend contract validation: PASS`, exit 0 |
| Byte-compile | `python3 -m compileall -q app scripts tests` | exit 0 |

## BEAM waves — `beam/apps/vitrial_sync`

**8,992 lines across 26 modules, 555 tests**, all pure computation over validated input.
`beam/mix.lock` and `beam/deps` do not exist; nothing in `lib/` opens a socket, touches a
file or starts a process.

**Admission steps 5-8** (`app/sync_service.py:227-477`; the next `def` is at `:479`).
Five named decisions as pure functions over the validated record — item-delete guard (S-58),
lifecycle (S-59), delivery authority (S-60) with its four handoffs, generic authorization +
ownership plan (S-61), evidence GC (S-62) — wired into `Admission.decide/2` as one ordered
pipeline through `VitrialSync.Authority`. Each returns a token from the closed rejection
vocabulary rather than an exception class name.

**Pull visibility resolution** (`pull_prefetch.py:116-314` ported). `VitrialSync.Prefetch`
with `load_page_context/3`, `resolve_visible/3` and `resolve_delivery_visible/2`, written
branch-for-branch against `record_is_visible` and `delivery_execution_is_visible` with the
deliberate `deleted_at` asymmetry preserved in **both** directions.

**Wire assembly (4c) is not built**, and it is the last slice in wave 4 that can be built
without a database.

**Test count: 555** = 552 tests + 3 doctests, verified by `mix test` through
`./scripts/beam_gate.sh`. Per test module, aggregated from a `--trace` run rather than
grepped (each case is emitted twice by `--trace`, so the count is halved):

| | | | |
|---|---:|---|---:|
| lifecycle 62 | ownership 59 | delivery 58 | json 38 |
| page 25 | admission 23 | prefetch 22 | canonical_json 22 |
| value 20 | canonical 20 | canonical_json_values 20 | authority 20 |
| cursor 18 (+3 doctests) | drift 17 | retry 16 | fingerprint 15 |
| record 14 | payload 14 | decimal 14 | entity_order 13 |
| evidence_gc 10 | timestamp 9 | item_delete_guard 9 | rejection_code 8 |
| parity 6 | | | |

## What to do next

1. **Point `ci.yml`'s BEAM job at `beam/Dockerfile.gate`,** or fix `BEAM_BASE_IMAGE_ONLY=1`
   to actually force the bare image. Until one of those happens, the parity corpus runs
   nowhere but a local gate, and a CI badge that says "BEAM green" means "BEAM green with
   the oracle skipped."
2. **Re-establish the environment and run the Python suite before anything is trusted.**
   `DATABASE_NULL_POOL=true`, migrations applied, `python -m pytest` (never the `pytest`
   console script). The five fixes above are unverified by execution until this happens.
3. Build **4c**, wire assembly — the last zero-dependency slice in wave 4.
4. Decide `VITR-V004` — a GET that writes. The design question is what happens to orgs
   created before the seed moved to org creation.
5. `evidence_gc_backlog` is registered at `app/metrics.py:485` and **never set**, so the
   GC backlog is invisible. One line. It belongs with whatever give-up policy is chosen for
   `VITR-V010` — give-up is deliberately not implemented, because abandoning a row means
   never reclaiming that object, which is an operator's call.
6. `VITR-V011`'s fix has a real trade-off, now that it is fixed and thus now that it
   matters: the run holds its row locks *and* its org locks until the end, so a slow object
   store holds them longer and an upload to that org waits longer. One transaction per row
   restores that at the cost of re-selecting per row and a `dry_run` that never reschedules.
7. `.staging/` orphans remain only reapable by a bucket lifecycle rule, and none is
   configured in this repo. Nothing in `app/`, `scripts/` or `deploy/` reconciles them; the
   `storage.staging_delete_failed` log line is the only handle.
8. **`beam/apps/PLANNED_APPS.txt`'s `vitrial_ownership` line is false.** It still declares
   that `app/ownership.py:726` carries an unreachable duplicate delivery block, citing
   `VITR-V006`; that block was deleted by the `VITR-V009` fix and the defect was `VITR-V009`.
   `check_beam_scaffold.py` checks the line's *presence*, not its truth, so nothing catches
   this class of rot. Same for `beam/README.md`, which still describes the Python side as
   "7,884 lines of application code with 11,487 lines of tests" — measured now: **10,274
   and 15,199** (`wc -l app/*.py`, `wc -l tests/*.py`).

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
- **The Python service is not being retired.** It is the reference implementation and the
  parity oracle; `beam/ARCHITECTURE.md` §5 says why deleting it would delete the only thing
  that can prove the BEAM code is right.
- Evidence PUT intentionally has no middleware ceiling; `evidence_max_bytes` (100 MiB)
  bounds it while streaming.
- Internal admin routes stay `include_in_schema=False`.

## Burned lessons

- **A gate narrower than the work it gates turns "covered" into "never ran", silently.**
  Three instances in this tree, and the third was found only because the count moved: the
  boto3 stub too narrow for its call (`lambda **kwargs` against a positional
  `boto3.client("s3", …)`), the orphan guard blind at clause granularity (only
  `mix compile --warnings-as-errors` saw `f1ba4ab`'s unreachable clause), and now the pinned
  image having no `python3`, so `mix test` reported green with the oracle skipped. **A skip
  count is a measurement of the gate, not of the code — read it every run.**
- **A document whose headline number is a partial sum reads exactly like a correct one.**
  `ARCHITECTURE.md` said "12 modules" while 26 existed, because it was last written before
  two waves landed and nothing recomputed it. Numbers in prose rot silently; the fix that
  works is to make something *assert* them (as `ParityTest`'s `@documented` now does) or to
  re-measure them on every touch, because neither of those can be satisfied by a stale
  sentence.
- **A whole second authorization gate survived 11 green tests.** The tests called
  `authorize_record` directly; production dispatches delivery to
  `authorize_delivery_execution` first and never reaches the branch. It was not merely
  dead, it had *drifted* — fewer checks than the live gate. The general form: **a test that
  calls a function directly tests that function, not that the function runs.** Assert the
  reachability property instead.
- **Read-only columns hide their own absence.** `attempts`/`last_error` were written at two
  sites, declared in the model and in the migration, and read by nothing. Grep for a
  **read**, not a write, when asking whether a counter does anything.
- **A retry that swallows its own cleanup makes a success look like a failure.**
  `VITR-V013`'s staging-delete retry ran inside `except Exception: pass` and then
  re-raised, so the object was durably published while the client got a 503. Best-effort
  cleanup must be scoped *after* the point of no return, and be narrow enough that
  pre-publish failures still raise.
- **Do not write a status row before the evidence supports the word "Fixed," and re-check
  the line numbers too.** `VITR-V004` sat marked Fixed against a commit whose own message
  says it was "the first step". Separately, four of this tracker's own `:line` citations had
  drifted by the time this run re-measured them (`VITR-V009`'s `:750-762`, `VITR-V010`'s
  `:160`/`:192`, `VITR-V013`'s `:219-246`, `VITR-V005`'s `:343`/`:42`). A citation is a
  claim about a file, and it decays the same way a status does.

## Concurrent ownership (2026-10-03)

Two agents, one tree, no branches — both commit to `main`.

- **Python/agent work:** `app/reference_data.py`, `app/ownership.py`, `app/evidence_gc.py`,
  `app/storage.py`, `tests/test_evidence_gc_postgres.py`, `tests/test_object_storage.py`,
  `tests/test_delivery_authorization_dispatch.py` (new),
  `tests/test_reference_baseline_seed_concurrency_postgres.py` (new),
  deleted `tests/test_delivery_execution_ownership.py`, `trackers/vitrial-api-TRACKER.md`,
  `memory-seeds/`.
- **BEAM work (a parallel agent):** everything under `beam/apps/vitrial_sync/lib/` and
  `/test/`, plus `beam/Dockerfile.gate`, `scripts/beam_gate.sh`, `beam/ARCHITECTURE.md`.

The `git add -A` guidance in the 2026-10-02 seed ("commit your own files only") is
**superseded**: this tree is shared and concurrent, so a selective `git add` silently drops
the other agent's work. Stage everything and name the foreign work in the commit message
instead.
