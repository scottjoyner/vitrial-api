---
kind: candidate
recordKind: continuity
category: goal-set
seed_type: goal-set
name: vitrial-api-state-2026-10-02
description: "vitrial-api at clean main 208afa4: 293 tests green, 1 skipped. Six verified defects VITR-V001..V006 filed in trackers/vitrial-api-TRACKER.md. First-contribution target: VITR-V001 guard-prefix dead rules."
agent: nez
date: 2026-10-02
tasks: []
projects: [vitrial-api]
repos: [vitrial-api]
---

## State

`/home/nez/Projects/vitrial-api` @ `208afa4` (HEAD, clean tree). PostgreSQL 17-backed
suite: **293 passed, 1 skipped** (S3, needs Docker). No open PRs. Three open issues: #28
(the real 0.3.0 spec), #30 (MinIO→RustFS, landed but box still open), #2 (effectively done).

## UBS status — loop closed 2026-10-02

Real fixes shipped: 6 `security-assert` criticals in `ownership.py` (asserts →
`AuthorizationRejected`), and the blocking read on `LocalObjectStore.stream` now uses
`asyncio.to_thread`. `ubs --only=python` (with `.ubsignore` scoping) dropped criticals
48→16, warnings 65→3. The residual 16/3 are documented false positives for this codebase
and are recorded in the tracker so the loop has a defined floor, not an infinite one —
running `ubs` to literally 0 means distorting `auth.py`'s `token_hash ==` lookup,
`ownership.py`'s int `revision !=` comparison, or the validated `X-Request-ID`, all of
which would be a regression. Next session: treat any *new* class of warning as a real-fix
prompt, and do not re-litigate the false-positive set.

## Pipeline gates + branch hygiene (established 2026-10-02)

Three repeatable gates now exist so the next agent inherits the mechanism instead of
re-deriving it:
- `make ubs` — UBS delta gate vs committed `ubs-baseline.json` (floor 16c/3w = validated
  false positives; never rebaseline to go green; scope noisy rules in `.ubsignore`).
- `make branches` — content-based branch staleness. On this squash-merge repo
  `git branch --no-merged` and `git cherry` both report false positives; this does not.
  Exit 1 means real unmerged work. Current: 0 real.
- `python scripts/check_guard_prefixes.py` — every guard prefix maps to a live route.

`make check` runs contract validation + guard lint + tests. Baseline: **430 passed, 1 skipped** (includes other agents' BEAM, docs and assembly-contract work merged into main — that count is the whole tree, not just this agent's changes).

## HOME MOVED (2026-10-02)

Work now happens in **`/home/nez/Projects/server-api`** (GitHub `astrix-group-accounts/server-api`),
not `~/Projects/vitrial-api`. All six W1–W6 commits migrated with history intact, verified as
ancestors of the new `main` (`48fc04f`). `~/Projects/vitrial-api` is a read-only source now.
A BEAM/Elixir effort runs on the same repo by another agent; Python-only changes, commit
your own files only (`git add <files>`, never `git add -A` — there is concurrent in-flight work).

## Graceful drain landed (commit 04a612d)

The one W7 item whose absence was a real defect. 100 MiB streamed uploads and org-locked
pushes were being SIGKILLed by Docker's 10s default on every deploy. Fixed with a drain
flag: `/ready` goes 503 on SIGTERM while `/health` stays 200, uvicorn gets a 45s graceful
timeout, compose gets `stop_grace_period: 60s` (must EXCEED it), and `deploy.sh` awaits the
replaced container and fails on non-zero exit. Negative controls planted and confirmed red.

Durable tooling: venv `~/.venvs/vitrial-api`, PostgreSQL at `~/.pgdata/vitrial` port 5435.
`/mnt/build-fast/tmp/opencode` is wiped between sessions — do not put anything load-bearing there.
A parallel BEAM/Elixir effort also commits on `main`; my Python commits are all ancestors.

## W1–W6 all closed (2026-10-02)

All six `VITR-Vxxx` defects are fixed and the full suite is green at 311 passed / 1 skipped.
Commits: W1 `18a4198`, W2 `5f6178f`, W3 `d0e1d93`, W4 `33ccee4`, W5 `30b29ab`, W6 `257e725`.
Remaining work is the #28 production-foundation boxes tracked as W7, not defects. Note the
parallel BEAM pass also committed on this branch; my commits are all ancestors of HEAD.

Environment note: the `/mnt/build-fast/tmp/opencode` scratch is wiped between turns. Durable
tooling now lives at `~/.venvs/vitrial-api` and a PostgreSQL cluster under `~/.pgdata/vitrial`
(port 5435). Run migrations before pytest or 44 tests fail for the wrong reason.

## Good first contribution — W1 (`VITR-V001` guard-layer dead rules), top of the tracker queue

