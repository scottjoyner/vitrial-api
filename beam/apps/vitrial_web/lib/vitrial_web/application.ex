defmodule VitrialWeb.Application do
  @moduledoc """
  Supervision root for vitrial web.

  The child list is what this wave exists to fill in: the HTTP listener and
  the two process-backed pieces of the request path the boundary owns. All
  three have failure modes worth a restart policy and all three need an
  explicit shutdown:

    * `VitrialWeb.Metrics` -- the counters the outermost instrumentation stage
      records into. Restartable trivially (a fresh registry loses counters,
      which is the same degradation the reference documents for a broken
      metrics layer: no metric, never no response).
    * `VitrialWeb.RateLimit` -- the sliding-window limiter. Its buckets are
      in-process state by design (app/rate_limit.py:36-39); a restart re-arms
      the window, which is the honest in-process cost of a crash, not a
      policy change.
    * `VitrialWeb.Listener` -- the cowboy/ranch listener, whose child spec
      carries the explicit non-brutal shutdown: a listener KILLED rather than
      drained drops in-flight requests, so its shutdown is timed (drain
      before drop), and a pool killed rather than drained leaks its sockets
      (the pool arrives with a later wave; the supervisor is in place before
      it is).
  """

  use Application

  @impl true
  def start(_type, _args) do
    children = [
      VitrialWeb.Metrics,
      VitrialWeb.RateLimit,
      VitrialWeb.Listener
    ]

    Supervisor.start_link(children, strategy: :one_for_one, name: VitrialWeb.Supervisor)
  end
end
