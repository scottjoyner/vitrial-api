---
id: VITR-DOM-001
title: "Real-World Quote Domain Mapping — Villa Camila"
owner: vitrial-api
category: DOM
status: proposed
date: 2026-10-02
---

# Real-World Quote Domain Mapping — Villa Camila

> **Source:** Villa Camila — Cotización No. 001, dated 2026-09-16, user-provided business document.
>
> **Scope:** map a real Vitrial job to the existing API/domain contracts and identify narrow future
> seams. This document does **not** change the current sync schema, authorization capabilities,
> quotation lifecycle, delivery state machine, or mutation authority.

## 1. Why this belongs in the API docs

The quotation supplies real business constraints that must eventually be enforced server-side,
not only explained by iOS copy.

It validates several existing decisions:

- approved quotation is an immutable commercial handoff;
- delivery is a state machine, not one generic status;
- material requirements freeze before procurement;
- procurement carries supplier/readiness evidence;
- production and installation have separate preconditions;
- measurement/configuration history must remain revisioned.

It also exposes gaps that should be handled as targeted extensions rather than a rewrite.

## 2. Source-derived requirements

The source describes aluminum, glass, and stainless-steel architectural work across compound
window, door/window, swing-door, and railing assemblies.

Commercial/operational requirements include:

1. customer directly buys/pays materials from suppliers;
2. Vitrial may recommend suppliers and provides technical material selection/coordination plus
   labor;
3. supplier material values shown in the quote are estimates and may vary;
4. labor payment is 50% advance and 50% at completion;
5. work start requires:
   - total material availability;
   - final opening-measurement verification;
   - effective labor-advance payment;
6. one finished-structure transport is included when openings are ready;
7. extra transport caused by unavailable openings/project/customer changes is additional scope;
8. scaffolding/lifts/special access equipment are excluded/customer responsibility;
9. execution estimate is 40–45 calendar days after prerequisites are met;
10. installation is phased because openings are not all available simultaneously;
11. tempered glass can only be released to production after definitive field dimensions are
    verified;
12. later dimension/construction changes can require additional materials/work that must be
    communicated and accepted.

## 3. Existing contract fit

### Canonical ownership hierarchy

The current sync contract already carries:

