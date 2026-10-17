---
id: VITR-API-ORG-PILOT-001
title: "Company workspace and pilot seat-bound authentication"
status: draft
date: 2026-10-16
---

# Company workspace pilot (SCRUM-46, SCRUM-47, SCRUM-48)

This describes the **local review branch**, not a production deployment. Existing
`Organization` and `Membership` records remain canonical. A company owns its
Customer and Project data; an internal staff membership is not a Customer.

## Authentication and read access

`GET /api/v1/company/workspace` requires a valid bearer session. It uses the
organization bound to that session, never an organization ID supplied in a query
or request body. The no-store response reports organization identity, active
team members, effective seat allowance and remaining seats. It does not expose
email addresses, issued access tokens, membership capabilities or admin keys.

The existing `POST /internal/admin/v1/bootstrap` retains its separate admin-key
guard and is **not** a self-service signup or invitation endpoint. An operator
may explicitly request `sharedCompanyAccess: true`, granting both
`allCustomers` and `allProjects` to that **one company member**. The request
must also include the `sync` capability and must not include individual
`customerIDs` or `projectIDs`. Existing scoped members with this flag absent
retain their old access policy; no global or cross-organization access changes.

## Seat capacity

Migration `0008_pilot_company_seats` adds `Organization.seat_limit` (default 4).
The trusted bootstrap may supply `pilotSeatLimit` in the inclusive range
1–10,000. Historical organizations retain at least their current active seat
count when migrated, rather than having existing staff displaced by the default.

The backend counts active memberships. New active seats and reactivations require
available capacity; inactive memberships do not consume a seat. Lowering the
allowance below active enrollment returns HTTP 409. Company-scoped transaction
advisory locking plus an organization row lock serializes competing writes,
including first-company creation. A revoked membership is rejected at the
existing bearer authorization boundary, even if its prior token has not expired.

## Explicit non-goals / pending gates

- This phase does **not** send invitation emails, establish new credentials from
  the public API, authorize company admins to invite members, or implement paid
  subscriptions. Member provisioning remains operator-only.
- The API does not automatically add sectors, invent customer catalog prices,
  or migrate actual customer quotations.
- Finish capability/role entitlement matrix, invitations, separate tenant
  manager UI, and full V1/V2/cross-tenant mutation coverage before declaring
  enterprise release readiness. Exact TestFlight physical acceptance remains held.
- Canonical remote push is blocked by missing GitHub authorization on x1-370.
  Preserve this local branch and **do not deploy** it until reviewed and merged
  into the actual Astrix repository with migrations scheduled.

## Test evidence

An isolated temporary PostgreSQL 17 container was migrated to revision 0008.
Positive and negative API tests exercised two shared tenant seats, cross-tenant
workspace isolation, capacity overflow, revocation and reactivation, concurrent
new-company creation, and a last-seat race. The schema was downgraded/upgraded;
a five-active-member legacy tenant kept five authorized seats. No real users,
production database, signing identities, or payments were involved.
