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
> (HEAD `77c83c2`, 2026-10-03). Only check a box when a commit in `git log` is
> evidence of it. Use `[~]` for the part-that-works, not `[x]`.

> **Verification status of the rows below — INDEPENDENTLY RE-EXECUTED 2026-10-04.**
> The claim on `55876bd` was "executed evidence", but it was CI, not a Python run, and
> the workspace had no interpreter. There **is** a working one
> (`~/.venvs/vitrial-api`, PostgreSQL at `~/.pgdata/vitrial` on 5435). Re-run here at
> `77c83c2`: **`415 passed, 1 skipped`**, UBS delta gate PASS, contract validation PASS,
> guard-prefix lint ok. CI on `77c83c2` agrees: Unit + contract, PostgreSQL + S3
> integration, Security, Acceptance deployment topology and Container build all
> **success**. The one red lane is **BEAM umbrella (compile + format + test)**, which is
> the in-flight wave-5/6 `Pgconn` work and not a Python regression.
>
> Each `Fixed` row was then checked individually rather than trusted from the suite:
> - `-008` catch/rollback/bounded-retry is correct, and has a dedicated concurrency test.
> - `-009` delivery **fails closed** — `ownership.py:847` raises
>   `f"canonical authorization is not implemented for {entity_type}"` dynamically, so the
>   generic gate refuses a delivery record before touching the database.
> - `-010` backoff **measured**: three consecutive failures push `not_before` by
>   300 s → 600 s → 1200 s with `attempts` incrementing.
> - `-011` the SAVEPOINT-per-row design is sound, and its two tests are load-bearing:
>   re-planting the per-row `commit()` turns **7 tests red**, including the
>   two-sessions overlapping-collector behavioural test.
> - `-013` the post-publish staging delete is best-effort and logs, so a published upload
>   is never escalated to a 503.
> - `-004` correctly left **not fixed** — `33ccee4` only splits out `_list_publications`;
>   the four GET helpers still seed and commit.
>
> **The BEAM half moved again (waves 4c/4d/4e, then 5, then 6) and every BEAM number
> below was re-measured here at the landed tree.** `./scripts/beam_gate.sh` → PASS,
> exit 0: `vitrial_web` `66 passed`, `vitrial_sync` `657 passed (3 doctests, 654
> tests), 20 skipped` — **723 passed, 20 skipped** in total, where the 2026-10-03
> morning state was 555. All 20 skips are the wave-5 live-PostgreSQL suites without a
> server; each prints its reason by name. Guards re-run here, all exit 0:
> `check_beam_scaffold.py` (8 apps, **2 built**, 6 declared), `check_beam_orphans.py`
> (136 sources, 88 modules, 87 reachable, no dead modules). The BEAM CI job now runs
> the oracle and fails on any `SKIPPED` (`e279fb2`) — the "does not run in CI yet"
> note this header used to carry is resolved, not forgotten.
> Full state and next steps: `memory-seeds/vitrial-api-state-2026-10-03.md` (revised
> in place by the records pass, which also committed the wave-5 and wave-6 working
> trees that waves 4c/4d/4e had left behind them).

> **A green gate is a statement about the gate, not about the port.** The pinned
> `hexpm/elixir` image has no `python3`, so before `342f9be` the differential
> harness skipped itself and `mix test` reported `532 passed / 23 skipped` — all
> green, with the oracle that proves the port correct never executed. That lesson
> produced the two fixes this header used to carry as open items, both now landed:
> `beam/Dockerfile.gate` gives the container the `python3` (measured: default gate
> **723 passed / 20 skipped** on the landed tree; bare image via
> `BEAM_BASE_IMAGE_ONLY=1` **715 passed / 28 skipped** — exactly the eight parity
> blocks extra, both skip notices printed), and `e279fb2` pointed `ci.yml`'s BEAM
> job at that image with a repository-root mount and a **fail-on-`SKIPPED`** check,
> retiring the old `beam/`-only-mount lane (last measured there: 556 passed /
> 31 skipped). The remaining skip class is honest by construction: the wave-5
> live-database suites decline, by name, when no PostgreSQL is reachable — and the
> reason they decline in this tree (`transport raised exit: :badarg`, the wave-5
> wire fixes living only in an ephemeral working copy) is the first item under
> **Open, and specific** below.
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

