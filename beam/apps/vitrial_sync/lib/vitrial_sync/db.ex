defmodule VitrialSync.Db do
  @moduledoc """
  The single database boundary for the sync engine.

  Everything above this module — the push walk's transaction wrapper, the pull
  page queries, the readiness gate — talks to PostgreSQL through the four names
  here and the row codecs beside them. Nothing above sees a wire type, a driver
  term, a connection pid, or a pool pid. That boundary is not aesthetic: it is
  what keeps the dependency doctrine's swap rule (`vitrial-phase0/DEPENDENCY-DOCTRINE.md
  §4.2`/§5.2) a one-module change instead of a rewrite — demonstrated in
  practice when the epgsql admission was superseded, in place, by the native
  `VitrialSync.Db.Pgconn` client and only `Conn` changed above the wire. If the
  query surface ever outgrows the hand-written client, a driver yields *inside*
  `Conn` and `Pool`, and the rest of this estate recompiles unchanged.

  The four names:

    * `with_conn/2` — check a connection out, run `fun` with it, guarantee the
      check-in (including on raise/throw). This is `session_scope()`
      (`app/db.py:65-67`), made explicit.
    * `health/0` — pool stats plus one bounded liveness ping, for the readiness
      gate a later slice wires up.
    * `drain/1` — stop granting connections and give the checked-out ones the
      remaining shutdown budget to come home. Called at the start of shutdown,
      before the supervisor kills its children.
    * `VitrialSync.Db.Pool` — the supervision child; started by
      `VitrialSync.Application`, not by callers.

  The row layer sits beside these, not behind them: `VitrialSync.Db.Rows` turns
  driver result maps into plain maps and canonical structs, and
  `VitrialSync.Db.Writes` turns `%VitrialSync.Batch.Effects{}` into literal SQL +
  parameters. There is no ORM, no schema DSL, and no migration runner here — the
  doctrine rejects Ecto for exactly that reason, and the argument holds: this
  service's writes are hand-rolled state machines (`app/ownership.py`, 1,098
  lines) that changesets would fight, not help.
  """

  @doc """
  Check a connection out of the pool, run `fun` with it, check it back in.

  Returns `fun`'s own return value on success, or `{:error, reason}` when no
  connection could be had: `{:error, :pool_timeout}` after the pool timeout,
  `{:error, :draining}` once `drain/1` has been called, or `{:error, reason}`
  when the connect itself failed. Raises (or throws, or exits) whatever `fun`
  raises, after checking the connection back in.
  """
  @spec with_conn(GenServer.server(), (term() -> result)) :: result | {:error, term()}
        when result: var
  defdelegate with_conn(pool, fun), to: VitrialSync.Db.Pool

  @doc "Pool statistics plus one bounded liveness ping of an idle connection."
  @spec health(GenServer.server()) :: map()
  defdelegate health(pool), to: VitrialSync.Db.Pool

  @doc """
  Stop granting connections; checked-out ones get `timeout_ms` to come home.

  Returns `{:ok, outstanding}` with the number of connections still checked out
  at the moment draining began.
  """
  @spec drain(GenServer.server(), pos_integer()) :: {:ok, non_neg_integer()}
  defdelegate drain(pool, timeout_ms), to: VitrialSync.Db.Pool
end
