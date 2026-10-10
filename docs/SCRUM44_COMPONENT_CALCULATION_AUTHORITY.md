# SCRUM-44 v0.2.2 — Authorized Item Component Calculation

**Status:** Draft review contract; not enabled in production or TestFlight.
**Jira:** SCRUM-44. **PDF:** *Item Configurator - Aluminum, Glass & Steel.pdf*,
sections 8–13, 19–24, especially AC8–AC14, AC18, AC27–AC29.

## Endpoint boundary

`POST /api/v1/items/{item_id}/component-calculation/preview`

Read-only despite being POST (request body carries bounded calculation inputs).
Requires a real bearer-authenticated session; `require_item_access` scopes the
Item to the authenticated organization/customer/project before publication
lookups. The Item path and payload Item ID must match. Does **not** write
configuration versions, catalog entries, quotations, orders, or production states.

The request includes `configuration` plus both immutable reference publication
IDs and **their exact expected SHA-256 content digests**. Client-controlled
`trustedRules`, `trustedCatalog`, `overrideAuthorized`, or other injected
fields are rejected by the request's strict Pydantic schema.

The server resolves catalog and compatibility-rule publications exclusively
from `(principal.organization_id, publication_id)`; cross-organization
publications cannot satisfy the request. Both publications must have exactly
the declared kind, matching stored SHA-256 and `authoritative: true`.
Bundled/fixture/non-authoritative catalogs must not drive genuine materials.

Technical rules are read from **approved immutable**
`compatibility_rules` publications. Each applicable `compatibilityRules`
entry has the rule ID and Item Type ID, and the following typed shape within
`attributes.componentQuantityRule`:

- `catalogEntryID`: exact ID from the pinned catalog.
- `systemID`: required aluminum system ID, or null for a genuinely generic
  rule approved for all systems.
- `mode`: `per_item` or `per_matching_section`.
- `sectionType`: required only for `per_matching_section`.
- `quantityPerOccurrence`: **positive finite number approved by technical owner**.

Catalog `entries` are eligible only when they carry an explicit
`attributes.componentUnit` (`each`, `panel`, `m`, or `m2`), with
optional `itemTypeIDs` and `systemIDs` compatibility lists.
The fixture's `2 rollers per sliding section` is *test data*, NOT an approved
System 7-44 rule. Real component quantities, cut sizes, losses and deductions
cannot be invented from generic rectangular geometry.

## Safety and lineage

- Preview contains independently owned Item/configuration/version component
  identities, source rule IDs and versioned catalog reference snapshots.
- Input SHA-256 covers the normalized calculation input including opening/product
  sizes, section identities/types/opening directions/sizes, provenance of inferred
  widths, aluminum and glass selections, additional vano legs, panel specifications
  and explicitly selected component IDs.
- Response envelope `vitrial.item-component-preview.v1` includes
  `authorization: read_only_preview`, both source publication digests,
  bounded catalog snapshots and nested `vitrial.item-components.v1` calculation.
- Missing/ambiguous rules, incompatible/inactive references, duplicate identities,
  invalid quantities or denied overrides produce **zero proposed components** and
  explicit issues. Rule precedence is not guessed.
- Override is permitted only when the **authenticated Principal** carries
  `item.components.override`; user input cannot elevate privilege.
- Publication or digest errors return 409, unauthorized Item scope 403,
  absent authentication 401 and malformed request 422.
- The endpoint accepts an authenticated *draft preview*, not proof that a
  client-submitted configuration version is stored as an approved canonical revision.
  Completion/procurement/production require independent immutable version validation.

## Review and deployment gates

1. Business technical owner publishes and signs the exact vendor catalog and
   approved compatibility/quantity rules through the existing protected
   `reference-data` publication workflow.
2. Verify unique active SKU/system compatibility, server-limited catalogs, quantity
   rationale, and independent catalog and rule SHA-256s.
3. Run read-only scope isolation, input-lineage, missing-rule, override and HTTP
   tests, then validate Swift request/response fixtures and full iOS round-trip.
4. QA exact-source simulator and physical iPhone save/reopen/preview/recalculate.
5. Authorize canonical Astrix API/iOS review and Gate A–F production readiness
   **separately**. Do not release just because this draft CI passes.

## Verification

Local isolated API mirror worktree:
`/home/scott/git/wt-vitrial-api-scrum44-20261010`.
Targeted Python tests (calculator, route, auth HTTP, OpenAPI, request bounds)
pass. No migrations, traffic routing, live publications or production changes.