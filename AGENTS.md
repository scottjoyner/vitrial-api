# server-api

Server authority for the Vitrial iOS configurator. FastAPI + PostgreSQL 17 + S3-compatible
evidence storage, with a BEAM/Elixir port under `beam/`. Read `README.md` for the stack,
endpoints, and security invariants, `deploy/README.md` for deploy/rollback/acceptance, and
`MIGRATION.md` for where this tree came from.

**Working on the BEAM port? Read `beam/ARCHITECTURE.md` first.** It states where the
port actually is — what is built, what is a declared scaffold, what does not exist
yet at all — and the order the remaining work has to happen in. `beam/README.md` says
what the umbrella *is*; the architecture doc says what state it is *in*, and the
remaining work is ordered by real dependencies rather than convenience.

## Where agents look — in order

1. **This file** — routing and the compliance checklist below.
2. `~/.agents/skills/vitrial-api-guardrails/SKILL.md` — the binding rules for code in this
   repo. It is auto-loaded as a skill; you do not need to paste it.
3. `trackers/vitrial-api-TRACKER.md` — where every issue and defect actually stands
   (`VITR-V001`…`VITR-V006`). The single source of truth for defect state.
4. `docs/CONVENTIONS.md` — where things live and how to name them.
5. `docs/INDEX.md` — full map of the docs.
6. `sessions/` and `memory-seeds/` — continuity for your next session.

Contract boundary: V1 is pinned to iOS client SHA `74822ac9`. Any change to
`contracts/backend/v1/` or the V1/V2 vocabulary must pass
`python scripts/validate_backend_contract.py` and `tests/test_contract_vocabulary_drift.py`.

## Compliance checklist — run before you call it done

**Test environment (cheapest trap first):**
- [ ] Apply migrations first: `alembic upgrade head`. Without them the suite fails 44 tests
      that look like defects but are only `relation "evidence_blobs" does not exist`.
- [ ] Invoke as `python -m pytest`, never the `pytest` console script (the latter aborts
      collection because `tests/test_production_preflight.py` imports `scripts.*`).
- [ ] Set `DATABASE_NULL_POOL=true POSTGRES_INTEGRATION=1`. A pooled engine + fresh-loop-per-test
      = "attached to a different loop" across ~20 tests.
- [ ] Baseline to compare: **311 passed, 1 skipped** (S3 needs Docker).

**Repeating the repo's own checks** (run these instead of re-deriving them):
- [ ] `make ubs` — UBS bug gate: fails on any NEW critical/warning finding vs the
      committed baseline `ubs-baseline.json`. The residual 16c/3w is the validated
      false-positive floor, documented in the tracker under "UBS loop status". Never
      rebaseline to make it green; scope a validated-noisy rule in `.ubsignore`.
- [ ] `make branches` — content-based branch staleness audit. On this squash-merge
      repo `git branch --no-merged` and `git cherry` both report false positives;
      this script does not. Exit 1 means a branch really holds work main lacks.
- [ ] `python scripts/check_guard_prefixes.py` — every declared guard prefix maps to a live route.
- [ ] `make beam-structure` — no undeclared `mix new` app, and no lib module that nothing
      calls and nothing tests. Both are pure-stdlib Python, so they run in about a second;
      run them before you push rather than discovering them in CI.

## Do not leave scaffolding or dead code behind

The umbrella under `beam/` has eight apps and seven of them are still empty. That is a
deliberate, recorded state, and two guards keep it from decaying. Both fail in **both**
directions, which is what makes them trustworthy.

**Never `mix new` an app and leave it empty.** An undeclared scaffold fails CI. Either
build it, or add a line to `beam/apps/PLANNED_APPS.txt`:

    # <app> -- <commit> -- <what it is for>

One sentence of intent instead of a directory of empty promises. And when an app does get
built, **delete its line** — a registry that only grows is a to-do list pretending to be a
status board, and the guard will fail CI asking you to.

A long, confident `@moduledoc` is not implementation. `mix new` writes two modules per app
and the guard counts anything beyond them, so prose cannot make an app look built.

**Never leave a module that nothing calls.** `mix compile --warnings-as-errors` catches an
unused *variable*; it cannot catch an unused *module*, because an unreferenced module is a
perfectly valid compilation unit that happens to do nothing. That is the expensive kind of
dead code — it reads as finished, and the next reader assumes something depends on it.
`scripts/check_beam_orphans.py` builds the reachability graph from source and fails on a
`lib/` module that no caller reaches and no test names. Wire it up, delete it, or record
why it is waiting in `beam/apps/ORPHAN_EXEMPT.txt` with a real reason.

A module with a test counts as specified and needs no exemption — that is what makes the
guard usable while the port is being built bottom-up.

## Never finish with uncommitted work

More than one agent edits this repo concurrently, and scratch under `/mnt/build-fast/tmp`
is wiped between sessions — so uncommitted work can be lost twice over. **Commit and push
everything you produce before you declare a task done.**

- [ ] `make verify-clean` — fails on a dirty tree or unpushed commits. Run it last.
- [ ] **Stage only your own files** (`git add <path>...`). **Never `git add -A`** — it
      sweeps a concurrent agent's in-flight edits into your commit. This has already
      happened once here: another agent's `git add -A` absorbed this repo's tracker and
      seed edits into their commit. Content survived; authorship and message did not.
- [ ] Durable tooling lives in `~/.venvs/vitrial-api` and `~/.pgdata/vitrial` (port 5435),
      never in `/mnt/build-fast/tmp`.

**Rules that are already held — do not churn them:**
- [ ] Ripgrep only, never grep.
- [ ] Env-gated, default OFF, degrade honestly (admin routes 404 when no key hash; rate limiter
      fails open and logs the bypass).
- [ ] No stubs — but `VITR-V001` is a Rule-5 violation in the guard layer: 4 declared guard
      prefixes match no live route. Fixing that is the top contribution target, not a regression.
- [ ] Measure, don't assert. And verify a guard asserts against the live path — `test_request_size.py`
      currently asserts against a phantom path and passes.

**Hard invariants that look like bugs — pinned by tests, do not "fix":**
- [ ] Soft-deleted records stay visible in a pull (`deletedAt` is delivered; absence is never evidence).
- [ ] `delivery_execution` **does** check `deleted_at` at every hop (state machine), unlike generic types.
- [ ] Evidence PUT has no middleware byte ceiling by design; `evidence_max_bytes` bounds it while streaming.
- [ ] Internal admin routes stay `include_in_schema=False` and disabled unless `ADMIN_API_KEY_HASH` is set.
- [ ] `resolve_visible`/`resolve_delivery_visible` stay line-for-line twins of the visibility probes.

**Before you add a guard, limit, or allow-list:**
- [ ] Pin it with a test that enumerates `app.routes`, not a hardcoded string.
- [ ] If it is a rejection reason, use a stable code, not `type(exc).__name__` (`VITR-V006`).

## Tracking

No in-repo tracker exists. Work lives in three open GitHub issues:
- **#28** — the real 0.3.0 spec (~30 boxes, most already shipped but unchecked). Reconciled
  state is maintained in `trackers/vitrial-api-TRACKER.md`.
- **#30** — MinIO→RustFS, landed; box still open.
- **#2** — 0.2.0 hardening, effectively complete.

Defects found during review are registered as `VITR-V001`…`VITR-V006` in the tracker, each with
severity, repro, and status. Add new verified ones there, not in the skill.
