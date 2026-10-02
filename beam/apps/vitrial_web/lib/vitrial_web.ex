defmodule VitrialWeb do
  @moduledoc """
  The HTTP boundary, and the only place the outside world is visible.

  Routing, body admission, authentication hand-off and the health surface.

  Body size is bounded here rather than only in the service layer, because a body
  is read before any service-layer code runs. The application enforces tighter
  per-path limits; this enforces the outer one, so the inner ones mean something.

  Health is not a stub that returns 200. `/health` is liveness -- the process is
  up. `/ready` is readiness -- dependencies are reachable. Collapsing them makes
  an orchestrator route traffic to an instance that cannot serve it, which is
  the failure this split exists to prevent.
  """
end
