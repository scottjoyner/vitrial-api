# SCRUM-51 — Job-specific finished-Item draft contract

**October 18, 2026 — implementation review; NOT an approved catalog or production release.**

## What changed

The existing canonical Item sync handler already enforces Customer → Project →
ProjectSector ownership and an optimistic server revision. This slice adds a
**strict, optional** `jobItemDraft` sub-document to Item V1/V2 mutations.

The validated sub-document supports:
- A required Item/Project/ProjectSector identity matching **the server's
  canonical owning relationship**.
- A job-local Spanish item label, location, reference shorthand, positive
  integer quantity, and optional manually entered opening measurements in
  centimeters (rectangular width/height or corner/L-shaped two legs).
- An optional template identity **paired with an explicit template version**,
  always marked `pending_customer_review`. A custom Item can omit both.
- Zero to 32 independent component descriptors: glass, aluminum, steel,
  hardware, accessory, other; optional positive decimal-string quantity
  and matching unit. Material components do not become warehouse inventory,
  purchase orders, automatically calculated fabrication BOMs or supplier costs.
- Exact, bounded validation for names/IDs, positive finite dimensions,
  component IDs, precision and numeric units. Any supplied unit price,
  vendor SKU, stock quantity, fabricated manufacturing BOM, or
  false `approved` claim is **rejected**.
- Existing Item payloads **without** `jobItemDraft` remain valid. Tombstones
  retain existing referential integrity controls. V1/V2 mutation success still
  passes through the existing session, capability, tenant and revision gates.

The server intentionally does **not** resolve a template ID to a globally
published catalog or grant source approval. It only validates that an
optional provenance reference is internally consistent in an Item draft.
Independent Alejandro/customer review is still required before any of the
16 Villa Camila source groups can be approved for catalog publication.

## Evidence

- `tests/test_scrum51_finished_item_draft.py`: synthetic acceptance and
  adversarial unit checks for geometry, pending approval, three material
  types, duplicate components, unsupported supplier/warehouse/cost fields,
  mismatched Item/Project/Sector identities, malformed quantities, custom
  Items and legacy Item compatibility.
- Opt-in private fixture check **reads but does not commit** the existing
  16-group/20-row Villa Camila historical fixture. File SHA-256 is pinned:
  `07d6452d29e175bea666a66f3f76f0c1099a101e2a5cc4ccf00dc1f1e75e97c1`.
  It checks only 16 template **names and references** with
  `pending_customer_review`, not customer quotation line values or prices.
- `tests/test_scrum51_item_draft_postgres.py`: real FastAPI ASGI V2,
  disposable migrated PostgreSQL 17/Alembic `0008`, synthetic identities,
  two separate companies sharing the same canonical Item ID, create/edit,
  rejected invalid vendorSKU, rejected stale revision, independent V2 pull
  streams and anonymous denial. Sync pulls can legitimately return
  **multiple change-log revisions** for an Item, so the tests verify every
  delivered revision instead of assuming one snapshot.
- **Focused** 28 passed, 0 skipped. **Full cross-feature suite with
  disposable PostgreSQL** 535 passed, 1 skipped. The one skip and known
  schema-name/deprecation warnings are not release acceptance.
- No production migrations, customer PDFs, price book, real database, live
  ingress, iOS build or Proxmox changes performed.

## Outstanding work and dependency order

1. **SCRUM-50**: independently compare exact 16 Spanish finished-item names
   and structural references against the original 8-page source PDF; obtain
   customer/Alejandro approval, source hash custody and publish only reviewed
   company-scoped catalog versions. This work does *not* publish templates.
2. **SCRUM-51**: implement iOS selection/editor, local offline draft persistence,
   add/edit/remove of job-specific components, UI confirmation on delete,
   cold relaunch, actual conflict resolution, and immutable quoted snapshots.
   The server contract and synthetic multi-user sync proof are only one slice.
3. **SCRUM-52 / 68**: join canonical Item references to the existing opt-in
   priced 20-line quotation, its 16 subtotals and COP 64,245,000 *historical
   fixture*; independent paper/PDF source and commercial signoff pending.
   Do not promote historic Item quote prices as global supplier costs.
4. **SCRUM-47/48/59/62/63/64**: canonical server-api upstream integration,
   full tenant and role negative matrix, exact-head hosted CI and revocation.
5. **SCRUM-29/39/40/41/42**: independently signed networkless VM101
   PostgreSQL archive restore + WAL/data integrity then reversible
   production API/DB/S3/TLS release; **HOLD** until separate approvals.
6. **SCRUM-49/57/65/69**: exact signed TestFlight device and real
   three-seat/offline/online customer acceptance; **HOLD**.

The canonical GitHub repo `astrix-group-accounts/server-api` remained
inaccessible via connected GitHub and Fleet HTTPS authentication at the
last checkpoint. A locally committed branch, isolated staging deploy or
verified private Git bundle must not be described as an upstream PR/merge,
customer acceptance, or production deployment.
