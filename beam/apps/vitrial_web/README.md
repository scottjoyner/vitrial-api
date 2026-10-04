# VitrialWeb

The HTTP boundary.

  Routing, body admission, authentication hand-off, and the health surface.

  Body size is bounded at the edge as well as in the service, because a body
  is read before any service-layer code runs.

## Wave 6 — what is here

The boundary, and nothing else (per this file's promise and
`beam/README.md:37`):

  * the cowboy listener, started from `beam/config/config.exs`'s
    `VitrialWeb.Endpoint` keys; `cowboy 2.19.0` is the umbrella's first hex
    dependency, admitted with the verdict already written in
    `../vitrial-phase0/DEPENDENCY-DOCTRINE.md` §3.1/§3.2;
  * the hand-written route table: all 23 clauses of the reference surface,
    with `VitrialWeb.Handler` sequencing the four stages in the reference's
    measured order (Metrics → RequestSize → RateLimit → Correlation →
    dispatch) — the mechanism decision is documented in that module;
  * `/health` and `/api/v1/version` fully servable (pure configuration
    projections);
  * every other clause answers ONE uniform explicit 501
    `{"detail":"not implemented"}` — the wave's single invented behaviour,
    documented in `VitrialWeb.Router`'s moduledoc, replaced engine by engine
    by later waves. `/ready` and `/metrics` stay unwired on purpose: drain
    state and the metrics registry are business surface, not boundary.

No sync, ownership, auth or evidence logic crosses into this app.

## OpenAPI: the check this side cannot satisfy yet

There is **no native OpenAPI generator** on the BEAM side
(`../vitrial-phase0/DEPENDENCY-DOCTRINE.md` §2.1/§5.1; `beam/ARCHITECTURE.md`
flags the same gap). Consequences, stated rather than implied:

  * `scripts/validate_backend_contract.py` validates the pinned static pack in
    `contracts/backend/v1/` — it never calls a server — so the check itself
    still passes unchanged; it is simply not satisfied by anything this app
    produces, because this app produces no document.
  * The Python side's generated-document test
    (`tests/test_generated_openapi.py`, against `app.openapi()`) has no BEAM
    counterpart yet. Until a native generator or a shared pinned document is
    decided on, boundary parity is behavioural only: the route table in
    `VitrialWeb.Router` and the wire shapes in `VitrialWeb.Handlers` are kept
    branch-for-branch against `app/main.py` and friends, and the ExUnit suite
    pins what the wire actually does.

## Docs

Each module's `@moduledoc` records which reference file and lines it ports,
the measured behaviours it preserves, and any deliberate divergence.