Dead guard prefixes: `request_size.py:79-82` has `/api/v1/sync/v2/push`, `/api/v1/admin`,
`/api/v1/auth/refresh` (none exist); real V2 is `/api/v2/sync/push` with **no** size ceiling
and no `SyncBatchV2` aggregate validator. 20 MB body → 422 on V2 vs 413 on V1. Fix both the
prefixes and the missing aggregate ceiling, and rewrite guard tests to enumerate `app.routes`
rather than assert on literals. Spec'd on #28 P0.

## What to not touch without a deliberate decision

- Soft-delete pull visibility asymmetry (`ownership.py:970-1001`) — `deletedAt` is delivered
  on purpose; a tombstone must be delivered, absence is never evidence. Pinned by
  `test_soft_delete_visibility_probe.py`.
- `delivery_execution` **does** check `deleted_at` at every hop — different rule, different question.
- Evidence PUT intentionally has no middleware ceiling; `evidence_max_bytes` (100 MiB default)
  bounds it while streaming.
- Internal admin routes stay `include_in_schema=False`; disabled unless `ADMIN_API_KEY_HASH` set.
- `resolve_visible`/`resolve_delivery_visible` must stay line-for-line twins of
  `record_is_visible`/`delivery_execution_is_visible` — pinned by
  `test_pull_prefetch_equivalence.py`.

## Metrics + Dependabot landed (commits 91434e0, beab668, 32d2a30)

**Metrics** (`app/metrics.py`, `app/metrics_middleware.py`). Hand-rolled Prometheus
exposition rather than `prometheus_client` — the image is security-scanned and carrying a
live advisory, so a dependency for ~120 lines is a bad trade. `GET /metrics` is
`include_in_schema=False` and gated on `require_admin_key`.

The design constraint that matters is **cardinality**, defended twice: request metrics
label on the matched route template (never the raw path — otherwise every uploaded
document mints a permanent series and OOMs the scraper), and every metric carries a
series cap that, when exceeded, is refused and counted in
`vitrial_metrics_label_series_dropped_total` rather than silently growing.

Bugs found while building it:
- `Registry.render()` held `_LOCK` and called `metric.render()`, which re-acquires the
  same non-reentrant lock. **Self-deadlock** — a scrape hangs forever rather than
  raising. Split into `_render_unlocked`; pinned by a watchdog-thread test.
- My first failure-policy test asserted the wrong mechanism and passed against a planted
  defect. Response integrity is *structural* (recording runs in a `finally` after the
  response is fully sent); what `except: return` actually buys is that the exception
  doesn't escape and turn every request into a spurious 500. Pin the real property.

**Dependabot alert 1**: `pytest<9.0.3` (GHSA-6w46-j5rx-g56g). `pytest-asyncio` 0.26
hard-pins `pytest<9`, so **both** must move or the environment is unsatisfiable rather
than partially fixed. Dev-only — pytest is not a production dep. Verified on an isolated
venv *and* from a clean-room lockfile install (the real CI path) before committing.

## W3 trust-proxy assertion landed (commit 94eaef8)

The XFF overwrite from W3 shipped untested: it could be reverted or re-spelled with no
test going red, while `rate_limit.client_key_for` reads the **left-most** XFF entry —
which under Caddy's default *append* semantics is the attacker's. A caller sending
`X-Forwarded-For: 1.2.3.4` gets a fresh throttle bucket per request and the rate limit
is a no-op.

Three assertions, because the positive case alone leaves the hostile spellings open:
both Caddyfiles carry the overwrite; `{http.request.header.X-Forwarded-For}` is rejected
explicitly (reads like a fix, is the exact reverse); and the preference order *inside*
`client_key_for` is pinned, so the Caddy control cannot quietly become decorative if the
key derivation ever changes. All three confirmed red against planted defects.

## Concurrent-agent merge (2026-10-03)

