defmodule VitrialReference.Application do
  @moduledoc """
  Supervision root for vitrial reference.

  The child list is empty, and a manifest GET is a read, and must stay one.

  it did write: it re-ensured baseline publications on every call, which is why
  publication_manifest had to be split away from the two seeding paths on the Python
  side. Nothing here needs supervising. Re-ensuring baselines is a migration's job,
  not a reader's.
  """

  use Application

  @impl true
  def start(_type, _args) do
    Supervisor.start_link([], strategy: :one_for_one, name: VitrialReference.Supervisor)
  end
end
