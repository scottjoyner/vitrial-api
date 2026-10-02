defmodule VitrialLifecycle.Application do
  @moduledoc """
  Supervision root for vitrial lifecycle.

  The child list is empty, and lifecycle transitions are pure functions over (state, event).

  lifecycle state is a column on the record, not process state. Nothing here needs a
  supervisor, and a GenServer that merely held the enum would add a process whose
  death means nothing.
  """

  use Application

  @impl true
  def start(_type, _args) do
    Supervisor.start_link([], strategy: :one_for_one, name: VitrialLifecycle.Supervisor)
  end
end
