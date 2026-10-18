# SCRUM-64 — company-isolated evidence downloads and browser privacy

**October 18, 2026. Integration review only; no production release.**

## Change

Previous evidence HEAD/GET endpoints already fetch `EvidenceBlob` using
`(authenticated organization ID, document ID)` and call canonical
`require_item_access`. This slice protects the HTTP delivery surface:

- HEAD, GET and PUT success responses emit `Cache-Control: private, no-store`
  and `X-Content-Type-Options: nosniff`.
- GET streams evidence as `Content-Disposition: attachment` with
  `Content-Security-Policy: sandbox`. This is deliberate even for image/HTML
  evidence: arbitrary customer-uploaded documents must not become trusted
  application web content.
- The existing binary streaming contract, SHA-256 header, MIME type, per-company
  evidence storage identity and ACL enforcement are unchanged.
- New PostgreSQL integration regression checks two real synthetic tenants
  sharing the **same document and item identifiers**, independently authorized
  data retrieval and SHA identities, coworker with restricted project scope
  rejected (403), unauthenticated access (401), nonexistent evidence (404),
  and a **revoked active token** (401). All synthetic blobs use an in-memory
  store. No actual customer blobs are loaded, stored, or distributed.

## Test and deployment gates

- Python 3.12 pinned environment on x1-370, PostgreSQL 17 bound only to
  `127.0.0.1`, Alembic revision `0008`.
- Combined tenant/evidence/sync PostgreSQL suite: **82 passed, 0 skipped**.
- Full suite including disposable PostgreSQL integrations:
  **480 passed, 1 skipped** (skipped external dependency not counted).
- Code reviewed/committed on descendant of SCRUM-47, SCRUM-48 and SCRUM-64
  tenant security. Exact isolated HTTP staging smoke and private Git bundle
  are recorded in the final Jira comment.
- Any one-shot Uvicorn process is restricted to loopback; kill after testing.
  The local database container must be stopped/removed after acceptance.

## Wider open-issue dependency plan (do not confuse testing with release)

1. **SCRUM-48/59/64/47/63:** canonical source integration, full negative
   customer/job/item/quotation/reference/pricing/evidence authorization matrix,
   external tenant switching, invitation/grants and revoked-member evidence.
2. **SCRUM-44/50/51/52/60/61/66/68:** authoritative company finished-item
   catalog and BOM materials, versioned pricebook provenance, 16 source-backed
   product templates versus 20 Villa Camila quotation lines, COP totals.
3. **SCRUM-29/39/40/41/42:** independent DBA/data-custodian/infra/release
   approval of the selected VM101 PostgreSQL backup; isolated networkless
   recovery, full WAL/customer record and backup-custody validation. Only
   after that a separate reversible VM100 API/DB, S3, TLS and ingress cutover.
4. **SCRUM-49/57/65/69/43:** exact signed iOS build and physical-device
   three-user end-to-end acceptance, offline drafts/relaunch and sync.

The canonical `astrix-group-accounts/server-api` repository is inaccessible
for authenticated writes from the last observed Fleet Git/GitHub connections.
A local commit and synthetic staging pass do NOT imply an upstream branch,
hosted CI success, production API deployment, TestFlight or customer approval.

**Production database/VM101/VM100/customer records and iOS release remain HOLD.**
