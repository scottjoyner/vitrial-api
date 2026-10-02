defmodule VitrialSync.Application do
  @moduledoc """
  Supervision root for the sync engine.

  The child list is empty and that is the correct specification, not an omission.
  Everything `VitrialSync` implements today is pure computation over validated
  input -- cursors, canonical encoding, fingerprinting -- and a pure function
  needs no supervision. Wrapping it in a `GenServer` would add a mailbox, a state
  table and a restart policy to code that has no state and cannot fail in a way
  that supervision could recover from.

  It is a supervision root anyway, and will stay one: the slices that follow (page
  assembly, transient retry, a pooled `epgsql` connection) are exactly the kind of
  resource whose loss has to be visible and recoverable. Starting with the
  supervisor in place means those slices add a child spec instead of introducing
  supervision into an app that never had it.

  When a child is added it goes here and nowhere else, and it gets an explicit
  `shutdown` -- a connection-pool owner that is SIGKILLed rather than drained
  leaks its sockets to PostgreSQL, which does not time them out on its own.
  """

  use Application

  @impl true
  def start(_type, _args) do
    Supervisor.start_link([], strategy: :one_for_one, name: VitrialSync.Supervisor)
  end
end