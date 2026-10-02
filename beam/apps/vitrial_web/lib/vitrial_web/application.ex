defmodule VitrialWeb.Application do
  @moduledoc """
  Supervision root for vitrial web.

  The child list is empty today. This is the one app in the umbrella where a
  supervised process will genuinely live, so the reason it is empty now is that
  there is nothing to supervise yet rather than that supervision was forgotten.

  It will hold the HTTP listener and the connection pool behind it, both of which
  have failure modes worth a restart policy and both of which need an explicit
  shutdown: a listener killed rather than drained drops in-flight requests, and a
  pool killed rather than drained leaks its sockets. The supervisor is in place
  before those children are, so adding them is a child spec rather than the
  introduction of supervision into an app that had none.
  """

  use Application

  @impl true
  def start(_type, _args) do
    Supervisor.start_link([], strategy: :one_for_one, name: VitrialWeb.Supervisor)
  end
end
