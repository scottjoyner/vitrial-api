defmodule VitrialDelivery.Application do
  @moduledoc """
  Supervision root for vitrial delivery.

  The child list is empty, and each delivery machine is a pure transition.

  the state lives in the database where the canonical record already is, so there is
  nothing here to supervise. When a worker appears -- outbox dispatch, webhook
  delivery -- it is added here with an explicit shutdown, because a worker killed
  rather than drained leaves its notification half-sent.
  """

  use Application

  @impl true
  def start(_type, _args) do
    Supervisor.start_link([], strategy: :one_for_one, name: VitrialDelivery.Supervisor)
  end
end
