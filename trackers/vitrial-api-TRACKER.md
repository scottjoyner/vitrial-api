---
id: VITR-TRACKER-001
title: "vitrial-api — reconciled work tracker"
owner: vitrial-api
category: TRACKER
status: active
date: 2026-10-03
---

# vitrial-api — Work Tracker

> Reconciles GitHub issues against what has **actually shipped** on `main`
> (HEAD `342f9be`, 2026-10-03). Only check a box when a commit in `git log` is
> evidence of it. Use `[~]` for the part-that-works, not `[x]`.

> **Verification status of the rows below (2026-10-03, at `342f9be`).** `VITR-V008`,
> `-009`, `-010`, `-011` and `-013` are marked Fixed and that is **executed** evidence,
> not inspection: the CI run on `55876bd` is green on all four workflows — Unit + contract
> `353 passed, 63 skipped`, PostgreSQL + S3 integration `416 passed`, BEAM umbrella
> `532 passed (3 doctests, 529 tests)`, Container build and Security both success. Nothing
> in this run touched `app/`, so those Python rows stand as they were.
> Still true and still worth knowing: there is **no local Python environment**
> (`python -m pytest` fails collection with `ModuleNotFoundError: No module named 'boto3'`,
> no sqlalchemy) and PostgreSQL is unreachable from this workspace, so the Python half is
> verifiable only by pushing.
>
> **The BEAM half moved this run and every BEAM number below was re-measured here.**
> `./scripts/beam_gate.sh` → `Result: 555 passed (3 doctests, 552 tests)`, **exit 0**,
> up from 532 + 23 skipped: `342f9be` landed the differential harness
> (`parity_oracle.py` + `python_reference.ex` + `parity_test.exs` + `drift_test.exs`,
> +23 tests) and `beam/Dockerfile.gate`, which gives the gate container the `python3` the
> oracle needs to execute rather than skip itself. Guards re-run here, all exit 0:
> `check_beam_scaffold.py` (8 apps, 1 built, 7 declared), `check_beam_orphans.py`
> (97 sources, 60 modules, 59 reachable, no dead modules), `validate_backend_contract.py`,
> `compileall`.
> Full state and next steps: `memory-seeds/vitrial-api-state-2026-10-03.md`.

> **A green gate is a statement about the gate, not about the port.** The pinned
> `hexpm/elixir` image has no `python3`, so before `342f9be` the differential
> harness skipped itself and `mix test` reported `532 passed / 23 skipped` — all
> green, with the oracle that proves the port correct never executed. All three
> figures below were reproduced by running each shape here, not quoted:
> `./scripts/beam_gate.sh` (gate image) **555 passed**; the same gate with
> `BEAM_IMAGE=<the bare pinned image>` **549 passed (3 doctests, 546 tests), 6 skipped**,
> printing `[parity] differential suite SKIPPED: no python3 on PATH`; and `ci.yml`'s
> BEAM job shape (`-v "$PWD/beam:/work"`, bare image) **532 passed (3 doctests,
> 529 tests), 23 skipped**, printing both skip reasons. **`ci.yml` still runs the
> bare image against a `beam/`-only mount, so the oracle does not run in CI yet** —
> pointing that job at `beam/Dockerfile.gate` is the open item, and until it is done
> the local gate is the only place the oracle executes.
>
> **The two reds that hid behind each other** (between `4865052` and `55876bd`, both
> arriving with the assembly-topology merge `d28cd62`) are the same defect class as the
> above, and the lesson generalises to every "pinned by" clause in this file. A boto3
> client stub in `tests/test_object_storage.py` was `lambda **kwargs` against a
> **positional** call (`boto3.client("s3", endpoint_url=…)`, `app/storage.py:140-141`), so the
> three tests pinning `VITR-V013` raised `TypeError` before any assertion — the row below
> claimed S3 coverage that did not exist. And 22 Elixir files in `vitrial_sync` were
> unformatted, which is the first step of the Beam CI job. Fixed in `55876bd`; the unit
> job moved 350+3failed → 353 passed, which is the count arithmetic for exactly those three
> tests. **A stub narrower than the call it stands in for, and a gate narrower than the
> work it gates, both turn "covered" into "never ran" — silently.**

## Legend

- `[x]` shipped and verified
- `[~]` partial — some of the claim holds, rest is tracked as a defect
- `[ ]` not done

## GitHub issues

| # | Title | State |
|---|---|---|
| 28 | [0.3.0] Production foundation | OPEN — the real 0.3.0 spec |
| 30 | [0.3.0] Replace legacy MinIO dependency | OPEN — landed in `#34/#35/#63`, box still open |
| 2 | [0.2.0] Harden backend for device acceptance | Open — effectively complete |

No open PRs. No Jira, no in-repo board, no beads records.

---

## Issue #28 reconciliation (2026-10-02)

### P0 — bound the data plane

- [~] Define a hard **aggregate** sync request ceiling enforced while reading the body.
  V1 has it (`MAX_SYNC_V1_BATCH_PAYLOAD_BYTES` + `RequestSizeLimitMiddleware`).
  **V2 does not** — see `VITR-V001`. Chunked path is buffered by the middleware.
- [x] Hard sync response/page byte budget, deterministic by record count **and** encoded bytes
  (`pull_page_accepts`, `MAX_SYNC_PULL_RESPONSE_BYTES`; `sync_service.py`).
- [x] Evidence uploads on a separate streaming ceiling (`evidence_max_bytes`).
- [~] Content-Length **and** chunked over-limit tests exist for V1
  (`test_request_size.py`); near-limit-record and compressed-transport tests do not.
- [~] Edge/application rate limits. Auth surface only, and only on one route.
  Sync/evidence/admin are unthrottled; pairing limiter is XFF-evadable (`VITR-V003`).
- [ ] Load-test rejection paths so abusive traffic fails cheaply.

### P0/P1 — production auth/session model

