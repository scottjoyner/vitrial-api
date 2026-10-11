# SCRUM-44 preview request-body guard — source-only October 11 checkpoint

**Branch:** `research/scrum44-preview-body-guard-20261011`. Based on draft mirror calculator head `6949b1b` and free-model audit `fff1555`. No production deployment, canonical Astrix write, changed capability vocabulary or iOS build.

## Narrow delivered change

- `app/request_size.py` now recognizes exactly `POST /api/v1/items/<one-item-id>/component-calculation/preview` at the ASGI middleware path level. It limits the **request body** to **256 KiB** by default; the existing pure-ASGI middleware enforces both declared `Content-Length` and streamed bytes.
- The configuration key is `VITRIAL_MAX_BODY_BYTES_API_V1_ITEMS_COMPONENT_PREVIEW`. Invalid/nonpositive values fall back to 256 KiB. Never set arbitrary large limits without iOS fixture proof.
- Other Item endpoints remain unbounded by this **new** limit. Existing auth-pairing, sync-push and admin limits are unchanged.
- Unit and middleware tests cover exact-route selection, a true oversized header, absent/falsified headers, a streaming aggregate overrun, accepted small requests, and unrelated Item paths. An AST-only test connects the guard to the registered dynamic FastAPI route declaration.

## Explicit remaining gates

1. The 256 KiB cap is **provisional**, not derived from a proven maximum live v0.2.2 configuration. Test a canonical maximal multi-vano/multi-panel iOS fixture through the real API and tune with evidence, before approving shipment.
2. This change adds **no new rate limit**: authenticated per-principal service throttling (if needed) requires a separate threat and load review. Never apply the unauthenticated pairing's IP rate limiter to all authenticated Item routes.
3. `item.components.override` remains **ungrantable** through the V1 Capability vocabulary; this is fail-closed and should stay disabled until the API/iOS versioned contract owners decide otherwise.
4. Approved vendor catalog and quantity-rule publications remain absent; fabricated cut sizes and System 7-44 rules are prohibited.
5. Canonical Astrix API, migrated Postgres HTTP verification, Swift request/response parity and physical iPhone save/reopen acceptance remain separate. Source-level ASGI tests are **not** evidence for those gates.
6. Review request rejection-code telemetry for the preview route and confirm canonical per-Item authorization before release.

**Test command:** `python3 -m pytest -q tests/test_scrum44_preview_body_guard.py tests/test_configurator_component_calculator.py`. These tests require no database or provider calls. Prior mirrored Postgres tests were not rerun in this isolated worktree.
