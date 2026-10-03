defmodule VitrialSync.Application do
  @moduledoc """
  Supervision root for the sync engine.

  Everything `VitrialSync` implemented through wave 4 is pure computation over
  validated input — cursors, canonical encoding, fingerprinting — and a pure
  function needs no supervision, so the child list stayed empty while that was
  the whole estate. Wave 5 adds the first real resource: the PostgreSQL
  connection pool (`VitrialSync.Db.Pool`), which is exactly the kind of child
  this supervisor existed to carry — its loss has to be visible and recoverable,
  and a `one_for_one` restart re-reads the environment and reopens lazily.

  The pool gets an explicit `shutdown`: a connection-pool owner that is SIGKILLed
  rather than drained leaks its sockets to PostgreSQL, which does not time them
  out on its own. The budget is the statement timeout
  (`DATABASE_STATEMENT_TIMEOUT_MS`, 15,000 ms by default) — the same bound the
  reference puts on a query — and it is what `VitrialSync.Db.Pool.drain/2` should
  be granted before the supervisor walks its children.

  When another child is added it goes here and nowhere else, and it gets an
  explicit `shutdown` too.
  """

  use Application

  alias VitrialSync.Db.Pool

  @impl true
  def start(_type, _args) do
    children = [
      # {Module, arg} resolves through Pool.child_spec/1, which reads the
      # environment for the shutdown budget — the same env read the pool's own
      # init performs, at the same boot moment (pydantic-settings semantics).
      {Pool, []}
    ]

    Supervisor.start_link(children, strategy: :one_for_one, name: VitrialSync.Supervisor)
  end
end