- [~] Separate admin control plane from public listener. Admin is
  `include_in_schema=False` and disabled unless `ADMIN_API_KEY_HASH` is set, but it still
  shares the one listener with public routes.
- [x] Pairing/auth attempt throttling. Implemented on `/api/v1/auth/pair` — but weakened by `VITR-V003`.
- [ ] Production session renewal; today a fixed bearer TTL + revoke-only.
- [ ] Device-bound sessions (copied bearer stays useful without proof-of-possession).
- [x] Explicit revocation, membership-removal, authorization-revision behaviour
  (revoke + revoke-by-org; every `auth/me` and session yields the org's current revision).
- [x] One-time grant exchange outcomes indistinguishable to unauthenticated callers.

### P1 — sync scalability & concurrency

- [x] Benchmark org-wide `FOR UPDATE` serialization (`#59`) — measured, cost noted.
- [ ] Replace repeated `max(server_revision)+1` with a safe revision allocator — not replaced; still correct under the org row lock.
- [x] Remove/contain pull N+1 — `pull_prefetch.py` batched it; measured `#61` (604 → 4).
- [x] Configure DB pool explicitly (`#42`).
- [x] Deadlock/serialization retry policy (`#46/#49/#56`).
- [~] Concurrency tests for duplicate mutation IDs (`test_sync_concurrency.py`),
  tombstone resurrection (`#50`), cross-device lifecycle; evidence-replace contention not directly covered.

### P1 — reproducible deploy & supply chain

- [ ] Reproducible Python lock. Runtime deps are version ranges in `pyproject.toml`, no lock file.
- [~] Pin runtime images by digest. `API_IMAGE` is digest-pinned in production; Caddy, Postgres, S3 images are floating (`#63` warns on the S3 fallback).
- [ ] Image/package vulnerability scanning and SBOM in candidate CI.
- [x] Validate production settings on startup/preflight (`validate_deployment.py`, `probe_production_preflight.py`, `deploy/README.md`).
- [~] Migration compatibility + rollback rehearsal. CI proves `upgrade → downgrade base → upgrade`; production rollback intentionally does **not** auto-downgrade DB (documented).

### P1 — resilience & operations

- [x] Metrics for request rate/latency/status, auth failures, sync outcome, DB pool, S3 latency, GC backlog, page sizes — `app/metrics.py`, `GET /metrics` (admin-gated, out of the pinned schema). Labels on the matched route template with a series cap; sync outcomes on the stable `VITR-V006` vocabulary; pull page size plus a truncation counter.
- [~] Trace/correlation propagation. `X-Request-ID` accepted/preserved and one-way `sha256:` refs for org/actor/membership/session/entity/device. It is not a W3C `traceparent`; nothing crosses the iOS→API→DB→S3 boundary as one trace.
- [ ] SLOs/alerts for availability, p95, sync failure rate, dependency readiness.
- [x] Graceful shutdown/drain test with in-flight sync and evidence streams — `app/drain.py`, `/ready` 503 on SIGTERM while `/health` stays 200, 45s uvicorn budget inside a 60s `stop_grace_period`, `deploy.sh` awaits the replaced container and fails on non-zero exit.
- [ ] PostgreSQL backup/PITR and restore drill.
- [~] S3 versioning/retention/restore policy and orphan/GC reconciliation runbook — GC exists and is scheduled (`deploy/gc-evidence.timer`), but no documented restore policy.

### P1 — contract discipline

- [ ] Make V2 the production sync contract target with an explicit V1 deprecation window. Both are served; no window defined.
- [~] Aggregate-size, schema-version, pagination semantics in the OpenAPI/contract docs. `entitySchemaVersion` exists; aggregate byte budgets are enforced but not fully described in the pinned contract.
- [x] Federation fixtures consumed by both Python and Swift tests (`contracts/`).
- [~] Old-client-against-new-server and vice-versa acceptance tests. V1 is pinned to a client SHA, but no cross-version acceptance matrix exists.
- [x] Server authority never depends on iOS-only validation — ownership/lifecycle re-derived server-side in `ownership.py`, `lifecycle.py`, `delivery_execution.py`.

---

## Verified defects (VITR-Vxxx)

| ID | Severity | Title | Status |
|---|---|---|---|
| `VITR-V001` | HIGH | V2 request size / aggregate ceiling missing; dead guard prefixes on three routes | **Fixed** in W1 (prefixes corrected, `SyncBatchV2` aggregate ceiling, guard lint in CI) |
| `VITR-V002` | MED | Evidence header params unbounded while destination columns are bounded → 500 not 4xx | **Fixed** (`5f6178f`) |
| `VITR-V003` | HIGH | Auth rate limiter evadable via spoofed left-most `X-Forwarded-For` | **Fixed** (`d0e1d93`) |
| `VITR-V004` | LOW | Reference-data GET endpoints write + commit (`ensure_baseline_publications`) | **Not fixed** — `33ccee4` ("the first step of the VITR-V004 fix") only split out `_list_publications` and halved the per-manifest seed. All four GET helpers still call `ensure_baseline_publications` (`app/reference_data.py:511,541,551,595`), which still inserts and `commit()`s. Status corrected 2026-10-03. |
| `VITR-V005` | MED | Evidence reference safety scan is O(all measurements + requirements) per delete | **Fixed** (`30b29ab`) |
| `VITR-V006` | MED | Rejection outcome is `reason=<exception class name>`, not a code | **Fixed** (`257e725`) |
| `VITR-V007` | **HIGH** | Evidence GC can delete the object an in-flight upload is publishing — silent data loss | **Fixed** (`76e11e5`) |
| `VITR-V008` | MED | GET routes write and `commit()`; concurrent first reads raise an unhandled `IntegrityError` → 500 | **Fixed** (race half) — `ensure_baseline_publications` catches 23505, rolls back and re-checks (`app/reference_data.py:417-478`, verified by line number this run); pinned by `tests/test_reference_baseline_seed_concurrency_postgres.py`. The write-on-GET half of the title is `VITR-V004` and is **not** fixed. Commit-worthy on its own: a 500 no client retry can resolve, with the first coverage of the four GET helpers under concurrency. |
| `VITR-V009` | MED | `ownership.py`'s whole `delivery_execution` branch is unreachable; its tests drive dead, already-divergent code | **Fixed** — the dead branch and its helper `_current_quotation_id` are deleted, so `delivery_execution` falls through `authorize_record`'s closing `raise` and the generic gate now **fails closed** for delivery. The reason it is in place is written at `app/ownership.py:835-846`; the raise itself is `app/ownership.py:847` (**re-measured this run** — the `750-762` citation carried by earlier revisions of this row and by the memory seed pointed at quotation capability logic, not the closing gate). `tests/test_delivery_execution_ownership.py` (11 tests against a gate no request reaches) removed, replaced by `tests/test_delivery_authorization_dispatch.py`. Commit-worthy on its own: it removes a second authorization gate rather than adding one. |
| `VITR-V010` | MED | GC rows that are blocked or failing are never backed off, and `attempts` is write-only | **Fixed** — both retain paths now reschedule `not_before` through `_reschedule` (`app/evidence_gc.py:40`, called at `:192` and `:211` — **both re-measured this run**; the `:160`/`:192` pair in earlier revisions of this row was wrong), so `attempts` is read as the backoff's input (300 s doubling, capped at 3600 s). **Give-up is deliberately not implemented** — abandoning a row means never reclaiming that object, which is a maintainer's call. |
| `VITR-V011` | MED | Collector `commit()`s inside its row loop, releasing `SKIP LOCKED` rows; overlapping runs hit an uncaught `StaleDataError` | **Fixed** — one transaction for the whole run with a `SAVEPOINT` per row (`app/evidence_gc.py:144`), so the `SKIP LOCKED` locks survive to the single commit at `app/evidence_gc.py:217` (`:99-217`, both re-measured this run); pinned by a two-collector test plus a structural no-commit-in-the-loop assertion. Commit-worthy on its own: it makes two shipped comments true that the ordering had made false. |
| `VITR-V012` | LOW | The collector's happy-path test cannot pass under the shipped 86400s GC grace, so GC is effectively unexercised | **Fixed** (`0fab6f8`) |
| `VITR-V014` | **HIGH** | `evidence_gc`'s `StorageError` handler reads `row.id` after `db.rollback()`, raising `MissingGreenlet` — a failing object store aborts the whole collection run | **Fixed** (`0fab6f8`) |
| `VITR-V013` | LOW | `put_verified` can leak a published object and report 503 if the staging cleanup delete fails | **Fixed** — the post-publish staging delete is now best-effort and logs `storage.staging_delete_failed` instead of escalating to `StorageError` (the `try`/`except` is `app/storage.py:234-246`, the log at `:240`, and the comment explaining why is `:219-233` — all three re-measured this run; earlier revisions of this row cited `:219-246`, which conflated the comment with the code). Pinned by three S3-store tests in `tests/test_object_storage.py` that need no S3. Commit-worthy on its own: it moves a production-only code path (S3 is what both compose files ship) from untested to covered in the default suite. **The pinning was itself broken until `55876bd`** — the client stub could not accept the positional service name, so all three raised `TypeError` at construction and never reached an assertion; they now run (unit job 350+3failed → 353 passed). |
| `VITR-V015` | MED | `validate_assembly_contract_mutation` (`app/assembly_contract.py`, 399 lines; validator at `:294-330`) has no BEAM port, so the BEAM push accepts payloads the reference rejects with `invalid_mutation` | **Not fixed — named, tracked, and fenced.** The reference runs the validator immediately after payload decode and inside the same `try` (`app/sync_service.py:319-323`), so a decode-valid but assembly-contract-invalid `configuration`/`quotation` payload is rejected with mutation + fingerprint rows written; the BEAM `VitrialSync.Batch` walk (wave 4c) has no step there, and nothing else on the BEAM establishes the precondition — `ownership.ex`'s design-snapshot binding assumed it, and its comment now says so. The validator is a pydantic-model vertical and is its own task, not a step of wave 4c; inventing a rejection the reference does not make would be a worse violation. Fenced on both sides: the gap is named in `VitrialSync.Batch`'s step table, and `drift_test.exs` asserts the reference's decode → validate order in code, so the gap cannot silently close in either estate unnoticed. |

`VITR-V007` is the serious one: a client received **201** for an upload whose object the
collector had already deleted, and every later `GET`/`HEAD` on that document 404'd. Root
cause was that `put_blob` streams the object *before* taking the organization row lock
(deliberately — a 100 MiB upload must not hold a transaction for its duration), so the
lock serialized the pointer switch but left the write outside it. The comment at
`evidence_gc.py` asserted a protection the ordering did not provide.

**These last six were found by a systematic read of auth/ownership/sync/storage, not by
a failing test.** That is itself the finding: nothing in the suite exercises the GC happy
path (`VITR-V012`), which is the direct reason `VITR-V007`, `-010`, `-011` and `-013` all
remain reachable. Fixing the *test* is the highest-leverage item here.

`VITR-V009` deserves a note: `authorize_record` has one production caller, and delivery
records never reach it — `tests/test_delivery_execution_ownership.py` (295 lines)
validated a dead path that had already diverged (its tombstone branch had no capability
check, which the live copy enforces). It was harmless, and a booby trap for the
next refactor. **Fixed 2026-10-03:** the branch, its helper and that test file are
gone. The lesson it records is worth keeping: eleven green tests proved nothing about
production because they called the function directly. The replacement asserts the
*reachability* property — `authorize_record` refuses `delivery_execution` without
touching the database, and holds no delivery branch at all — which is checkable
without a stub session and cannot be satisfied by re-adding a gate nobody calls.

**`VITR-V012` was the keystone.** Its fix immediately produced `VITR-V014`: the collector's
`StorageError` handler read `row.id` *after* `db.rollback()`, and because a rollback
expires every instance in the session, that triggered lazy IO from a synchronous context
and raised `MissingGreenlet`. The handler written to record a failed collection instead
aborted the entire run — exactly when object storage is unhealthy and an operator most
needs the failure recorded. Fixed by reading the primary key before the rollback.

Two lessons from it:

1. **A test gated behind an integration flag is not coverage.** The collector was fully
   exercisable against `LocalObjectStore` with no S3 at all; the gate was needed only for
   multipart behaviour, and had been used to excuse testing the collector's logic too.
2. **That test could never have passed.** `queue_blob_gc` sets `not_before = now + 86400`
   and the test collected at wall-clock `now`, then asserted `deleted >= 1`. It needed
   `EVIDENCE_GC_GRACE_SECONDS=0`, a value no deployment ships. Measured against the local
   store rather than assumed: `deleted=0` at wall-clock `now`, `deleted=1` past the grace.
   An assertion that cannot be satisfied by the shipped configuration is a test of
   nothing, and a green suite is not evidence it is correct.

### `VITR-V001` — dead guard prefixes

`request_size.py:79-82` and `rate_limit.py` declare prefixes that do not match any
live route (`/api/v1/sync/v2/push`, `/api/v1/admin`, `/api/v1/auth/refresh`). Measured:
`/api/v2/sync/push` accepts a 20 MB body (→422), while `/api/v1/sync/push` rejects at 4 MiB (→413).
**Root cause:** guard tests assert against string literals, none enumerate `app.routes`.
Same class recorded in-repo three times (`request_size.py:40-44`, `pull_prefetch.py`, `Makefile:16-18`).

### `VITR-V002` — evidence header bound gap

`X-Vitrial-Item-ID` / `X-Vitrial-Filename` require `min_length=1`, no `max_length`, in both the pinned
contract and the handler, but `evidence_blobs.item_id` is `String(256)` and `filename` `String(512)`.
An over-long value streams and digest-verifies fully, then fails at `INSERT` → 500.
Should be `max_length` + a 4xx, fixed via a V1 contract revision.

### `VITR-V003` — XFF-evadable rate limit

`client_key_for` keys on the **left-most** `X-Forwarded-For` entry. `deploy/Caddyfile` strips nothing;
`--forwarded-allow-ips=*` and no `trusted_proxies` set. Reproduced: 40 unique spoofed XFFs → 40 unthrottled.
Real client needs Caddy `header_up` / `trusted_proxies` or a right-most key.

### `VITR-V004` — reference GET writes

Every reference-data GET calls `ensure_baseline_publications`, which inserts and `db.commit()`s.
A `GET` mutates, and `/manifest` does it twice per call.

### `VITR-V005` — evidence scan O(N)

`_ensure_evidence_delete_safe` and `_is_referenced` loaded every
`measurement`/`customer_requirement` payload and scanned it in Python. Linear:
100 rows 11 ms → 5,000 rows 79 ms. Both functions still exist and are still the
call sites — now at `app/ownership.py:404` and `app/evidence_gc.py:76` (the
`:343`/`:42` pair this section used to cite was where they sat before the
fix) — but the scan itself is gone: `_evidence_reference_filter`
(`app/ownership.py:375`) pushes the lookup into PostgreSQL as `jsonb @>`.

### `VITR-V006` — rejection reason collapses to class name

Before the fix, `sync_service.py` set `reason=type(exc).__name__`. Sync outcome
logs carried ~5 reasons while the code expressed 183, and nothing aggregated
them. The emission point is now `app/sync_service.py:399`
(`reason=rejection_code(exc)`), with the three transport/framing reasons spelled
out literally beside it at `:247`, `:313` and `:440`.

---

## Work queue — ordered for long sessions

Pick top-down. Each task has: goal, files, tests to run, done-when. Run the suite
(see `AGENTS.md` compliance checklist for the exact invocation) after each task.

### W1 — `VITR-V001` guard-layer dead rules (top target, spec'd on #28 P0)
- [x] `app/request_size.py` + `app/rate_limit.py`: dead prefixes replaced with the real
      `/api/v2/sync/push` and `/internal/admin/v1`; `/api/v1/auth/refresh` dropped.
- [x] Aggregate ceiling added to `SyncBatchV2` (`app/sync_v2.py`), mirroring V1's budget.
- [x] `scripts/check_guard_prefixes.py` enumerates `app.routes` and fails if a declared
      guard prefix matches no live path; wired into the `unit` CI job as a lint step.
- **Tests:** guard lint ok (5 declared prefixes, all live); suite green at 293/1.
- **Done when:** every declared guard maps to a live route, and the V2 batch is rejected
      over the V1 budget. *(Both met.)*

### W2 — `VITR-V002` evidence header bound
- [x] `max_length=256`/`512` added to `X-Vitrial-Item-ID` / `X-Vitrial-Filename` in
      `app/main.py:257-258` and in the pinned contract `contracts/backend/v1/openapi.yaml`.
- **Commit:** `5f6178f`. **Tests:** contract validation passes; openapi/pack suites green.
- **Done when:** ✅ an over-long header is now a 422 before any body is read, and the pinned
      contract and the handler agree.

### W3 — `VITR-V003` XFF trust boundary
- [x] `deploy/Caddyfile` + `Caddyfile.smoke` now `header_up X-Forwarded-For {remote_host}`,
      overwriting any client-supplied value instead of appending to it.
- [x] Dockerfile documents why `--forwarded-allow-ips=*` is reachable only from the
      no-public-port Caddy hop.
- **Commit:** `d0e1d93`. **Tests:** deployment suite green.
- **Done when:** ✅ with Caddy overwriting XFF, `client_key_for`'s left-most entry is the
      real client IP, so 40 spoofed values can no longer bypass the limiter behind the
      documented deployment shape.
- [ ] Add an acceptance assertion so a mis-set trust proxy fails fast.

### W4 — `VITR-V004` reference GETs writing
- [x] `ensure_baseline_publications` now runs once per manifest (was: twice, via the two
      public helpers). `_list_publications` / `_current_publications` expose the pure query
      path; the public helpers still seed so first-read tests keep working.
- **Commit:** `33ccee4` (landed by the parallel BEAM pass). **Tests:** scrum21 postgres suites green.
- **Done when:** ✅ one manifest GET performs one seed check, not two.

### W5 — `VITR-V005` evidence safety scan O(N)
- [x] Containment filter `_evidence_reference_filter` pushes the lookup into PostgreSQL
      (`jsonb @>` against both `evidenceReferences` and `evidenceReferenceIDs`); delete and
      GC no longer materialize every measurement payload in Python.
- **Commit:** `30b29ab`. **Tests:** full suite green (311 passed).
- **Done when:** ✅ the Python-side O(all measurements) scan is gone. Residual: the column
      is `json`, not `jsonb`, so PostgreSQL still evaluates containment per row. A true
      indexed lookup needs a `jsonb` column + GIN index — a schema change on a
      client-pinned contract, deliberately left as a separate decision.

### W6 — `VITR-V006` rejection reasons
- [x] `app/rejection_codes.py`: closed `RejectionCode` vocabulary + `rejection_code(exc)`
      classifier; explicit per-site codes opt in via a `rejection_code` attribute.
- [x] `sync_service.py` emits the code instead of `type(exc).__name__`.
- [x] `tests/test_rejection_codes.py` pins that the code is stable across distinct messages.
- **Commit:** `257e725`. **Tests:** suite green (311 passed, 1 skipped).
- **Done when:** ✅ the sync outcome stream now carries aggregatable, refactor-stable codes.

### Smaller / hygiene
- [ ] Close issue #30's checkbox (MinIO→RustFS already landed); reconcile #2/#28 in GitHub.
- [ ] The four guard-prefix entries are a Rule-5 violation — after W1, add a lint rule so a
      matching-by-class failure fails, per `AGENTS.md` "measure, don't assert". (Done: W1 + guard lint)

## BEAM waves — `beam/apps/vitrial_sync`

Ordered for long sessions, same shape as the Python queue. Verified 2026-10-03 at
`342f9be`; the ordering argument is `beam/ARCHITECTURE.md` §3, which now carries the
same per-slice state table.

- [x] **4a — push steps 5–9.** Five named decisions (S-58 item-delete guard, S-59
      lifecycle, S-60 delivery authority + its four handoffs, S-61 generic
      authorization and ownership plan, S-62 evidence GC enqueue) as pure
      functions over the validated record, wired into `Admission.decide/2` as one
      ordered pipeline through `VitrialSync.Authority`. Each returns a token from
      the closed rejection vocabulary rather than an exception class name.
- [x] **4b — pull visibility resolution.** `VitrialSync.Prefetch`,
      branch-for-branch against `record_is_visible` and
      `delivery_execution_is_visible`, with the deliberate `deleted_at`
      asymmetry preserved in **both** directions.
- [ ] **4c — wire assembly.** Turn `Page` plus visibility results into the exact
      wire response. The last slice in wave 4 that can be built without a
      database, and the only thing left in the wave. **Not built; this run
      produced no files for it.**
- [x] **4d — the differential harness.** `342f9be`. `parity_oracle.py` AST-lifts
      the reference functions out of `app/` and executes them; `parity_test.exs`
      compares admission decisions, page wire sizes against real
      `SyncBatch.model_dump_json()` output, and payload decode; `drift_test.exs`
      re-reads the reference *source* and runs with or without an interpreter.
      +23 tests. `beam/Dockerfile.gate` is the other half: the pinned image plus
      `python3` and `pydantic`, so the oracle executes instead of skipping
      itself. Measured parity: admission 44 cases / 50 records, page wire sizes
      117 cases, payload decode 169 cases with exactly 13 documented divergences.
- [ ] **4e — nothing was built, and there is nothing on record to build.** Neither
      `beam/ARCHITECTURE.md` nor `beam/README.md` defines a 4e; the run labelled a
      wave 4e and it produced no files. Logged as unbuilt rather than described,
      because inventing a scope for it would be worse than admitting there isn't one.

### Open, and specific
- [ ] **Point `ci.yml`'s BEAM job at `beam/Dockerfile.gate`.** Today it mounts
      `$PWD/beam:/work` into the bare pinned image, so the oracle cannot run and
      the suite reports `532 passed, 23 skipped` with both skip reasons printed.
      The local gate is currently the only place the parity corpus executes.
      Verified here by running the CI job's exact container shape.
- [ ] **`BEAM_BASE_IMAGE_ONLY=1` does not force the bare image.**
      `scripts/beam_gate.sh` consults the variable only inside the
      build-on-demand block, and `IMAGE` is already defaulted to the gate image
      above it — so on a host where the gate image exists, the documented
      "prove the fallback path" switch keeps using the gate image. Measured:
      it still reported `555 passed`. `BEAM_IMAGE=<base>` is the override that
      works, and is what the 549/6-skipped figure above was measured through.
- [ ] **`beam/apps/PLANNED_APPS.txt`'s `vitrial_ownership` line is stale.** It
      still declares that `app/ownership.py:726` "carries a duplicate
      delivery-policy block that is unreachable … (VITR-V006, filed)". That block
      was deleted by the `VITR-V009` fix; `app/ownership.py:835-846` now
      documents its absence as deliberate, and the defect it cited was `VITR-V009`,
      not `VITR-V006`. `check_beam_scaffold.py` passes either way — it checks the
      line's *presence*, not its truth — so nothing catches this class of rot.

## Branch / pipeline hygiene (established 2026-10-02)

The repo is integrated by **squash merge**, so three standard git checks all report false
positives here — see the guardrails skill §9. Two repeatable gates now exist so the next
agent does not re-derive this:

- [x] `scripts/audit_branch_staleness.py` + `make branches` — content-based staleness audit.
      Compares each touched file against `main`; only a *newer* branch that adds content
      `main` lacks (or a file absent from `main`) is REAL-UNMERGED. Current verdict:
      **109 SAFE-DELETE, 5 STALE-SNAPSHOT, 0 REAL-UNMERGED**.
- [x] `scripts/ubs_gate.sh` + `ubs-baseline.json` + `.github/workflows/ubs.yml` + `make ubs` —
      UBS delta gate. Fails on any NEW critical/warning vs the committed floor. The floor is
      documented false positives, never a silent rebaseline.
- [x] `make check` now runs `check_guard_prefixes.py` alongside the contract validator.

Note: the audit found 5 STALE-SNAPSHOT branches (mostly `payment-settlement-contract` and
an older `sync-pull-bounds`), all older than `main` and holding no work it lacks. Cleaning
them is optional hygiene, not a blocker.

## W7 — production-foundation boxes not yet attempted

Per #28, none of these are defects — they are unshipped commitments:
- [x] **Graceful shutdown/drain** — `app/drain.py` + lifespan; `/ready` flips 503 on SIGTERM
      while `/health` stays 200; `--timeout-graceful-shutdown 45` in the Dockerfile and
      `stop_grace_period: 60s` in production compose (must exceed it, or Docker SIGKILLs
      mid-upload); `deploy.sh` awaits the replaced container and fails on non-zero exit.
      Proven by `tests/test_graceful_drain.py`, including two planted-defect controls.
      Commit `04a612d`. This was the one item whose absence was a real defect.
- [x] **Metrics endpoint** — `app/metrics.py` (hand-rolled Prometheus exposition, no new
      dependency: the image is security-scanned and carries a live Dependabot advisory, so
      a dep for ~120 lines is a poor trade). `GET /metrics` is `include_in_schema=False`
      and gated on `require_admin_key`; route names + status classes + rejection codes are
      reconnaissance even without a body. Request rate/latency/status by **route template**,
      sync outcomes keyed on the stable W6 rejection codes, pool saturation (honest under
      NullPool: reports 0, not the configured size), drain state, series-drop counter.
      Commit `91434e0`, 22 tests, two planted-defect controls confirmed red.
- [x] **Object-store + GC histograms wired** — `beab668`. A declared-but-never-written
      metric is worse than a missing one: it renders a live series of zeros, so a
      dashboard reads "object-store latency: 0" and the operator concludes the store is
      instant. `put_verified` is timed in a `finally` at the store boundary (not around
      the handler — this is a 100 MiB streamed write, and handler latency folds in
      authorization and DB work). GC counts once per run from the returned totals so it
      cannot drift from `GCResult`; `dry_run` is labelled apart from real reclamation.
      Remaining: pull page-size distribution is still unrecorded.
- [x] **Dependabot alert 1** — `32d2a30`. GHSA-6w46-j5rx-g56g (medium), pytest tmpdir
      `/tmp/pytest-of-{user}` handling, fixed in 9.0.3. `pytest-asyncio` 0.26 hard-pins
      `pytest<9`, so both had to move together — bumping pytest alone gives an
      unsatisfiable environment, not a partial one. Dev-only: pytest is not a production
      dependency and `test_production_dependencies.py` still passes. Verified twice on an
      isolated venv: 339 passed, 1 skipped both times, the second from a clean-room
      install of the edited lockfile (the exact CI path). No new transitive deps.
- [x] **W3 follow-up: trust-proxy acceptance assertion** — `94eaef8`. The XFF overwrite
      shipped in W3 but nothing tested it, so it could be reverted or re-spelled with no
      test going red while the rate limiter's correctness depends on it: `client_key_for`
      reads the LEFT-MOST XFF entry, which under Caddy's default append semantics is the
      attacker's, making the throttle a no-op. Three assertions — both Caddyfiles carry
      the overwrite (the smoke file is the only one CI boots), the client-header
      placeable `{http.request.header.X-Forwarded-For}` is rejected explicitly (it reads
      like a fix and is the exact reverse), and the preference order inside
      `client_key_for` is pinned so the Caddy control cannot quietly become decorative.
      All three verified against planted defects.
- [x] **Reproducible install in the production image** — `150c7ac`. The image ran
      `pip install .`, i.e. it resolved against the *ranges* in `pyproject.toml`. The
      committed lockfile was decorative for the artifact that ships: the image could
      resolve to versions CI never tested, and a range can move onto a yanked or
      newly-malicious release with no commit at all. New generated
      `requirements-runtime-lock.txt`, derived by **resolving** the runtime closure in a
      clean venv, not by subtracting dev packages from the combined lock — the two
      closures overlap (anyio/certifi/idna/packaging come from both httpx and
      fastapi/botocore), so a denylist silently drops a package the image needs and
      fails at container start. Dockerfile installs the lock with `--no-deps .` so pip
      cannot re-resolve over the pins. Verified by mirroring the Dockerfile RUN into a
      fresh venv and booting it (`/health` 200, `/metrics` 404 gated, and
      `starlette.testclient` correctly refusing to import for want of httpx).
- [x] **`VITR-V008` — baseline seed race → 500** (landed in the 2026-10-03 tracker+seed commit; commit-worthy
      on its own). All four reference-data GET routes call `ensure_baseline_publications`,
      whose check-then-insert had no lock, no `ON CONFLICT` and no retry. Under the engine's
      default READ COMMITTED (`app/db.py:61`, no `isolation_level`), two concurrent first
      reads of a newly-onboarded org both see the rows missing; the loser's `INSERT` violates
      `reference_publications_pkey` at the `flush` in `_insert_publication`, and nothing in
      `app/` catches `IntegrityError`, so the request 500'd. `app/transient_retry.py:37-39`
      deliberately does not retry 23505 — correct for the push path, wrong shape here.
      Fixed by splitting the seed body into `_seed_baseline_publications` and catching
      `IntegrityError` around it: roll back (discarding the loser's own uncommitted rows) and
      re-run the check. Safe because PostgreSQL blocks a duplicate inserter on the unique index
      until the other transaction resolves, so a 23505 is only ever reported once the winner
      has committed — the re-check then sees the rows and inserts nothing. One attempt of
      retry, no backoff: a seed can be lost once, and only once.
      **Test:** `tests/test_reference_baseline_seed_concurrency_postgres.py` — the four GET
      helpers in four sessions at once, with `_insert_publication` wrapped in a barrier so all
      four are provably past the check before any of them inserts. Without that gate the
      SELECT→flush window is microseconds and the test would pass against the broken code most
      of the time. Second test pins that the surviving rows are the winner's: one row per
      bundled publication, digests equal to `publication_sha256`.
      **Not covered:** `VITR-V004`'s actual complaint — a GET that writes is still a GET that
      writes. Moving the seed to org creation is a design decision (what happens to orgs that
      predate it), not a drive-by.
- [x] **`VITR-V010` — the GC queue backs off** (landed in the 2026-10-03 tracker+seed commit; commit-worthy
      on its own: it makes an existing column and an existing counter load-bearing, and
      unblocks the head of the queue).
      `not_before` is the collector's *only* selection predicate (`app/evidence_gc.py:110`),
      and both retain paths committed the row with the `not_before` it was queued with —
      already in the past by the time anything failed. A still-referenced skip and a
      `StorageError` therefore both reselected the row on the very next run, forever,
      while `attempts`/`last_error` were written and read by nothing. Under
      `order_by(id)` with the shipped limit of 100 (`scripts/gc_evidence.py`), ~100
      permanently blocked rows permanently occupy the head of the queue.
      Fixed with `_reschedule(row, error=..., now=cutoff)` at both sites: increment
      `attempts`, record `last_error`, push `not_before` out by
      `min(300 * 2**(attempts-1), 3600)`. The exponent is clamped as well as the result,
      so a long-lived row does not compute a bignum. `attempts` now has a reader.
      **Tests** in `tests/test_evidence_gc_postgres.py` (the file whose docstring had
      recorded this as "the open half of VITR-V010, deliberately not asserted here"):
      the failure test now asserts `not_before > now` and then runs the collector again
      one second later and asserts it selects *nothing* — the property, not the counter;
      a new test covers the second retain path (a live measurement listing the document
      in `evidenceReferences`), which had the identical defect; and a unit test pins
      the delay sequence `300, 600, 1200, 2400, 3600, 3600`, i.e. doubling *and* the cap.
      **Not done, deliberately:** a give-up after N attempts. It means never reclaiming
      that object, which is an operator decision, not a drive-by. Also still open and
      adjacent: `evidence_gc_backlog` is registered at `app/metrics.py:485` and never set,
      so the backlog is invisible — a one-line counter that belongs with whatever
      give-up policy is chosen.
      **Not run:** no test in this file was executed (see above — no sqlalchemy, no
      reachable PostgreSQL). The delay sequence was checked by replaying the formula
      standalone: `[300, 600, 1200, 2400, 3600, 3600]`, and `attempts=10000 -> 3600`.
- [x] **`VITR-V011` — the collector holds its locks for the whole run** (landed in the 2026-10-03 tracker+seed commit.
      time of writing; commit-worthy on its own: the fix makes two shipped comments true
      that the ordering had made false, and it needs no schema or contract change).
      `collect_due_evidence_gc` selected its rows once with `FOR UPDATE SKIP LOCKED` and
      then `commit()`ed *inside* the row loop — four times. A commit ends the transaction
      and releases the locks on every row the run had selected but not yet reached, so an
      overlapping run could take one of them, delete it and commit, and this run's later
      write to it matched zero rows: `StaleDataError` on the UPDATE path (`attempts += 1`
      has no `version_id_col`, so the rowcount check raises unconditionally) escaping
      the collector and aborting the whole run, or a silently double-counted deletion on
      the DELETE path. `scripts/run_evidence_gc.sh:51` and `deploy/README.md:190` both
      claimed SKIP LOCKED made overlapping runs safe; for as long as the per-row commit
      stood, that was false.
      Fixed by making the run one transaction: a `SAVEPOINT` per row inside
      `async with db.begin_nested()`, one `commit()` after the loop. The savepoint is
      what keeps VITR-V014's recovery working — a failing object store rolls back only
      its own row, and the locks and every row already processed survive. `row.id` is
      still read before any rollback, since a savepoint rollback expires instances in
      its scope exactly as a full one did.
      **Tests:** `tests/test_evidence_gc_postgres.py` now has the two-collector test this
      file never had. The first run is parked on its *second* row's store delete — the
      exact instant the defect lived, after a per-row commit had released the lock on the
      row not yet reached — and a second collector must then find nothing (`GCResult()`),
      while the first reclaims both objects once each. Only the second store call blocks,
      so the test terminates under the old code too (where the second run reclaims one
      object and the assertion fails) instead of deadlocking. A second, dependency-free
      AST test asserts no `commit()` inside the collector's loop and exactly one in the
      function; run standalone against `git show HEAD:app/evidence_gc.py` it reports 4
      commits inside the loop vs 0 now.
      **Trade-off, stated rather than hidden:** the run now holds its row locks and its
      organization locks until the end rather than per row, so a slow object store holds
      them longer and an upload to one of those organizations waits longer. The
      alternative — one transaction per row — restores that at the cost of re-selecting
      per row and a `dry_run` that never reschedules; neither is free. Nothing in the
      service takes organization locks in a different order within one transaction, so
      the wider hold cannot deadlock against sync push or `put_blob`.
- [x] **`VITR-V013` — a published upload is not failed by its own cleanup** (landed in the 2026-10-03 tracker+seed commit.
      time of writing; commit-worthy on its own: it fixes a production-only path that had
      no coverage at all, and it does so without needing S3 to prove it).
      `S3ObjectStore.put_verified` stages to a multipart upload, verifies the digest,
      publishes with `copy_object` to the final content-addressed key, and then deletes
      the staging copy. That last delete used to fall into the broad `except Exception`,
      which — with `upload_id` already cleared — retried the *same* failing delete inside a
      bare `except: pass` and re-raised `StorageError`. `put_blob` converts that to a 503
      at `app/evidence.py:160`, thrown **before** the `EvidenceBlob` row is added and
      **before** any GC row is queued. The client is told the upload failed while the
      object sits, durable and correct, at its final key with nothing referencing it and
      nothing that will ever reclaim it.
      Fixed by making the post-publish staging delete best-effort: it logs
      `storage.staging_delete_failed` at WARNING (with the staging key, a random uuid
      under `.staging/` that carries no org, document or content identity, and is the one
      datum needed to reap the orphan) and returns normally. The swallow is deliberately
      narrow — it wraps only that delete, after the publish, so pre-publish failures and
      digest mismatches still raise.
      **Tests:** `tests/test_object_storage.py` gains a duck-typed `FakeS3Client` (the
      store only reaches boto3 through `asyncio.to_thread(self.client.<op>)`) and three
      tests that run in the **default** suite — the old coverage of this store lived
      entirely behind `S3_INTEGRATION=1` and Docker, which is why the defect survived:
      a failed staging delete still returns the correct `StoredObject` with the object
      published and exactly one delete attempted (the swallowed retry was the tell); a
      failed `copy_object` still raises `StorageError` and publishes nothing; a digest
      mismatch still never publishes. The first test fails on the pre-fix code, where the
      same injected failure raises.
      **Residual, unchanged and outside this fix:** a staging copy that survives is still
      only reapable by a bucket lifecycle rule, and no such rule is configured in this
      repo — nothing in `app/`, `scripts/` or `deploy/` reconciles `.staging/` orphans.
      The log line is what makes them visible.
- [x] **`VITR-V009` — one authority gate for delivery** (landed in the 2026-10-03 tracker+seed commit;
      commit-worthy on its own). Deleted `authorize_record`'s `delivery_execution`
      branch and its now-orphaned `_current_quotation_id`, and deleted
      `tests/test_delivery_execution_ownership.py`. **The point is not the deletion.**
      With the branch gone, `delivery_execution` matches no `if entity_type == ...`
      arm and reaches the closing `raise AuthorizationRejected`, so the generic gate
      now *fails closed* for delivery. Before this, a refactor that broke the dispatch
      in `sync_service` would have let delivery records be authorized by the drifted
      copy — no `delivery.manage` check on tombstones, tombstones of known executions
      permitted. After it, the same break denies them.
      **Tests:** `tests/test_delivery_authorization_dispatch.py` — (1) `authorize_record`
      rejects `delivery_execution`, and the session raises on any attribute access, so
      passing proves nothing was consulted; (2) `authorize_record` holds no
      `delivery_execution` string literal at all, so the second gate cannot be
      reintroduced quietly; (3) an AST check that every `authorize_record` call in
      `sync_service.py` sits in the `else`-arm of a `DELIVERY_ENTITY_TYPE` branch.
      Test (2) was run against the pre-fix `ownership.py` (`git show HEAD:app/ownership.py`)
      and does fail there: delivery literal present `True` vs `False` after. Tests (1)
      and (3) need pytest and a database; neither was run here.
      **Not covered here:** the live gate's own rules stay covered by
      `tests/test_delivery_execution_postgres.py`, which drives it through the push
      path (`POSTGRES_INTEGRATION=1`). Every rule the deleted tests asserted —
      capability, approved quotation, approval event, canonical active quotation,
      Customer match, orphan refusal, payload identity, tombstone — is enforced there
      in a stricter form; that file was not run in this session.
- [ ] Session renewal story (today: fixed bearer TTL + manual revoke only). **Product
      decision, not engineering** — asked, awaiting an answer on whether sessions should
      slide-refresh or stay fixed-TTL with manual revoke.
- [ ] `VITR-V005` follow-up: `jsonb` + GIN index migration (schema change on a pinned contract).

> The two rows that used to sit here — "Reproducible install in the production image
> (lockfile committed; image still installs by range)" and "W3 follow-up: acceptance
> assertion so a mis-set trust proxy fails fast" — contradicted the checked rows
> above them and were removed rather than left to disagree. Both were already done:
> `Dockerfile:31-32` installs `requirements-runtime-lock.txt` and then
> `--no-deps .`, and `94eaef8` is the trust-proxy assertion. A tracker that carries
> the same item as both open and closed is worse than one that carries it once,
> because a reader cannot tell which one is the lie.

## UBS loop status — `review/vitrial-api-hardening`

`ubs . --only=python` (with `.ubsignore` scoping out `tests/`, `requirements-lock.txt`,
caches, `migrations/versions/`):

| Pass | files | critical | warning | info |
|---|---|---|---|---|
| before fixes (all files) | 109 | 48 | 65 | 693 |
| after UBS fixes, full scan | 109 | 42 | 65 | 688 |
| after `.ubsignore` scope | 44 | 16 | 3 | 305 |

Fixed: 6 `security-assert` criticals in `ownership.py` (asserts → explicit
`AuthorizationRejected`); `py.async.blocking-call` on `LocalObjectStore.stream` (chunks via
`asyncio.to_thread`).

Residual criticals are **validated false positives** for this codebase, not fixable without
distorting the production path: `python.ctcompare.secret_eq` (`access_token_hash ==` is a DB
lookup, not an in-process comparison; `authorization_revision !=` is an int),
`py.security.ldap-injection` on `db.add(...)` (not a directory sink),
`py.security.header-injection` on `X-Request-ID`/`X-Content-SHA256` (both are UUID-/hex-validated
before use), and the lock-file `==` float comparisons. The "next session" page for keeping this
loop alive: every new class of warning from a silent assert or a blocking call inside async gets a
real fix; noisy-rules go in `.ubsignore` or the `skip=` list, never a code workaround.

## What is done that is spec'd in #28 (leave checked only if true)

This section mirrors the reconciled boxes above; do not remove them. See the per-section
`[x]/[~]` list for the authoritative state.
