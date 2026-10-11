# SCRUM-44 preview request-body guard — source-only October 11 checkpoint

**Branch:** `research/scrum44-preview-body-guard-20261011`. Based on draft mirror calculator head `6949b1b` and free-model audit `fff1555`. No production deployment, canonical Astrix write, changed capability vocabulary or iOS build.

## Narrow delivered change

- `app/request_size.py` now recognizes exactly `POST /api/v1/items/<one-item-id>/component-calculation/preview` at the ASGI middleware path level. It limits the **request body** to **2 MiB** by default; the existing pure-ASGI middleware enforces both declared `Content-Length` and streamed bytes.
- The configuration key is `VITRIAL_MAX_BODY_BYTES_API_V1_ITEMS_COMPONENT_PREVIEW`. Invalid/nonpositive values fall back to 2 MiB. Never set arbitrary large limits without iOS fixture proof.
- Other Item endpoints remain unbounded by this **new** limit. Existing auth-pairing, sync-push and admin limits are unchanged.
- Unit and middleware tests cover exact-route selection, a true oversized header, absent/falsified headers, a streaming aggregate overrun, accepted small requests, and unrelated Item paths. An AST-only test connects the guard to the registered dynamic FastAPI route declaration.

## Explicit remaining gates

1. The 2 MiB cap is **provisional** but exceeds a validated maximal-shape request of ~1.12 MiB from the server type definitions (256 panels, 256 sections, 64 vano segments, 256 overrides, full-length string fields). This is not yet the canonical maximal **iOS** fixture; test that exact Swift payload through the real API before approving shipment.
2. This change adds **no new rate limit**: authenticated per-principal service throttling (if needed) requires a separate threat and load review. Never apply the unauthenticated pairing's IP rate limiter to all authenticated Item routes.
3. `item.components.override` remains **ungrantable** through the V1 Capability vocabulary; this is fail-closed and should stay disabled until the API/iOS versioned contract owners decide otherwise.
4. Approved vendor catalog and quantity-rule publications remain absent; fabricated cut sizes and System 7-44 rules are prohibited.
5. Canonical Astrix API, migrated Postgres HTTP verification, Swift request/response parity and physical iPhone save/reopen acceptance remain separate. Source-level ASGI tests are **not** evidence for those gates.
6. Review request rejection-code telemetry for the preview route and confirm canonical per-Item authorization before release.

**Test command:** `python3 -m pytest -q tests/test_scrum44_preview_body_guard.py tests/test_configurator_component_calculator.py`. These tests require no database or provider calls. Prior mirrored Postgres tests were not rerun in this isolated worktree.

## Independent adversarial review corrections

- A schema-valid maximal **ASCII** request using 256 structural sections, 256 glass panels, 64 vano segments, 256 overrides (each with a 2,048-character reason), and full-length identifiers serialized to **1,175,718 bytes**. The original 256 KiB default would reject it. The provisional **2 MiB** cap is measured against that server-side shape, not the as-yet-unverified Swift request envelope. Large Unicode strings can be larger.
- **Verified routing bypass in first draft:** Starlette's route regex matches before a trailing newline decoded from `%0A`; the first body guard did not. The guard now covers this exact route variant. An independent Starlette `compile_path` parity test and an oversized ASGI request regression test protect the boundary.
- The optional environment override is usable in the **source process**, but the current checked-in `deploy/compose*.yml` files enumerate environment keys and **do not pass this new variable through**. Production operator override availability remains unverified until explicit deployment-manifest review.
- The legacy generic `scripts/check_guard_prefixes.py` audits only its declared prefix tuple, not this specialized dynamic path. Focused tests now check the registered route decorator, router prefix, `app.include_router` registration, and selected Starlette route-path behavior. A canonical full-route guard inventory is still a release-review follow-up.
