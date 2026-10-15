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
