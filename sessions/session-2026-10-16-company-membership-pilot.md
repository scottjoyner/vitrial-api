---
date: 2026-10-16
topic: SCRUM-46/47 company membership and pilot seats
status: local-draft
---

# Session handoff: company workspace / pilot seat policy

Worktree: `/home/scott/git/vitrial-api-scrum47-20261016`.
Branch: `feat/scrum47-pilot-company-members-20261016`.
Base: cached `origin/main` at `77c83c25e7af8343004aa7487ae9f38c31d4d6cc`
(Oct 3); newer canonical upstream may exist and must be reconciled.

Implemented: optional shared-company authority for an explicitly trusted
provisioned member; PostgreSQL transactional seat checks and migration 0008;
authenticated GET /api/v1/company/workspace; migration preserves legacy
active-member counts. Does not weaken scoped legacy membership or admin guard.

Validation: disposable PostgreSQL 17 container + isolated .venv, full pytest,
migration up/down, two concurrent race tests, backend contract lint and
guard-prefix checks. Full API suite: **440 passed, 1 skipped** before the
legacy-seat backfill adjustment; rerun after final changes and record Jira.
The five-seat legacy-tenant migration preservation check passed.

Known limitations: GitHub connector returns 404 for
`astrix-group-accounts/server-api`; host GitHub CLI logged out and SSH denied.
Do not put credentials into remotes or mirror customer data to public GitHub.
No canonical push/PR, staging deployment, release or TestFlight promotion.
A company-facing invite/accept flow, sector selection, permission matrix and
physical acceptance remain separate tickets.

Next: after authorized canonical repository connection, fetch fresh upstream,
rebase/reconcile this pinned local commit; rerun migrations and all tests in CI,
open a **draft** PR, independent security review, staging witness PR #182,
then pilot QA. Release HOLD remains.
