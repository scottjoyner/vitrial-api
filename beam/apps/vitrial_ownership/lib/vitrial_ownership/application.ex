defmodule VitrialOwnership.Application do
  @moduledoc """
  Supervision root for vitrial ownership.

  The child list is empty, and canonical ownership resolution is a pure function of server state.

  no pool, no cache, nothing to restart. The next slice adds a read-through of canonical
  ownership rows, and that read is the first thing here with a failure mode worth a
  supervisor.
  """

  use Application

  @impl true
  def start(_type, _args) do
    Supervisor.start_link([], strategy: :one_for_one, name: VitrialOwnership.Supervisor)
  end
end