- [x] Define a hard **aggregate** sync request ceiling enforced while reading the body. V1 and V2 both: `MAX_SYNC_V1_BATCH_PAYLOAD_BYTES` and the `SyncBatchV2` aggregate validator, plus a 4 MiB `RequestSizeLimitMiddleware` guard on BOTH push paths (`/api/v2/sync/push` added by W1 `18a4198`; the old "V2 does not — see VITR-V001" note was stale). Every value is listed in `docs/contract/LIMITS.md` and checked.
- [x] Hard sync response/page byte budget, deterministic by record count **and** encoded bytes
  (`pull_page_accepts`, `MAX_SYNC_PULL_RESPONSE_BYTES`; `sync_service.py`).
- [x] Evidence uploads on a separate streaming ceiling (`evidence_max_bytes`).
- [x] Content-Length **and** chunked over-limit tests exist for V1 (`test_request_size.py`). Now also: **near-limit** — a record exactly at `MAX_SYNC_V1_WIRE_PAYLOAD_BYTES` is accepted and one byte over is refused (off-by-one control confirmed red) — and **compressed transport** — a gzip body that would inflate to 64 MiB is refused at parse time (400) in milliseconds, i.e. never inflated, so no decompression bomb past the wire-byte guard (`tests/test_contract_limits_postgres.py`).
- [~] Edge/application rate limits. Pairing **and the admin control plane** (`/internal/admin/v1/*`, incl. the nested `pairing-grants`) are throttled, one budget per (surface, client), refused before the handler (commit "Rate-limit the admin control plane, with a separate budget per surface"). Still application-level and in-process; no edge (Caddy) limit, and per-instance only.
  Sync/evidence/admin are unthrottled; pairing limiter is XFF-evadable (`VITR-V003`).
- [x] Load-test rejection paths so abusive traffic fails cheaply — measured, not load-tested: `tests/test_rejection_cost_postgres.py` counts engine connection checkouts per abusive request shape. No/non-bearer auth, oversized body, throttled pairing/admin: **0** connections. Unknown token: **exactly 1** (the irreducible lookup), pinned so it cannot grow. Two controls confirmed red.

### P0/P1 — production auth/session model

