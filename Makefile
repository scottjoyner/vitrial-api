SHELL := /bin/bash

.PHONY: install postgres postgres-native migrate migrate-native test test-integration test-all run check beam-structure ubs ubs-accept branches verify-clean

install:
	python -m pip install -e '.[dev]'

# Container runtime. Convenient, and what CI uses.
postgres:
	docker compose up -d postgres

# Homebrew PostgreSQL, for machines without a container runtime.
#
# The PostgreSQL integration suite is skipped unless POSTGRES_INTEGRATION=1, so
# without a reachable database it is easy to believe the whole suite passes when a
# third of it never ran. That is not hypothetical: the batched pull prefetch (#53)
# shipped three rounds of locally-green, CI-red results because every defect was in
# code only the integration job executes.
postgres-native:
	@command -v pg_ctl >/dev/null 2>&1 || { \
		echo "PostgreSQL not on PATH. Install it first:"; \
		echo "  brew install postgresql@17"; \
		echo "  export PATH=/opt/homebrew/opt/postgresql@17/bin:\$$PATH"; \
		exit 1; }
	@pg_ctl -D /opt/homebrew/var/postgresql@17 -l /tmp/postgresql@17.log start \
		2>/dev/null || echo "(server may already be running)"
	@psql -d postgres -c "CREATE ROLE vitrial LOGIN PASSWORD 'vitrial' SUPERUSER;" 2>/dev/null || true
	@psql -d postgres -c "CREATE DATABASE vitrial OWNER vitrial;" 2>/dev/null || true
	@echo "vitrial/vitrial@localhost:5432/vitrial ready (matches .github/workflows/ci.yml)"

migrate:
	alembic upgrade head

PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python)

# Unit tests. Uses the module form and the same interpreter resolution as
# test-integration, because the `pytest` console script does not put the repository
# root on sys.path -- `tests/test_production_preflight.py` then fails to import
# `scripts.probe_production_preflight` and collection aborts, which reads as a
# broken repository rather than a broken invocation. See test-integration below.
test:
	$(PYTHON) -m pytest -q

# The suite CI actually runs, against a real database.
#
# Invoked as `python -m pytest`, not the `pytest` console script, because the latter
# does not put the repository root on sys.path and `tests/test_production_preflight.py`
# imports `scripts.probe_production_preflight`. With the console script that file
# fails to import and collection aborts, which reads as a broken repo rather than a
# broken invocation. CI uses the module form for the same reason.
#
# DATABASE_NULL_POOL=true is not optional. The pooled engine retains connections
# across tests while pytest-asyncio gives each test a fresh event loop, and the
# result is "attached to a different loop" across ~20 tests. CI sets it for the same
# reason; without it this target fails in ways that look like application defects.
test-integration:
	DATABASE_NULL_POOL=true POSTGRES_INTEGRATION=1 \
		$${PYTHON:-$$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python)} -m pytest -q

# Unit plus integration, which is what CI does.
test-all: test-integration

run:
	uvicorn app.main:app --reload

check:
	$(PYTHON) -m compileall -q app migrations tests
	$(PYTHON) scripts/validate_backend_contract.py
	$(PYTHON) scripts/check_guard_prefixes.py
	$(MAKE) test
	$(MAKE) beam-structure

# Structural guards on the BEAM umbrella. Both run on the host with no Elixir
# toolchain, which is the point: they must be cheap enough to run on every save,
# not only in a container on CI.
#
# They are also both directionally enforced -- they fail on drift in either
# direction, not just on something new appearing. A guard that only ever adds
# findings is a to-do list; a guard that also fires when something is cleaned up
# is a status board.
#
#   check_beam_scaffold.py  an app that is a `mix new` scaffold must be declared
#                            in beam/apps/PLANNED_APPS.txt, and a declared app
#                            that has since been built must have its line removed
#   check_beam_orphans.py    a lib module that nothing calls and no test names is
#                            dead code; wire it, delete it, or exempt it with a
#                            reason
#
# Unused *variables* are already covered by `mix compile --warnings-as-errors` in
# the BEAM CI job. These two cover the shapes the compiler cannot see: an app
# that is all intention, and a module that is all documentation.
.PHONY: beam-structure
beam-structure:
	$(PYTHON) scripts/check_beam_scaffold.py
	$(PYTHON) scripts/check_beam_orphans.py

# UBS rapid bug gate — the same verdict CI enforces, locally, in seconds.
#
# Fails on any NEW critical/warning finding versus the committed baseline
# (ubs-baseline.json); the baseline's residual findings are the documented
# false-positive floor (see trackers/vitrial-api-TRACKER.md "UBS loop status").
# A scan that cannot run is a failure, not a pass — see scripts/ubs_gate.sh.
ubs:
	bash scripts/ubs_gate.sh ubs-baseline.json

# Are any branches holding work main does not have? Content-based, because
# --no-merged / git cherry both lie on a squash-merge repo. Exit 1 if real work found.
branches:
	$(PYTHON) scripts/audit_branch_staleness.py

# Accept the findings the last scan reported as KNOWN.
#
# `make ubs` writes ubs-history.json, which is what makes "new" mean "never seen
# before" rather than "absent from the last run". It is deliberately NOT
# auto-committed by the gate: accepting findings silently is the rebaseline the
# gate exists to prevent, so acceptance is an act somebody performs on purpose.
#
# Run this only after deciding each new finding is a validated false positive or
# an accepted debt -- never to make the gate green.
# Refuse to finish a session with uncommitted work.
#
# More than one agent edits this repo concurrently, and the `/mnt/build-fast/tmp`
# scratch is wiped between sessions -- so uncommitted work can be lost twice over.
# Run this before declaring anything done. It is a check, not a fixer: never
# `git add -A` here, because that sweeps another agent's in-flight edits into your
# commit. Stage the files you actually changed.
verify-clean:
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "WORKING TREE DIRTY -- commit or stash before finishing:"; \
		git status --porcelain; \
		echo; \
		echo "Stage ONLY your own files (git add <path>...); git add -A sweeps a"; \
		echo "concurrent agent's in-flight work into your commit."; \
		exit 1; \
	fi
	@if [ -n "$$(git log --oneline @{u}..HEAD 2>/dev/null)" ]; then \
		echo "UNPUSHED COMMITS -- push before finishing:"; \
		git log --oneline @{u}..HEAD; \
		exit 1; \
	fi
	@echo "clean: tree committed and pushed"

ubs-accept:
	@test -f ubs-history.json || { \
		echo "no ubs-history.json; run 'make ubs' first"; exit 1; }
	@echo "ubs-history.json now records these findings as known:"
	@$(PYTHON) -c "import json;d=json.load(open('ubs-history.json'));print(len(d['fingerprints']),'fingerprints')"
	@echo
	@echo "Review the diff, then commit it yourself:"
	@echo "  git diff ubs-history.json && git add ubs-history.json && git commit -m 'UBS: accept <n> validated findings as known'"