The BEAM agent merged 8 commits (`3830efe` "Merge current Vitrial API work into Astrix
home", incl. #66 ops baseline and #68 topology evidence). My push was rejected as
non-fast-forward. `git diff --stat HEAD...origin/main` showed overlap only in
`deploy/README.md` (theirs), so I stashed `ubs-history.json`, rebased, re-ran the full
suite on the merged result (**356 passed**), and pushed. The dropped stash held a stale
scan timestamp superseded by the committed one — dropped rather than left to rot.

## The image was never installing the lockfile (commit 150c7ac)

`requirements-lock.txt` was committed and CI installed it — but the **production
image** ran `pip install .`, resolving against the *ranges* in `pyproject.toml`. The
lockfile was decorative for the only artifact that ships. Fixed with a generated
`requirements-runtime-lock.txt` + `scripts/generate_runtime_lock.py`.

**The lesson worth keeping: do not derive a set by subtracting another set.**
"Combined lock minus dev packages" was the first implementation and it is wrong
*quietly*. Resolving the runtime and dev closures separately in throwaway venvs showed
they overlap in both directions — `anyio`, `certifi`, `idna`, `packaging` come from
both httpx and fastapi/botocore — so `certifi` looks dev-only in one resolution and
required in another. A hardcoded denylist drops a package the image needs, and the
failure is an ImportError at container start for a dependency nobody thought was
optional. Resolve the set you actually want instead.

Verification that mattered: mirrored the Dockerfile `RUN` into a fresh venv and booted
it. `starlette.testclient` refusing to import for want of `httpx` was the proof the
runtime lock actually excludes dev tooling — the error *is* the assertion.

UBS caught a real defect in this work on its first run (`subprocess.run` with no
timeout — a hung index would wedge a build step forever). Fixed with an explicit bound.
The companion finding (float-equality on `alembic==1.20.0`) is a version pin read as
numeric comparison; scoped in `.ubsignore` with reasoning rather than worked around.

## VITR-V007 — silent data loss in evidence publication (commit 76e11e5)

The most serious defect found so far, and it survived because no test exercised the GC
happy path. A client got **201** for an upload whose object the collector had already
deleted; every later `GET`/`HEAD` on that document 404'd.

Reachable interleaving: upload v1 (bytes X) → key K canonical → upload v2 (bytes Y) →
key K2 canonical and K queued for GC → upload v3 (**bytes X again**; keys are
content-addressed so this writes K again, and `evidence.py:209` queues K2 leaving the
stale row for K) → collector deletes K because the blob still points at K2 → upload v3
commits its pointer to K and returns 201.

**The lesson: a lock only protects what happens inside it.** `put_blob` streams the
object BEFORE taking the org row lock — deliberately, so a 100 MiB upload does not hold a
transaction for its whole duration. So the lock serialized the pointer *switch* while
leaving the *write* outside it, and a comment at `evidence_gc.py` asserted a protection
the ordering never provided. Comments that promise safety are load-bearing; if the code
changes, they must change with it.

Fixed from both sides (both take the same row lock, which is what makes the pair safe):
cancel any pending GC row for the key being published, then verify the object still
exists and fail retryably rather than commit a pointer to a missing object. Each half
verified against a planted regression.

`VITR-V008`–`VITR-V013` are logged in the tracker and NOT fixed. Of those, **VITR-V012 is
the highest leverage**: the collector's happy-path test cannot pass under the shipped
86400s GC grace, so GC is effectively unexercised — which is precisely why -007, -010,
-011 and -013 all remain reachable. Fixing the *test* unlocks the rest.

## VITR-V012 + VITR-V014 — the untested path was hiding a crash (commit 0fab6f8)

Fixing `VITR-V012` (GC untested) immediately produced `VITR-V014` (HIGH) on its first run.
Worth remembering as a sequence, not two facts.

`VITR-V012`: the collector's only test was gated behind `S3_INTEGRATION=1`, which the
default baseline does not set. But the gate is only needed for S3 *multipart* behaviour —
`evidence_storage_provider` defaults to `local`, so the whole collector was exercisable
with `LocalObjectStore` and no S3. **A test behind an integration flag is not coverage**;
the flag gets used to excuse testing the logic too.

Worse, that test could never have passed: `queue_blob_gc` sets `not_before = now + 86400`
(the shipped grace) and the test collected at wall-clock `now`, asserting `deleted >= 1`.
Measured rather than assumed — `deleted=0` at `now`, `deleted=1` past the grace. **An
assertion the shipped configuration cannot satisfy is a test of nothing, and a green
suite is not evidence it is right.**

`VITR-V014`: the collector's `StorageError` handler did `db.rollback()` then read `row.id`.
A rollback expires every instance in the session, so that touched property triggers a
refresh — lazy IO from a sync context, `MissingGreenlet` under asyncpg. The handler
written to record a failed collection instead **aborted the whole run**, precisely when
object storage is unhealthy. Same shape as `VITR-V007`: code that only executes on the
error path is code nobody has run.

## Verified another agent's unverified "Fixed" claims (2026-10-04)

A concurrent agent closed `VITR-V008`/`-009`/`-010`/`-011`/`-013` and honestly admitted it
could run **no Python test** (no sqlalchemy, no boto3, no venv; PostgreSQL unreachable) —
"Fixed means pending a green suite". Its tests were written, just never executed.

The environment existed all along: `~/.venvs/vitrial-api` + `~/.pgdata/vitrial` on 5435.
Re-ran at `77c83c2`: **415 passed, 1 skipped**, all gates green. Then checked each claim
individually instead of trusting the suite:

- `-010` backoff **measured**: 300 s → 600 s → 1200 s across three failures.
- `-011` tests are **load-bearing**: re-planting the per-row `commit()` turns **7 tests
  red**, including the two-sessions overlapping-collector behavioural test.
- `-009` delivery **fails closed** — the guard is an f-string at `ownership.py:847`, which
  is why `rg 'not implemented for delivery_execution'` finds nothing and looks alarming.
- `-004` correctly left unfixed.

**Lesson: "no interpreter in this workspace" is a claim about the workspace, not about the
repo.** Before accepting it, check `~/.venvs/` — the tooling seeds live outside the tree
precisely so they survive, and a missing module in the default interpreter says nothing
about whether the suite can run.

Also: a green CI lane is evidence about *CI*, not a substitute for a local run. CI's
integration job had already proven these; the value was in running it where a planted
regression could be tested.

## VITR-V004 reduced, not closed (commit 03e10d9)

Four GET helpers each call `ensure_baseline_publications`, so every read route took a
WRITE transaction and committed. That is why `VITR-V008` existed at all — the first
writer was a GET, so two concurrent GETs raced — and it means reads can never come from a
replica.

`bootstrap_identity` now seeds at creation. **The lazy GET seed is kept deliberately.**
Removing it would change what a GET returns for an organization that exists but was never
bootstrapped through this path, and that is client-visible on a pinned contract. So
`VITR-V004` is *reduced*: for new organizations the write path seeds and the GET finds
nothing to insert. Closing it needs a backfill migration for existing organizations first,
which is a deployment decision, not a code cleanup.

Lesson from the test: the first version truncated the `organizations` table and **passed
alone but failed in the full suite** — other tests leave `canonical_projects` rows whose FK
chain reaches `organizations`. Cleanup is now scoped to the two org ids the file creates.
A test that clears global tables is asserting on the absence of other tests' state as much
as on its own, and it will look fine right up until someone adds a test with an FK into
your tables.

## Session lifecycle was unpinned (commit ddae47b)

`app/auth.py::current_principal` is the single gate every authenticated route passes
through, with four rejection branches. **None was exercised by name, and two were never
exercised at all.**

Expiry was the serious one. Every session the suite created had `expires_at` in the
*future*, so `expires_at <= now` never fired. Deleting the clause — or inverting it to
`>=` — would have left the suite green while every expired bearer token kept working. A
revocation test existed; an expiry test did not. That asymmetry is the lesson: covering
*one* revocation lever is not covering the gate, and the covered one is the one that
looked more important.

**A behavioural test cannot pin the expiry boundary, and I only found out by planting.**
My first boundary test set `expires_at = now` and expected rejection — which passes
under `<=` *and* under `<`, because `current_principal` computes its own `now` a few
microseconds later. Planted `<`, the test stayed green. It is now an AST assertion, with
the commit saying why. When a distinction is not observable from outside, say so and pin
the shape rather than shipping a test that flatters itself.

## Burned lessons (each cost a pipeline cycle — do not repeat)

- Run migrations before `python -m pytest` or you get 44 failures that look like defects but
  are only `relation "evidence_blobs" does not exist`.
- Invoke as `python -m pytest`, not the `pytest` console script — the latter aborts collection
  because `tests/test_production_preflight.py` imports `scripts.*`.
- `DATABASE_NULL_POOL=true` is required — pooled engine + fresh-loop-per-test = "attached to a
  different loop" across ~20 tests.
- Three occurrences, same class as `VITR-V001`: a correct component that was not the code that
  ran (`request_size.py:40-44`); `pull_prefetch` excluding delivery under a comment claiming
  the opposite; prefetch "locally green, CI red" for three rounds because the defect was in code
  only the integration job executes.
- A test that passes against a planted defect is worse than no test: it launders the gap as
  coverage. Always confirm a negative control actually turns red — twice this session the
  first attempt didn't fail, and both times the *test* was wrong, not the code.
- Don't `pip install -e .` into a scratch venv to test a dependency bump: it silently
  downgrades pytest back to the range in `pyproject.toml` and you will think you tested
  9.x while running 8.x. Install the target version explicitly afterwards and check
  `pytest --version` before trusting any result.
- On a shared branch, check for overlap with `git diff --stat HEAD...origin/main` BEFORE
  rebasing a rejected push. "Rejected" does not mean "conflicting" — this time only the
  other agent's `deploy/README.md` moved, and the rebase was free. Re-run the suite on
  the merged result either way: their 8 commits changed the tree under you.
- `ubs-history.json` is rewritten by every `make ubs` run, so it is almost always dirty
  when you go to rebase. Stash it explicitly (`git stash -- ubs-history.json`) or the
  rebase refuses to start.
- S3 integration tests are gated behind `S3_INTEGRATION=1` and are NOT in the default
  baseline, so a whole subsystem (evidence GC) can be effectively untested while CI is
  green. When reading a green CI result, ask what the default baseline actually skipped.
