# SCRUM-44 backend CI import/dependency repair — research only

Source: descendant of scottjoyner/vitrial-api PR #75 head
0217466066f1ba0a023f4130016760824b242c4d. Not merged or deployed.

## Reproduced CI collection failures (October 14, 2026)

- Guard-prefix lint and PostgreSQL/S3 test collection failed importing app.auth:
  refresh research models referenced StrictModel, Field, timedelta, secrets and
  correlation_ref without importing them. This prevented the server's app.main
  module from loading for ordinary tests, not just the refresh endpoint.
- Tests inspecting Compose structure imported yaml, absent from the dev extra.
  PyYAML is therefore pinned **dev-only**; it must not enter production
  dependencies, and no production image has been rebuilt here.
- This patch contains strictly missing imports, a dev-test requirement, and
  source/contract tests. It does **not** enable a new route or change deploy
  configurations, migrations, API credentials or server topology.

## Verified locally

Isolated Python 3.12 environment: 16 scoped authentication, SCRUM-44,
OpenAPI and contract tests PASS. Guard-prefix lint PASS with 5 live declared
prefixes. No migrated PostgreSQL/S3 or production instance was contacted;
full integrated CI/redeploy remain unverified until exact-head evidence.

## Explicit NO-GO: refresh-token design is incomplete

The /api/v1/auth/refresh function exists as research source but is NOT
mounted in the live app router. Its refresh-token rotation code also does not
return a new refresh token to the caller even though it changes the server's
stored refresh hash. Do not expose it to iOS on this patch. A separately
reviewed design must establish secure renewal, Keychain custody, token
rotation/replay resistance, revocation, conflict handling and real-device
acceptance before it becomes part of a connected release.

No merge, signing, API deployment, production DB migration, customer mutation,
S3 network change or automatic failover is permitted by this research slice.

## Second proof slice: SQL schema and pairing contract (October 14)

The first GitHub PR #76 CI run advanced past missing imports, then exposed a
real database/model mismatch: AuthSession and the pairing exchange write
refresh_token_hash and refresh_token_expires_at, but migration 0007 does
not add those columns. Additive migration 0008 now introduces both **nullable**
columns and a unique, nullable SHA-256 digest index. Old sessions remain
access-token-only; no refresh token can be inferred or manufactured. CI must
recreate its disposable PostgreSQL database and run Alembic to 0008 before
running integration tests; this patch does not execute production migrations.

Synthetic rate-limit and request-size handlers were constructing an obsolete
two-field pairing response. Test doubles now match the server's actual
four-field PairingExchangeResponse (access and refresh tokens, both expiries).
No server wire behavior is changed by the fixture correction.

The unit job installs only requirements-lock.txt followed by dev package
without dependencies, so declaring PyYAML under dev was **insufficient**.
Explicitly pin PyYAML 6.0.3 in the test lock as well. It remains absent
from runtime project dependencies; the packaged image needs separate audit.

Independent isolated local Python 3.12 execution now reports **357 passed,
51 skipped** (PostgreSQL/S3 opt-in integration skipped), plus the subset
**54 passed** including response-fixture and migration-schema checks. This
is not an independent physical DB readback or acceptance deployment.

### Live real-app release implications

The iOS pairing decoder currently consumes accessToken and expiresAt;
extra server fields are ignored. The server research code creates refresh
tokens, but /api/v1/auth/refresh is **not mounted**, so there is no verified
iOS session-renewal path. This is a distinct HOLD, not a reason to expose
incomplete token rotation. A new reviewed end-to-end refresh contract must
cover Keychain token custody, atomic rotation, revocation, replay resistance,
offline ambiguity and upgrade from existing paired devices.

## HTTP-to-durable-database acceptance extension

The isolated PostgreSQL/S3 GitHub integration lane has now completed its
actual Alembic upgrade/downgrade/re-upgrade exercise and 408 tests PASS.
RustFS reported live S3 readiness during the run.

A new opt-in PostgreSQL test exercises FastAPI ASGI HTTP (not internal
apply_push alone) with **two independently paired synthetic operators**:
two scoped /auth/me checks, a deliberately unordered
Customer -> Project -> ProjectSector -> Item sync/push, separate PostgreSQL
canonical row readback, second-operator sync/pull, durable mutation-id
acknowledgements, idempotent replay, logout revocation, and continued access
from the remaining operator. Records use disposable CI-only identity IDs and
are cleaned up after execution.

This test is SKIPPED unless POSTGRES_INTEGRATION=1 and requires a properly
migrated disposable database; an unexecuted fixture is not evidence. Its
actual CI result must be evaluated independently at the exact branch head.

The acceptance Compose lane still failed on API health despite the separate
successful DB/S3 job. Its boot step has been changed to collect bounded
service logs and health statuses even when Compose up fails before the
existing readiness loop. The failure still fails CI and never causes an
automatic redeploy. Do not infer production topology readiness until that
diagnostic run identifies and resolves the fault.

### October 14 acceptance container health investigation

At research head d5abe40, Docker Compose started Uvicorn successfully, PostgreSQL
and RustFS reported healthy, and s3-init created the evidence bucket, yet the
API container's composite dependency readiness healthcheck still failed. Logs
alone exposed no dependency-specific error because the probe uses quiet mode.
The next diagnostic iteration runs the same read-only database and storage
probes *inside the failed container* and emits only categorical ok/unavailable
statuses. It preserves exit 1 and tears down disposable CI services; it never
prints a connection URL or credential. This is fault isolation, not an
acceptance pass or permission to deploy.

### Cold-boot health convergence

The latest isolated acceptance CI diagnosis found that the API was running,
PostgreSQL and RustFS were healthy, and an in-container manual read-only
dependency probe reported database=ok, objectStorage=ok, status=ready even
though Docker Compose had already marked its earlier startup healthcheck
unhealthy. This is a cold-boot/health convergence failure, not permission
to skip dependency health.

The disposable-only acceptance runner now checks Docker's own API container
health status for up to 12 bounded 5-second attempts after an initial Compose
rejection, and only re-evaluates the unchanged Compose graph after status is
actually healthy. If Docker never reports healthy, the job still emits the
bounded redacted dependency report and FAILS. It does not change production
Compose readiness thresholds, repeat domain writes, start production servers,
or tolerate permanently unhealthy API processes.