\`\`\`text
customer
project
project_sector
item
measurement
evidence
customer_requirement
configuration
configuration_version
blocker
quotation
delivery_execution
\`\`\`

That is sufficient to represent the commercial/technical spine today.

### Quotation lifecycle

The API already treats quotation state/history separately from delivery execution. Preserve that
boundary. A field change after approval should never rewrite an approved quotation in place.

### Delivery lifecycle

Current canonical delivery statuses are:

\`\`\`text
engineeringReview
-> materialsRequired
-> procurement
-> production
-> readyForInstallation
-> scheduled
-> installed
-> complete
\`\`\`

This is directionally correct and should remain the quotation-level lifecycle.

### Materials plan

The existing plan requires positive quantities/units and freezes before Procurement.

### Procurement plan

The existing plan already supports:

- per-requirement readiness;
- \`unconfirmed | ordered | shortage | available\`;
- shortage quantity;
- lead time;
- expected availability;
- supplier snapshot;
- purchase/reference number;
- shortage owner;
- revision/provenance.

This maps strongly to the real quote's customer/supplier coordination workflow.

### Production plan

The existing production plan is revision-bound to frozen procurement and tracks owned production
steps. Preserve that freeze boundary.

### Measurement history

The iOS/backend entity vocabulary already includes measurement records; iOS uses
reported/confirmed/final stages. This provides the data needed for a measurement-gated release
policy without inventing a separate measurement subsystem.

## 4. Gap: customer material responsibility is implicit

The source makes a legally/commercially important distinction: the customer purchases and pays
materials directly, while Vitrial advises/coordinates and supplies labor.

Current procurement evidence records supplier and purchase details but does not make purchasing
responsibility an explicit first-class contract field.

Candidate future vocabulary:

\`\`\`json
{
  "purchasingResponsibility": "customer",
  "paymentResponsibility": "customer"
}
\`\`\`

Possible enum:

\`\`\`text
customer
vitrial
shared
third_party
\`\`\`

Do not ship this field until its ownership, defaults, migration behavior, and quotation provenance
are specified and tested.

## 5. Gap: production release can depend on final measurement

Today production readiness is primarily constrained by frozen procurement/material availability.
The real quote proves that this is not sufficient for every operation.

Explicit source-backed case:

> tempered glass must not be ordered/released to production until definitive opening measurements
> have been verified in the field.

Target policy seam:

\`\`\`text
ProductionReleaseConstraint
  id
  deliveryExecutionID
  itemID
  materialRequirementID? / productionStepID?
  requiredMeasurementStage
  measurementID
  measurementVersion
  status: blocked | satisfied
  satisfiedAt
  provenance
\`\`\`

Important: do not make \`final\` measurement a universal production prerequisite. The rule should
be attached to affected materials/operations.

### Narrow first implementation

Before adding a synced entity, implement/read-test a pure evaluator over existing canonical records:

\`\`\`text
(item/configuration + measurement + materials/procurement)
    -> [release constraint assessment]
\`\`\`

Promote the assessment to persisted canonical state only if workflow evidence requires cross-device
mutation/history.

## 6. Gap: quotation-level delivery status is too coarse for phased openings

The source explicitly says installation follows construction progress because different openings
will not be ready at the same time.

Current \`delivery_execution\` is one execution raised from one approved quotation. Do not reinterpret
its \`scheduled\` or \`installed\` state as proof that every quote line/opening has the same state.

Target layering:

\`\`\`text
delivery_execution              # quotation-level authority
  -> item/work-package progress # line/item-level operational truth
\`\`\`

Candidate item-level fields:

\`\`\`text
itemID
quotationLineID
finalMeasurementReady
materialsReady
productionState
openingReady
installationScheduledAt
installationCompletedAt
blockerSummary
evidenceRefs
\`\`\`

### Migration strategy

Start with a deterministic read model/projection from current Item + delivery plans where possible.
Introduce a new canonical child/entity only when independent per-item mutation is required.

This preserves the proven delivery machine and avoids status explosion.

## 7. Gap: opening readiness is distinct from production readiness

The quote distinguishes:

- materials available;
- structure fabricated;
- physical opening ready;
- installation schedulable.

Installation scheduling should eventually consume explicit site/opening readiness evidence rather
than infer it from production completion.

Candidate constraint:

\`\`\`text
InstallationReadiness
  itemID
  openingReady
  verifiedAt
  evidenceRefs
  actor/provenance
\`\`\`

This is especially important for avoiding additional transport caused by failed site readiness.

## 8. Commercial terms must not become inventory data

The source contains commercial terms that are not material catalog attributes:

- one included transport;
- additional transport rules;
- access-equipment exclusion;
- customer responsibility for supplier delays/defects/replacements;
- payment milestones;
- conditional schedule start.

Model these as quotation/contract terms or structured commercial policy, not \`Item\` catalog
properties and not material requirements.

Near-term rule: preserve them in immutable quotation content/provenance. Structure them only when
the product needs machine-enforced behavior.

## 9. Schedule semantics: prerequisites, not simple date arithmetic

The source's 40–45 day estimate begins after required materials, opening readiness, and effective
advance payment conditions.

Do not implement:

\`\`\`text
estimatedCompletion = quotationApprovedAt + 45 days
\`\`\`

A future schedule forecast should carry a start-eligibility boundary:

\`\`\`text
startEligible =
  depositSatisfied
  && requiredMaterialsAvailable
  && requiredFinalMeasurementsSatisfied
  && requiredSiteConditionsSatisfied
\`\`\`

The API may then compute/display an estimate relative to the eligibility timestamp.

## 10. Change-order/revision contract

The source allows field measurement and construction changes to create additional material/work.

The API already has immutable configuration versions and quotation lifecycle/revision concepts.
Use those rather than in-place edits.

Target flow:

\`\`\`text
new measurement version
-> technical impact
-> configuration revision (if needed)
-> BOM delta
-> quotation/change revision
-> approval
-> new delivery-plan revision / work package delta
\`\`\`

Every commercial delta should reference the measurement/configuration provenance that caused it.

## 11. BOM boundary

The existing architecture direction already separates:

\`\`\`text
Technical Configuration
-> Bill of Materials
-> Cost Calculation
-> Commercial Pricing
-> Quotation
\`\`\`

The real quote confirms that this separation is necessary.

A BOM should eventually express at least:

- glass area/specification;
- aluminum/profile length/specification;
- hardware quantity;
- accessories;
- fabrication operations;
- waste/allowance;
- installation labor basis.

Do not treat source quote prose as the BOM. The quote is the customer-facing commercial projection.

## 12. Historical actuals / feedback loop

The API should eventually retain immutable completed-job actuals at Item/assembly granularity:

\`\`\`text
itemID
configurationVersion
measurementVersion
catalogVersion
pricingVersion
estimated material qty/cost
actual material qty/cost
estimated labor/time
actual labor/time
estimated supplier lead time
actual supplier lead time
waste/replacements
change-order delta
delay causes
installation result
\`\`\`

This should be analytics/training evidence, not a mechanism that silently rewrites historical
quotes or current authority.

## 12A. Cross-repo implementation evidence

iOS PR #95 merged the first read-only experiment boundary described here:

- deterministic real-quote assembly fixtures;
- configuration-derived reference BOM output;
- a shared customer/AR projection tied to the same configuration revision;
- a tempered-glass final-measurement release assessment.

iOS draft PR #98 adds an ephemeral scan/opening binding and RealityKit planar preview. It binds
local scene placement to an explicit Item Measurement and the current ItemConfiguration, rejects
measurement/configuration drift, and does not persist an Opening entity.

No API entity, sync schema, capability, quotation authority, delivery transition, or production
authority changes are introduced by either slice. Treat the scan/opening result as local experiment
evidence until independent per-opening mutation/history proves a server-side entity is necessary.

## 12B. 2-D / corner topology experiment

The next cross-repo evidence slice is intentionally still **non-canonical**.

iOS stacked PR #100 introduces a read-only `AssemblySectionTopology` projection for the real
Villa Camila V3, V4, and V1 cases. The API experiment in this branch mirrors that evidence shape
with strict Pydantic validation and no route, database table, sync entity, or mutation surface.

The evidence separates:

- semantic topology (rows, columns, spans, corner legs);
- exact Item/configuration revision identity;
- source-backed constraints;
- dimensional readiness;
- missing dimensional evidence required before AR/fabrication geometry is safe.

This is important because the source establishes relationships that the current flat section list
cannot represent, but it does **not** provide every internal dimension. In particular, the
experiment must not invent V3 internal panel heights, V4 row heights/upper-leaf widths, or the
stored-section-to-corner-leg mapping for V1.

The experiment therefore keeps `assembly_topology` and `opening` out of the V1 sync
`EntityType` vocabulary. Promotion to canonical state requires the acceptance criteria below,
including ownership, offline conflict behavior, revision/freeze semantics, and backward
compatibility.

## 13. Proposed read-only experiment before any schema change

Use Villa Camila fixtures to build a pure contract prototype that answers, for each quoted Item:

1. can the existing configuration vocabulary represent the assembly?
2. what BOM would be derived?
3. which material requirements are customer-purchased?
4. which requirements need a final measurement before production release?
5. is the Item ready for production?
6. is the opening ready for installation?
7. what quote/delivery revisions would a measurement delta affect?

Output JSON only. Do not mutate canonical records.

This experiment adds evidence without widening:

- routing;
- authorization;
- sync entity types;
- quotation authority;
- procurement authority;
- production authority;
- installation authority.

## 14. Acceptance criteria for adopting new contract fields

A proposed field/entity should not enter V1/V2 canonical sync until:

- it solves a demonstrated Villa Camila workflow gap;
- iOS and API agree on ownership/parent chain;
- capability/authorization behavior is explicit;
- offline mutation/conflict behavior is defined;
- immutable/frozen-state behavior is defined;
- backward compatibility is defined;
- PostgreSQL + sync + contract tests cover it;
- old clients fail safely or ignore it safely;
- it does not weaken existing plan-freeze/provenance rules.

## 15. Privacy / source handling

The business source contains banking/payment destination information. That information is **not**
needed for domain modeling and is intentionally not copied into this repository document.

The reference case should retain business rules and non-secret technical examples, not banking or
credential-like data.
