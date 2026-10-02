defmodule VitrialAuth.Application do
  @moduledoc """
  Supervision root for vitrial auth.

  The child list is empty, and pairing and session verification are pure functions over stored hashes.

  there is no session cache to expire and no key to rotate, so there is no state to
  supervise. An in-memory revocation list, if one is ever needed, is what makes
  this app supervise something -- and it should arrive with an explicit reason for
  evicting.
  """

  use Application

  @impl true
  def start(_type, _args) do
    Supervisor.start_link([], strategy: :one_for_one, name: VitrialAuth.Supervisor)
  end
end