- [~] Separate admin control plane from public listener. Admin is `include_in_schema=False`, disabled unless `ADMIN_API_KEY_HASH` is set, and now **rate-limited** (own budget; so is `/metrics`, which checks the same key). It still shares the public listener, and that is a **deployment decision, not a code fix**: operators reach it through the public HTTPS edge today (README: "send the original key at provisioning time … over HTTPS"; the acceptance bootstrap script does too), so blocking `/internal/*` at Caddy would break provisioning. Options for the owner: a Caddy source-IP allow-list on `/internal/*`, or a second internal-only listener with provisioning via `docker compose exec`.
- [x] Pairing/auth attempt throttling. Implemented on `/api/v1/auth/pair` — but weakened by `VITR-V003`.
- [ ] Production session renewal; today a fixed bearer TTL + revoke-only.
- [ ] Device-bound sessions (copied bearer stays useful without proof-of-possession).
- [x] Explicit revocation, membership-removal, authorization-revision behaviour
  (revoke + revoke-by-org; every `auth/me` and session yields the org's current revision).
- [x] One-time grant exchange outcomes indistinguishable to unauthenticated callers.

### P1 — sync scalability & concurrency

- [x] Benchmark org-wide `FOR UPDATE` serialization (`#59`) — measured, cost noted.
- [x] Revision allocator — **decided: kept.** `max(server_revision)+1` is correct because every push holds the organization row lock, which serializes allocation; the concurrency suite (`test_sync_concurrency.py`) and the serialization-retry tests pin it. A sequence would only be needed if allocation ever moved outside that lock.
- [x] Remove/contain pull N+1 — `pull_prefetch.py` batched it; measured `#61` (604 → 4).
- [x] Configure DB pool explicitly (`#42`).
- [x] Deadlock/serialization retry policy (`#46/#49/#56`).
- [x] Concurrency tests for duplicate mutation IDs (`test_sync_concurrency.py`), tombstone resurrection (`#50`), cross-device lifecycle, and now **evidence-replace contention**: two real concurrent `put_blob` replaces on separate sessions leave the canonical pointer naming an object that exists, no orphan (every written object canonical or queued), and the live object never queued (`tests/test_evidence_replace_contention_postgres.py`; control — dropping the replace GC queueing — names the orphans; 5/5 stable).

### P1 — reproducible deploy & supply chain

- [x] Reproducible Python lock. `requirements-lock.txt` (what CI tests) is the version source of truth; `requirements-runtime-lock.txt` (what the image installs) is generated from it as a pip constraint, so they cannot disagree (`VITR-V017`, `4ec9341`). Offline `--check`, a `--no-deps` + `pip check` closure step in CI, and Dependabot configured to propose only mergeable bumps (`VITR-V018`).
- [x] Pin runtime images by digest. Production runs only `API_IMAGE` and `CADDY_IMAGE`, and `validate_deployment.py` refuses either without `@sha256:` (tested). Postgres/S3 images exist only in the ACCEPTANCE topology, which pins by tag deliberately and refuses `:latest` (now tested). The old "Caddy, Postgres, S3 floating" note was stale; it also hid an unreachable `mode == "production"` check nested inside the acceptance-only branch, removed in the same pass. `alpine:3.20` (acceptance-only chown helper) is the one literal tag left.
- [x] Image/package vulnerability scanning and SBOM in candidate CI. The image scan ran with `exit-code: '0'` — a fixable HIGH/CRITICAL in the shipped image was printed and passed. Now enforced (`exit-code: '1'`, `ignore-unfixed`), flipped after measuring 0 such findings across all 36 image targets. A CycloneDX SBOM of the image is generated and uploaded (90 days). Verified green in CI on `f6440fe`.
- [x] Validate production settings on startup/preflight (`validate_deployment.py`, `probe_production_preflight.py`, `deploy/README.md`).
- [~] Migration compatibility + rollback rehearsal. CI proves `upgrade → downgrade base → upgrade`; production rollback intentionally does **not** auto-downgrade DB (documented).

### P1 — resilience & operations

- [x] Metrics for request rate/latency/status, auth failures, sync outcome, DB pool, S3 latency, GC backlog, page sizes — `app/metrics.py`, `GET /metrics` (admin-gated, out of the pinned schema). Labels on the matched route template with a series cap; sync outcomes on the stable `VITR-V006` vocabulary; pull page size plus a truncation counter.
- [x] Trace/correlation propagation. A valid W3C `traceparent` joins the caller's trace: `traceID`/`parentSpanID` from the header, a fresh `spanID` per request, all on every JSON log line. Strictly parsed (version 00, lowercase hex, non-zero ids) — anything else starts a new trace and is never reflected, matching `X-Request-ID`. Not echoed as a response header, because the V1 response shape is pinned. Outbound propagation to S3/PostgreSQL is not done: neither consumes trace context today.
- [x] SLOs/alerts for availability, p95, sync failure rate, dependency readiness. `deploy/prometheus/alerts.yml`, 12 rules (<1% 5xx, p95 <1 s per route excl. evidence uploads, sync internal errors / rejection share / pull truncation, auth spikes, admin-key guessing, pool saturation, slow object store, stuck drain, overdue GC). Checked by `promtool` in CI **and** by `tests/test_alert_rules.py` against the live registry (metric, label, and closed-value existence). Readiness is a probe, not a metric, so it is consumed by the edge rather than alerted on.
- [x] Graceful shutdown/drain test with in-flight sync and evidence streams — `app/drain.py`, `/ready` 503 on SIGTERM while `/health` stays 200, 45s uvicorn budget inside a 60s `stop_grace_period`, `deploy.sh` awaits the replaced container and fails on non-zero exit.
- [ ] PostgreSQL backup/PITR and restore drill.
- [~] S3 versioning/retention/restore policy and orphan/GC reconciliation runbook — GC exists and is scheduled (`deploy/gc-evidence.timer`), but no documented restore policy.

### P1 — contract discipline

- [ ] Make V2 the production sync contract target with an explicit V1 deprecation window. Both are served; no window defined.
- [x] Aggregate-size, schema-version, pagination semantics in the contract docs. `docs/contract/LIMITS.md` lists every enforced ceiling, its value, its source constant and what exceeding it returns, including pull paging (which ends pages, never errors). Values are checked against the constants (`tests/test_contract_limits_doc.py`, incl. that every size-limited path is documented and every source-citing row is parsed); responses are **measured** against the real app (`tests/test_contract_limits_postgres.py`). Deliberately a reference beside the pinned contract, not an edit to `contracts/backend/v1/`.
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
| `VITR-V004` | LOW | Reference-data GET endpoints write + commit (`ensure_baseline_publications`) | **Reduced, not fixed** (`03e10d9`) — `bootstrap_identity` now seeds the baselines at organization creation, so for every NEW organization the first writer is the write path and the GET seed inserts nothing. The lazy seed is **kept on purpose**: removing it would change what a GET returns for an organization that exists but was never bootstrapped through this path, which is client-visible on a pinned contract. All four GET helpers still call `ensure_baseline_publications` and can still write for such an organization. Closing it properly needs a backfill migration for existing organizations first. **Backfill tool now exists**: `scripts/backfill_baseline_publications.py` (dry run by default, idempotent, re-measures after `--execute`, exits 1 while anything is incomplete; operator steps in `deploy/README.md`). Remaining to close: run it against production, record `incomplete=0`, then delete the lazy seed from the four GET helpers. |
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

| `VITR-V016` | MED | BEAM `VitrialSync.Auth` answers differently from `app/auth.py::current_principal` on three refusals (all still 401) | **Open — BEAM-side, recorded not fixed.** (1) **Unknown token**: Python `auth.py:48` is `if not auth_session or …` → `"session expired or revoked"`; BEAM `auth.ex:182` (`fetch/2`, `{:ok, nil}`) → `:authentication_required`. Python deliberately makes "no such token" indistinguishable from expired/revoked (pinned by `test_session_lifecycle_postgres.py::test_an_unknown_token_is_rejected_without_touching_membership`); the BEAM split is a token-existence oracle, and `auth_test.exs:244` **pins the divergent answer** as intended. (2) **Missing organization**: Python `auth.py:53` → `"membership inactive"`; BEAM `auth.ex:108` uses `fetch/2` → `:authentication_required`. (3) **Wire text**: `handlers.ex:198` renders any other atom verbatim, so the body is `{"detail":"session_expired_or_revoked"}` / `"membership_inactive"` / `"session_membership_mismatch"` (underscores) where Python sends spaces. No endpoint test asserts auth refusal text, so (3) is unpinned on both sides. Also stale: `auth.ex:192-194` says the missing-membership refusal is "403"; `push_service.ex:300-303` correctly maps all four to 401. Found 2026-10-07 by read-only review of `e88b5e2`/`f5e8f61`. |
| `VITR-V017` | **HIGH** | The image and CI installed **different** library versions, and the runtime-lock CI gate was non-deterministic | **Fixed** (`4ec9341`). The runtime closure is now resolved with `requirements-lock.txt` as a pip **constraints** file, so resolution decides membership and the combined lock decides versions; regenerated, the image went back to the tested `sqlalchemy 2.1.1`, `boto3`/`botocore 1.43.107`, `markupsafe 3.0.3`. `--check` is **offline**: each runtime pin equals the combined pin, every declared runtime dependency is pinned, no dev tooling ships. A separate CI step proves closure the way the Dockerfile installs (`--no-deps` + `pip check`). The unit suite runs `check()` too, and an AST test pins that `check()` never calls the resolver. Controls: a drifted pin and a resolver call each turn a test red. `main` CI back to 6/6 green on `4ec9341`. Then `9d7d6d7` landed the sqlalchemy 2.1.2 patch through the fixed path — and the offline gate caught the half-done state first (`image ships 2.1.1 but CI tests 2.1.2`). |
| `VITR-V018` | MED | Dependabot pip config produced PRs that could not merge | **Fixed** (`e248d9c`). `versioning-strategy: increase-if-necessary` stops patch bumps rewriting `pyproject.toml` floors; `pydantic`/`pydantic-core` and `boto3`/`botocore`/`s3transfer` share groups listed **before** the catch-all `patch`/`minor` groups (Dependabot assigns to the first match, so order is the property). Pinned by `tests/test_dependabot_config.py`. PRs #20 and #21 closed with explanations. |

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
- [x] Add an acceptance assertion so a mis-set trust proxy fails fast. — `94eaef8`: both Caddyfiles assert the XFF overwrite, the client-header placeable is rejected, and `client_key_for`'s preference order is pinned; three planted defects confirmed red.

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
- [x] Close issue #30 / reconcile #2/#28 — **not applicable on this repo.** Those issues live on the retired `scottjoyner/vitrial-api` (push denied to this account); `astrix-group-accounts/server-api` has no GitHub issues, and this tracker is the source of truth.
- [x] The four guard-prefix entries are a Rule-5 violation (closed by W1 `18a4198`; `scripts/check_guard_prefixes.py` runs in CI) — after W1, add a lint rule so a
      matching-by-class failure fails, per `AGENTS.md` "measure, don't assert". (Done: W1 + guard lint)

## BEAM waves — the umbrella

Ordered for long sessions, same shape as the Python queue. Re-verified 2026-10-03 at
`77c83c2` plus the working tree that the records pass committed; the ordering argument
is `beam/ARCHITECTURE.md` §3, which carries the same per-slice state table measured
on the landed tree.

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
- [x] **4c — the push batch walk.** Built — not the pull-wire assembly this row
      once described, and the row is corrected rather than left to imply a
      missing slice. `VitrialSync.Batch` walks one push batch in `EntityOrder`
      order: one `Admission.decide/6` per record against a WORKING scope taken
      from the principal at entry and widened after every accepted generic
      customer/project create (`app/ownership.py:865`/`:884`), commit visibility
      threaded through `%Batch.Store{}`, outcomes keyed by ORIGINAL index, and
      `%Batch.Effects{}` carrying every database effect for the caller's one
      transaction; `finish/2` builds the `SyncResult` wire response.
      Committed across `a2413ae` (a concurrent agent's commit of in-flight work)
      and `9aa3582`. The parity oracle gained a 7-case batch section (including
      the intra-batch A-stale-under-`m1`/B-reuses-`m1` case); 6 drift assertions
      pin the reference's batch-assembly lines. Found and fixed while building it:
      the **twelfth execution-only defect**, `ownership.ex`'s item-child arm firing
      `absent_canonical` on `generic_exists` alone where the reference guards with
      `canonical is None AND generic_entity_exists` (`app/ownership.py:597-598`)
      — the port had refused every legitimate update of an existing item child.
      One reference step is deliberately absent: `validate_assembly_contract_mutation`
      is VITR-V015's own vertical (row above), fenced in `batch.ex`'s step table.
- [x] **4d — the differential harness.** `342f9be`. `parity_oracle.py` AST-lifts
      the reference functions out of `app/` and executes them; `parity_test.exs`
      compares admission decisions, page wire sizes against real
      `SyncBatch.model_dump_json()` output, and payload decode; `drift_test.exs`
      re-reads the reference *source* and runs with or without an interpreter.
      `beam/Dockerfile.gate` is the other half: the pinned image plus
      `python3` and `pydantic`, so the oracle executes instead of skipping
      itself. Measured parity (re-measured, never restated): admission 44 cases /
      50 records; page wire sizes 117 cases, all three ceilings reached by a named
      case; payload decode 169 cases with exactly 13 documented divergences; batch
      7 cases. The harness also caught a **reference-side** defect: non-UTF-8
      `SyncRecord.payload` (typed `bytes`, no UTF-8 check, `app/schemas.py:76`)
      crashes `pull_page_accepts`' `model_dump_json()` — an unhandled 500 out of
      the pull loop — pinned by the `non_utf8_payload` case.
- [x] **4e — document truth.** This row used to read "nothing was built, and there
      is nothing on record to build" — true at the time, and left standing until a
      scope existed rather than inventing one. It exists now: 4e is the
      document-truth slice, performed by `beam/ARCHITECTURE.md`'s revisions —
      twice on 2026-10-03, because waves 5 and 6 landed on top of the first pass
      and falsified its numbers within hours (587 passing → 723). Every count in
      that document is re-measured by running the gates and guards, not copied.
- [x] **5 — the database floor.** A hand-written zero-dependency PostgreSQL v3
      client (`VitrialSync.Db.Pgconn`, 617 lines, landed by a parallel agent at
      `3da3b87`, superseding the doctrine's `epgsql` ADOPT — amendment at
      `vitrial-phase0/DEPENDENCY-DOCTRINE.md` §4.2/§5.3) plus the wave's
      adaptation: `Codec`/`Rows`/`Writes`/`Pool`/`Config`/`Url` (2,922 lines over
      the nine `db` files), five test files, and a pool that now survives contact
      with a real server. Row coverage: **14 of the 18** domain tables. State is
      split and must stay split in every record: the shared tree is
      **serverless-green (657 passed / 20 skipped**, skip reason printed by name —
      the transport still raises `:badarg` against a live server because the ~12
      wire fixes live only in an ephemeral working copy, `/tmp/w5-live/server-api`
      at last record); a working copy carrying those fixes ran the identical suite
      **live-green (677 passed / 0 skipped) against PostgreSQL 16.14**. This tree
      has never passed a live-database test. Flip-back trigger per the doctrine
      amendment: met as written; the operator decides.
- [x] **6 — the HTTP boundary.** `vitrial_web` is built: the cowboy 2.19.0
      listener (the umbrella's first hex dependency — `beam/mix.lock` exists,
      pinned to doctrine §4.4's exact shape), the 23-clause route table
      (`router/table.ex`, counted) with two servable routes (`/health`,
      `/api/v1/version`) and every other clause on one uniform 501, the byte
      guard, the fail-open rate limiter, correlation + NDJSON events, and the four
      reference middleware stages sequenced as function calls in
      `VitrialWeb.Handler` (decided on the record in its moduledoc — cowboy 2.x
      middleware has no outbound hook). **66 tests against the real listener.**
      Standing cost of the pin: cowlib 2.20.0 carries two open advisories
      (EEF-CVE-2026-43966 MEDIUM, EEF-CVE-2026-43969 LOW); no version room under
      the exact pin.

### Open, and specific
- [x] **Point `ci.yml`'s BEAM job at `beam/Dockerfile.gate`** — **done in
      `e279fb2`** (read in `.github/workflows/ci.yml:242-281`): the job builds the
      gate image, mounts the repository root, and fails the job on any `SKIPPED`
      line, so the oracle runs in CI and a skip can no longer pass unnoticed. The
      old `beam/`-only-mount lane this item described no longer exists in the
      workflow; its last measured figure (556 passed / 31 skipped) is retained
      above as history.
- [x] **`BEAM_BASE_IMAGE_ONLY=1` does not force the bare image** — **fixed at
      `414b7a0`**: the selection now reads the variable before `IMAGE` is chosen
      (`scripts/beam_gate.sh:58-64`), and the bare-image shape was re-measured
      through it on the landed tree (715 passed / 28 skipped — exactly the eight
      parity blocks more than the default shape's 20 skips).
- [ ] **The wave-5 transport wire fixes are not in this tree.** `db/pgconn.ex` and
      `db/conn.ex` are byte-identical to `3da3b87` (measured: `git diff HEAD` on
      both is empty); the ~12 live-wire defects a parallel agent reproduced
      against PostgreSQL 16.14 were fixed only in an ephemeral working copy
      (`/tmp/w5-live/server-api` — `/tmp`, therefore not durable). The shared
      tree's live-db suites skip with `transport raised exit: :badarg`, so this
      tree has never passed a live-database test, and the doctrine amendment's
      flip-back trigger is met as written. Land the fixes (or land a
      consciously-rejected verdict) before anything trusts the transport.
- [ ] **Four tables have no row codec / write SQL:** `auth_sessions`,
      `pairing_grants`, `reference_publications`, `users` — 14 of 18 covered
      (`beam/README.md:58-59`). The store/query layer that executes wave 4's
      `%Batch.Effects{}` through the pool is the next slice after that.
- [ ] **cowlib 2.20.0 carries two open advisories** (EEF-CVE-2026-43966 MEDIUM,
      EEF-CVE-2026-43969 LOW) with no version room under the doctrine's exact
      cowboy 2.19.0 pin. Flagged by `mix hex.audit` at adoption; visible in every
      gate run's hex resolution. Needs a doctrine-level decision (pin bump vs
      accept), not a silent one.
- [ ] **`beam/apps/PLANNED_APPS.txt`'s `vitrial_ownership` line is stale** (checked
      again 2026-10-03, still at `:23`). It still declares that
      `app/ownership.py:781-849` "carries a duplicate delivery-policy block that
      is unreachable". That block was deleted by the `VITR-V009` fix;
      `app/ownership.py:835-846` documents its absence as deliberate, and the
      defect it cited was `VITR-V009`, not `VITR-V006`.
      `check_beam_scaffold.py` passes either way — it checks the line's
      *presence*, not its truth — so nothing catches this class of rot.
- [ ] **Four stale sentences survive in docs the waves moved:** `vitrial_sync.ex:7`
      still says "this app supervises no children" while `VitrialSync.Application`
      now supervises the pool; `beam/README.md:14` still describes the Python
      side as "7,884 lines … 11,487 lines of tests" where `wc -l` measures 10,274
      and 15,199; `.github/workflows/beam.yml:9-13`'s "WAVE 5 CORRECTION" header
      claims `beam/mix.lock` pins `epgsql 4.8.0` (one package, zero transitive
      closure) — but `3da3b87` deleted that lock in the very commit that added the
      sentence, and the lock the records pass commits pins cowboy/cowlib/ranch
      (three packages, two transitive edges), so the self-hosted lane's provisioning
      story no longer matches its lockfile. All prose, all wrong, all one-line fixes
      that belong to whoever next touches those files — recorded here so they are
      not rediscovered.

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
