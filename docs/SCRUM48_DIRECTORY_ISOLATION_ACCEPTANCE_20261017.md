# SCRUM-48: restricted company directory — isolated acceptance

**Scope:** An incremental SCRUM-48 security fix on top of the existing SCRUM-47 company-membership pilot. Not a substitute for the full cross-company customer/job/item/quote/sync matrix in SCRUM-64.

## Observed issue

`GET /api/v1/company/workspace` previously returned the full active member
directory (names, user IDs, membership IDs and role labels) to **any** valid
company bearer, including a restricted supplier/member without company-wide
Customer/Project access. The organization filter was correct, but this
violated least privilege *within* a company.

## Server-authoritative policy

- Principal and tenant are loaded from the current, active bearer session and
  membership; request-supplied `organizationID` is ignored for authorization.
- A member with `organization.members.manage` or
  `organization.roles.manage` sees the current company's team list.
- An ordinary shared-company member with both `all_customers` and
  `all_projects` plus `sync` also sees the team list.
- A scoped-only or restricted member sees **only its own** active membership
  and role labels; the server still counts total occupied seats without
  exposing the other members' names or IDs.
- A restricted principal whose own member ID is absent from the query
  fails closed with HTTP 401. The response remains `Cache-Control: no-store`
  and indicates `memberDirectoryScope: "team" | "self"`.

This changes a read-only response, not the seat admission logic, canonical
customer ownership, authorization tokens, pricing, commercial quotations,
manufacturer authority, or any production migration.

## Actual Fleet Commander acceptance

- Branch: `feat/scrum48-directory-scope-20261017`
- Base: `c9e6f86f7c1dc73a85fdcc1ef21b4e36e8e6aef2` (SCRUM-47 membership pilot)
- Implementation: `app/company_policy.py`, `app/company_workspace.py`
- Unit, contract and adversarial tests: `tests/test_company_policy.py`,
  `tests/test_company_workspace_contract.py`,
  `tests/test_company_workspace_postgres.py`
- Full local suite: **384 passed / 66 skipped** in the no-integration run.
  Skips are not counted as passes.
- Real disposable PostgreSQL 17, initialized with Alembic **0008**: **34
  tests passed / 0 skipped**, including concurrent seat limits and two-company
  scoped/shared views. PostgreSQL container was bound to `127.0.0.1` only.
- Actual temporary loopback TCP API smoke: `GET /health=200`,
  `GET /ready=200`; two synthetic companies; same-company shared user sees
  three active members; restricted user sees exactly itself; member manager
  sees the team; spoofed organization query is ignored; unauthenticated
  bearer returns 401; private response has no-store cache control.
- The temporary staging Uvicorn instance was **stopped** after acceptance,
  and no production API was deployed. All test accounts and records are
  synthetic inside the disposable database.

## Stop/go and unresolved dependencies

**GO for integration review only:** submit as a descendant of the SCRUM-47
company branch. First reconcile it with the latest canonical
`astrix-group-accounts/server-api` main, run the full SCRUM-64 cross-company
negative suite on a disposable migrated database, and obtain an independently
reviewed entitlement matrix (supplier-partner directory privileges).

**NO-GO for production:** current Fleet session cannot authenticate to the
canonical repository's remote, and its GitHub connector returns 404 for
`astrix-group-accounts/server-api`. No remote branch/PR or hosted exact-head
CI success is claimed. Never copy stale feature changes into production merely
to bypass the repository authorization or review.

VM101 still has unresolved approved backup restore/WAL recovery, VM100 API
private endpoint cutover, NFS/RustFS custody, real TestFlight physical iPhone
acceptance and a separately authorized release window. See the wider
`VITRIAL_OPEN_ISSUES_DEPENDENCY_PLAN_20261018.md` on the Vitrial recovery
draft branch.
