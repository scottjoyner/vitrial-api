# SCRUM-44 preview deployment environment and iOS size gate — October 11, 2026

**Base:** draft mirror API `85f4d1d`; **scope:** four deployment templates and tests only. No running container, NAS, client or production setting was changed.

## Safe environment-contract change

- Production and acceptance Compose shared `x-api-environment` pass through `VITRIAL_MAX_BODY_BYTES_API_V1_ITEMS_COMPONENT_PREVIEW` with the same **2,097,152-byte (2 MiB)** fallback as `app/request_size.py`.
- Both `.env.example` and `deploy/env.production.example` document the exact variable and size. Both Compose files pass `docker compose config --no-interpolate --quiet`.
- Existing auth, sync and admin body limits are unchanged. The override does **not** authorize `item.components.override` or create a new rate limit. The Caddy edge default `4MB` exceeds the preview's default size; increasing the app override past the edge limit requires independent edge review.

## Cross-repo request sizing evidence and remaining gate

The iOS [SCRUM-44 PR #149](https://github.com/scottjoyner/vitrialuminios-del-valle-ios/pull/149) head `f80df1455e18ee3866ae40dec063d2528a3abe64` includes `Sources/VitrialAluminiosKit/ConfiguratorComponentPreviewClient.swift`, where `requestData.count <= 512 * 1_024` is enforced. The corresponding `ConfiguratorComponentPreviewRequestWireV1.swift` omits `overrides` from its request, and sends the pinned IDs/digests plus a `configuration` object.

These are distinct constraints:
- The server accepts at most **2 MiB** by default. Its synthetic ASCII maximal-shape fixture is **1,175,718 bytes**, exceeding the iOS client's current 512 KiB send limit.
- The actual Swift encoder's 512 KiB limit **does not make 2 MiB server acceptance a contract violation**; it narrows what today's client can send. A request over 512 KiB must be handled on the client (no request sent).
- No exact-head physical iOS request, authoritative published vendor rules, signed production identity, or cross-repo immutable fixture has yet been validated here. A Python-generated body cannot substitute for a Swift-encoded actual request.
- Before Gate F / v0.2.2 promotion: export a deterministic Swift JSON fixture from the exact proposed iOS source; require its SHA-256 and byte count to be checked in Swift tests and in the API `ComponentCalculationPreviewRequest` test under the *correct* environment. Verify the same pins, measurements, sections/panels, item ID and stage, with negative over-limit and cross-org cases.

## Validation

`python3 -m pytest -q tests/test_scrum44_preview_deploy_contract.py tests/test_scrum44_preview_body_guard.py tests/test_configurator_component_calculator.py` — **36 passed**, Ruff E/F green. `docker compose ... config --no-interpolate --quiet` — both passed. This is a **local draft deployment-configuration gate**, not a production deployment.
## First actual Swift serialization bridge (after initial deployment check)

A **real Swift JSON encoder output**, generated from local iOS commit `0d49a04`, is stored as `contracts/scrum44/scrum44_component_preview_swift_wire_20261011.json`. Exactly **1,229 bytes** are transmitted; the fixture file adds one trailing newline. SHA-256 of the original transmitted bytes: `818e8f982742a6086a35b99c54992ce1150dc0b649f9e4956816a88c7aeaf94d`.

The API test `test_scrum44_swift_wire_fixture.py` exercises **the real Swift-encoded input**, using the pure `CalculationInput` model and static source checks of the preview request envelope. It verifies the 512 KiB iOS ceiling, the 2 MiB backend ceiling, centimeters-to-millimeters conversion in the populated fields, corner vane provenance, per-panel references, pinned digests, negative dangling sections and zero width. **This is not full FastAPI request-model or authenticated HTTP coverage**: SQLAlchemy is absent from this sandbox, so importing the API route to validate `ComponentCalculationPreviewRequest` was not possible. The exact GitHub PR #149 head also remains separately untested.

**Validation at this checkpoint:** **43 Python focused tests** passed, Ruff E/F passed, production and acceptance Compose parse checks passed. The paired iOS isolated source branch passed **10 relevant Swift tests**.

## Independent free-model review and regression repairs

A bounded `opencode/space-bunny-free` session reviewed this exact branch at `5dd3ac6` without editing any files. It confirmed the request-only scope and exposed three test false-positives, now repaired:

- The Compose default is compared with the **actual source constant**, while a separately pinned 2 MiB invariant guards against an unreviewed cap change.
- The effective `caddy.environment.CADDY_MAX_REQUEST_BODY` fallback is checked in **both** Compose files, not just an `.env.example`. This is still an offline default-only check, not an override-capacity proof.
- The preview guard test now composes `APIRouter(prefix=...)` with the actual `@router.post(...)` decorator and verifies the resulting path is size-limited. The request envelope AST test requires the **exact** declared fields, including server-only optional `overrides`.

Focused Python suite: **46 passed**, Ruff E/F passed. The Swift isolated branch separately passed **all 639 Swift package tests**. These findings are local static/source evidence; live auth, signed vendor rules, an actual HTTP response, exact GitHub PR-head parity and iPhone acceptance remain unverified.

**Next remaining authority gate:** validate a real server-generated `ComponentCalculationPreviewResponse` from independently authorized, tenant-scoped vendor publications against the exact Swift response model and frozen request. The synthetic fixture below proves structure only, not publication or manufacturing authority.

## Synthetic server-calculator response ↔ Swift receipt adoption checkpoint

The same immutable Swift request fixture is now fed into the **pure server-owned `calculate_components` function** with an explicitly synthetic two-item catalog and fictional per-item rules. Script: `scripts/generate_scrum44_test_response.py`. It produces `contracts/scrum44/scrum44_component_preview_fixture_response_20261011.json`; the response envelope is constructed in a test-only helper because importing the live route requires unavailable SQLAlchemy dependencies.

- Exactly **1,720 JSON wire bytes** (fixture file has one trailing newline); SHA-256 over wire bytes `c27005406f20b96ace0dafe945682144f7d21bc0211bfed6e85668f8b8e72e10`.
- Calculation input digest `d259d0f774dd22d28b461d0973221e1f9e71f43bd291f9745e5828b56f4499f1`. Both component lines use `fixture-only-` rule IDs, fictional quantities, no supplier SKU, and no actual publication authority.
- The stored response is byte-equal to a fresh generator run. The nested receipt passes `CalculationReceipt.model_validate` and retains the same Item/configuration identity and independent digest.
- The paired local iOS worktree decodes **that exact response JSON** into `ComponentPreviewWireV1` and successfully adopts it as a read-only `InternalComponentCalculationReceipt` against the same Swift configuration. This is a fake offline response with caller-pinned hashes; it does not attest the server or authorize manufacturing.
- **51 focused Python tests** and the isolated Swift source fixture/adoption tests pass at this checkpoint. The full Swift package suite was previously green at 639 tests; rerun for a precise total after adding the new test.

**Hard stop:** Real tenant-authenticated HTTP, approved immutable vendor publications, full `ComponentCalculationPreviewResponse` Pydantic route validation, exact GitHub PR-head parity, physical iPhone acceptance, quotation and production authorization remain unproven.
