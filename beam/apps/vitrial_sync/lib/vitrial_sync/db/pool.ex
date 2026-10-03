defmodule VitrialSync.Db.Pool do
  @moduledoc """
  The hand-written connection pool: checkout, check-in, health, recycle,
  overflow, drain. The `db_connection`-shaped pool the doctrine promised
  (`vitrial-phase0/DEPENDENCY-DOCTRINE.md §4.2`), sized against SQLAlchemy's
  `QueuePool` + `pool_pre_ping` semantics (`app/db.py:16-58`).

  ## Lazy by contract — this is why the gate stays green

  `init/1` reads the configuration and starts with **zero connections: no
  connect, no ping, no I/O of any kind.** Connections open on first checkout,
  growing to `pool_size` persistent plus at most `max_overflow` transient. This
  mirrors `create_async_engine`'s laziness (`app/db.py:61`) and it is a hard
  requirement, not a preference: the pool is a child of `VitrialSync.Application`,
  and Mix starts mod-declared applications under `mix test` — so the pool boots
  in EVERY test run, including the gate container, which has no PostgreSQL and
  possibly no `DATABASE_URL`. An eager warm-up would crash or stall every lane
  this repository runs. `pool_test.exs`' boot-without-a-database test is the
  permanent guard against someone adding a warm-up later.

  ## Where I/O happens, and under what bound

  The pool process performs exactly one kind of I/O: `VitrialSync.Db.Conn.connect/2`,
  bounded by epgsql's 5,000 ms connect timeout (`epgsql_cmd_connect.erl:91`).
  Every query — and the health ping — runs in a CALLER process, never here, so
  no query can stall the pool, and no caller can stall the others beyond one
  bounded connect. The health ping is additionally bounded by its own deadline
  (`VitrialSync.Db.Conn.ping/2`): epgsql's synchronous commands are
  `gen_server:call(..., infinity)` and a silently-dropped socket would otherwise
  hang the pinging process forever.

  ## The checkout trap, and the API that hides it

  A plain `GenServer.call(pool, :checkout, timeout)` that times out raises
  `exit({:timeout, ...})` IN THE CALLER — it does not return an error tuple.
  `checkout/2` catches that exit and translates it to `{:error, :pool_timeout}`;
  `with_conn/3` is the only shape most callers should touch.

  `with_conn/3` runs `fun` with the connection and guarantees the check-in on
  every path — return, raise, throw, exit. On a connection that DIED while `fun`
  held it, `with_conn/3` retries the operation ONCE on a fresh connection before
  surfacing the original failure. One retry, never more: a second failure is a
  pattern, and masking patterns is how double-writes happen.

  ## Crash absorption, on every edge

  The pool traps exits and owns every connection it opened, so each failure edge
  is handled here rather than taking the pool or a caller down:

    * **connection dies while parked** — `{'EXIT', conn, _}` removes it from the
      idle set and the freed capacity serves the queue;
    * **connection dies while checked out** — the pool reclaims the slot, and
      the holder's next driver call fails with a `noproc` exit, which
      `with_conn/3` converts into its one bounded retry;
    * **owner crashes while WAITING** — the waiter carries a monitor begun at
      enqueue; `{:DOWN, ...}` evicts it;
    * **owner dies between grant and first use** (reply lost) — the `DOWN`
      handler closes that connection, because its session state is unknown, and
      restores the capacity;
    * **owner crashes while HOLDING a connection** — same: close, never
      re-park; only the owner knows what its session left open.

  ## Recycle, overflow, null pool, drain

    * **Recycle** — at check-in, a connection older than
      `DATABASE_POOL_RECYCLE_SECONDS` (`app/settings.py:32-35`: below the ~10
      minute idle timeout of managed PostgreSQL and of connection-breaking
      intermediaries) is closed instead of re-parked.
    * **Overflow** — up to `max_overflow` transient connections beyond
      `pool_size`; a transient connection CLOSES on check-in instead of
      re-parking (SQLAlchemy's `max_overflow` semantics).
    * **Null pool** — `DATABASE_NULL_POOL=true`: connect-per-checkout, close on
      check-in, no parking, no pinging (`app/db.py:52-57`; pre-ping is pointless
      without reuse). Waiters cannot exist in this mode: every checkout opens.
    * **Drain** — `drain/2` is the ENTRY POINT, deliberately not
      `terminate/2`: once `terminate/2` begins, the process no longer runs
      `handle_call`, so no caller could ever be told `{:error, :draining}` from
      inside it. `drain/2` flips the flag while the process keeps serving (new
      checkouts are refused, check-ins keep working), gives the checked-out
      connections `timeout_ms` to come home, then sweeps. `terminate/2` does
      only the final sweep — on supervisor shutdown OTP delivers the parent's
      exit to the trapped process as termination, so the two coexist.

  ## Stats

  `health/1` returns the pool's shape (idle, checked out, total, overflow,
  waiters, draining, null-pool mode) plus one on-demand ping of an idle
  connection, for the readiness gate a later slice wires in. The ping runs in
  the CALLING process under the bounded deadline, never in the pool.
  """

  use GenServer

  alias VitrialSync.Db.{Config, Conn}

  @enforce_keys [:config]
  defstruct [
    :config,
    :idle,
    :checked_out,
    :waiters,
    draining?: false
  ]

  @type conn :: pid()

  # One bounded retry after a connection death, per operation. Named because it
  # is a correctness knob: zero retries surfaces transient connection crashes to
  # callers; more than one starts hiding systematic failures behind retry storms.
  @max_connection_failures 1

  # -- supervision -------------------------------------------------------------

  @doc """
  The `VitrialSync.Application` child spec. The explicit shutdown budget is the
  statement timeout (`application.ex` demands an explicit value — a
  connection-pool owner that is SIGKILLed rather than drained leaks its sockets
  to PostgreSQL, which does not time them out on its own): the shutdown path
  should call `drain/2` first, and this budget is what terminate gets if checked
  out callers do not come home.
  """
  @spec child_spec(keyword()) :: Supervisor.child_spec()
  def child_spec(opts) do
    %{
      id: Keyword.get(opts, :name, __MODULE__),
      start: {__MODULE__, :start_link, [opts]},
      restart: :permanent,
      shutdown: shutdown_budget(opts),
      type: :worker
    }
  end

  defp shutdown_budget(opts) do
    case Keyword.fetch(opts, :shutdown) do
      {:ok, value} ->
        value

      :error ->
        Config.load(Keyword.get(opts, :config_overrides, [])).statement_timeout_ms
    end
  end

  @doc """
  Start the pool. Options:

    * `:name` — registered name (default `VitrialSync.Db.Pool`)
    * `:config_overrides` — keyword passed to `Config.load/1` ahead of the
      environment (tests pin unreachable URLs and tiny sizes here)
    * `:config` — a fully built `%Config{}`, skipping the environment entirely
    * `:shutdown` — override the child-spec shutdown budget

  `init/1` performs no I/O: see the moduledoc.
  """
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @impl true
  def init(opts) do
    config =
      Keyword.get_lazy(opts, :config, fn ->
        Config.load(Keyword.get(opts, :config_overrides, []))
      end)

    # Trap exits: every connection is linked to us (epgsql_sock:start_link/0 from
    # our own connect), and a connection crash must arrive here as a handleable
    # {'EXIT', conn, reason}, never take the pool down. OTP special-cases the
    # PARENT's exit as termination, so supervision still works.
    Process.flag(:trap_exit, true)

    # ZERO connections. No connect, no ping, no I/O — see the moduledoc.
    {:ok,
     %__MODULE__{config: config, idle: :queue.new(), checked_out: %{}, waiters: :queue.new()}}
  end

  # -- public API --------------------------------------------------------------

  @doc """
  Check a connection out: `{:ok, conn, fresh?}` or `{:error, reason}`.

  `fresh?` is `true` when the connection was opened for this checkout (no health
  ping needed — `pool_pre_ping` is pointless on a brand-new socket,
  `app/db.py:54`); a parked connection should be pinged before use, which
  `with_conn/3` does. Waits up to the pool's `DATABASE_POOL_TIMEOUT_SECONDS`;
  exhaustion translates the caller-side exit into `{:error, :pool_timeout}`.
  `{:error, :draining}` after `drain/2`.
  """
  @spec checkout(GenServer.server(), keyword()) :: {:ok, conn(), boolean()} | {:error, term()}
  def checkout(pool, opts \\ []) do
    timeout_ms =
      Keyword.get_lazy(opts, :pool_timeout_ms, fn ->
        GenServer.call(pool, :pool_timeout_ms, 5_000)
      end)

    try do
      GenServer.call(pool, :checkout, timeout_ms)
    catch
      # A call timeout is an exit({:timeout, ...}) in the caller, not an error
      # tuple — the translation is the pool's whole job on this path.
      :exit, {:timeout, {GenServer, :call, [_pool, :checkout, _]}} -> {:error, :pool_timeout}
    end
  end

  @doc """
  Return a connection to the pool. `{:error, :unknown_connection}` means the
  pool already reclaimed it (it died, its owner crashed, or a drain swept it) —
  the capacity is already restored and the caller has nothing left to do.
  """
  @spec checkin(GenServer.server(), conn()) :: :ok | {:error, :unknown_connection}
  def checkin(pool, conn), do: GenServer.call(pool, {:checkin, conn}, 5_000)

  @doc """
  Throw a connection away (failed health ping, protocol surprise): close it and
  restore the capacity. Idempotent — an already-reclaimed connection is fine.
  """
  @spec discard(GenServer.server(), conn()) :: :ok
  def discard(pool, conn), do: GenServer.call(pool, {:discard, conn}, 5_000)

  @doc """
  Run `fun` with a checked-out connection, check it back in afterwards — on
  return, on raise, on throw, on exit. Returns `fun`'s value, or `{:error,
  reason}` when no connection could be had. See the moduledoc for the one
  bounded retry on a connection that died mid-operation.
  """
  @spec with_conn(GenServer.server(), (conn() -> result), keyword()) :: result | {:error, term()}
        when result: var
  def with_conn(pool, fun, opts \\ []) when is_function(fun, 1) do
    case checkout(pool, opts) do
      {:ok, conn, fresh?} ->
        run(pool, conn, fun, fresh?, @max_connection_failures, opts)

      {:error, _reason} = error ->
        error
    end
  end

  defp run(pool, conn, fun, fresh?, failures_left, opts) do
    if fresh? do
      exec(pool, conn, fun, failures_left, opts)
    else
      case Conn.ping(conn, Keyword.get(opts, :ping_timeout_ms, Conn.health_ping_timeout_ms())) do
        :ok ->
          exec(pool, conn, fun, failures_left, opts)

        {:error, why} ->
          # The one scenario pre-ping exists for: the parked socket is dead or
          # wedged. Close it, restore the slot, try once more from the top.
          discard(pool, conn)
          retry_or(pool, fun, failures_left, opts, fn -> {:error, {:connection_lost, why}} end)
      end
    end
  end

  defp exec(pool, conn, fun, failures_left, opts) do
    outcome =
      try do
        {:returned, fun.(conn)}
      catch
        kind, thrown -> {:caught, kind, thrown, __STACKTRACE__}
      end

    case outcome do
      {:returned, value} ->
        settle(pool, conn)
        value

      {:caught, kind, thrown, stack} ->
        alive? = Process.alive?(conn)
        settle(pool, conn)

        if alive? do
          # The connection survived; the failure belongs to fun. The check-in
          # guarantee holds on this path too — then it flies.
          rekindle(kind, thrown, stack)
        else
          retry_or(pool, fun, failures_left, opts, fn -> rekindle(kind, thrown, stack) end)
        end
    end
  end

  # Check in a connection that may have died under us. A dead connection is not
  # check-in-able — the pool already reclaimed it — so notify-and-move-on.
  defp settle(pool, conn) do
    if Process.alive?(conn) do
      checkin(pool, conn)
    else
      discard(pool, conn)
    end
  end

  # The retry budget. `fallback` is the ORIGINAL failure, re-raised or returned
  # once the one permitted retry is spent — never a new wrapper error.
  defp retry_or(pool, fun, failures_left, opts, fallback) do
    if failures_left > 0 do
      case checkout(pool, opts) do
        {:ok, conn, fresh?} ->
          run(pool, conn, fun, fresh?, failures_left - 1, opts)

        {:error, _reason} = error ->
          error
      end
    else
      fallback.()
    end
  end

  defp rekindle(:error, value, stack), do: reraise(value, stack)
  defp rekindle(:throw, value, _stack), do: throw(value)
  defp rekindle(:exit, value, _stack), do: exit(value)

  @doc """
  Pool shape plus one bounded liveness ping of an idle connection, in THIS
  process (never in the pool's). `ping: :no_idle_connection` when nothing is
  parked — including every null-pool and all-checked-out moment, which is the
  honest answer and not an error.
  """
  @spec health(GenServer.server()) :: map()
  def health(pool) do
    stats = GenServer.call(pool, :stats, 5_000)

    case Map.pop(stats, :idle_conn) do
      {nil, stats} -> Map.put(stats, :ping, :no_idle_connection)
      {conn, stats} -> Map.put(stats, :ping, Conn.ping(conn, Conn.health_ping_timeout_ms()))
    end
  end

  @doc """
  Drain: refuse new checkouts (`{:error, :draining}`), keep serving check-ins,
  and give checked-out connections `timeout_ms` to come home — the child's
  remaining shutdown budget. Returns `{:ok, outstanding}` with the count still
  checked out when draining began. At the deadline (or immediately, when there
  is nothing outstanding) everything still open is swept and queued waiters are
  told `{:error, :draining}`.
  """
  @spec drain(GenServer.server(), non_neg_integer()) :: {:ok, non_neg_integer()}
  def drain(pool, timeout_ms) when is_integer(timeout_ms) and timeout_ms >= 0 do
    GenServer.call(pool, {:drain, timeout_ms}, timeout_ms + 5_000)
  end

  # -- server ------------------------------------------------------------------

  @impl true
  def handle_call(:pool_timeout_ms, _from, s), do: {:reply, s.config.pool_timeout_ms, s}

  def handle_call(:stats, _from, s) do
    {:reply,
     %{
       pool_size: s.config.pool_size,
       max_overflow: s.config.max_overflow,
       idle: :queue.len(s.idle),
       checked_out: map_size(s.checked_out),
       total: total(s),
       overflow: overflow(s),
       waiters: :queue.len(s.waiters),
       draining?: s.draining?,
       null_pool?: s.config.null_pool,
       idle_conn: peek_idle_conn(s)
     }, s}
  end

  def handle_call(:checkout, from, s) do
    if s.draining? do
      {:reply, {:error, :draining}, s}
    else
      grant_or_queue(from, s)
    end
  end

  def handle_call({:checkin, conn}, _from, s) do
    case Map.pop(s.checked_out, conn) do
      {nil, _} ->
        # Already reclaimed: a connection death or an owner crash beat us here.
        {:reply, {:error, :unknown_connection}, s}

      {{_owner, ref, opened_at}, s1} ->
        Process.demonitor(ref, [:flush])

        cond do
          # Null pool: close on check-in, never park (app/db.py:52-57).
          # Recycle: close anything past its age instead of re-parking it.
          # Transient: overflow conns close on check-in, by definition.
          s1.config.null_pool or expired?(opened_at, s1) or transient?(s1) ->
            Conn.close(conn)
            {:reply, :ok, serve_waiters(s1)}

          true ->
            {:reply, :ok, serve_waiters(park(conn, opened_at, s1))}
        end
    end
  end

  def handle_call({:discard, conn}, _from, s) do
    case Map.pop(s.checked_out, conn) do
      {nil, _} ->
        {:reply, :ok, s}

      {{_owner, ref, _opened_at}, s1} ->
        Process.demonitor(ref, [:flush])
        Conn.close(conn)
        {:reply, :ok, serve_waiters(s1)}
    end
  end

  def handle_call({:drain, budget_ms}, _from, s) do
    outstanding = map_size(s.checked_out)
    s = %{s | draining?: true}

    cond do
      outstanding > 0 and budget_ms > 0 ->
        Process.send_after(self(), :drain_deadline, budget_ms)
        {:reply, {:ok, outstanding}, s}

      true ->
        {:reply, {:ok, outstanding}, sweep(s)}
    end
  end

  # -- capacity events -----------------------------------------------------------

  defp grant_or_queue(from, s) do
    if s.config.null_pool do
      # Null pool has no bounds and no queue: open per checkout or fail.
      open_and_grant(from, s)
    else
      case :queue.out(s.idle) do
        {{:value, {conn, opened_at}}, rest} ->
          s1 = %{s | idle: rest}

          if expired?(opened_at, s1) do
            Conn.close(conn)
            open_and_grant(from, s1)
          else
            grant(conn, false, opened_at, from, s1)
          end

        # NOTE: the empty arm yields the residual QUEUE, not our state — the
        # state is unchanged when there was nothing to pop.
        {:empty, _queue} ->
          open_and_grant(from, s)
      end
    end
  end

  defp open_and_grant(from, s) do
    if total(s) < s.config.pool_size + s.config.max_overflow do
      case Conn.connect(s.config.url, statement_timeout_ms: s.config.statement_timeout_ms) do
        {:ok, conn} ->
          grant(conn, true, now_ms(), from, s)

        {:error, reason} ->
          # The refused connect happened in here (bounded 5s), so the caller
          # sees the failure directly instead of hanging on the queue.
          {:reply, {:error, reason}, s}
      end
    else
      # At capacity: queue. The caller's GenServer.call deadline (the pool
      # timeout) is the waiter's bound; a timed-out caller exits, and its DOWN
      # evicts the entry.
      owner = elem(from, 0)
      ref = Process.monitor(owner)
      {:noreply, %{s | waiters: :queue.in({owner, ref, from}, s.waiters)}}
    end
  end

  # Hand a connection to the checkout at `from`. The monitor begun at enqueue
  # (or taken fresh for a direct caller) is the OWNERSHIP monitor from here on:
  # a `{:DOWN, ...}` for it means the owner died holding the connection.
  defp grant(conn, fresh?, opened_at, from, s) do
    owner = elem(from, 0)
    ref = Process.monitor(owner)
    s = %{s | checked_out: Map.put(s.checked_out, conn, {owner, ref, opened_at})}
    GenServer.reply(from, {:ok, conn, fresh?})
    {:noreply, s}
  end

  defp park(conn, opened_at, s), do: %{s | idle: :queue.in({conn, opened_at}, s.idle)}

  # On any capacity restore (check-in, reclaim, discard): serve the queue. One
  # pass; each grant consumes exactly one capacity unit and each capacity event
  # freed exactly one, so the queue cannot grow here.
  defp serve_waiters(s) do
    case :queue.out(s.waiters) do
      {:empty, _} ->
        s

      {{:value, {owner, ref, from}}, rest} ->
        s = %{s | waiters: rest}

        cond do
          # The DOWN for this waiter may still be in our mailbox: a grant
          # decision checks liveness, not just queue membership.
          not Process.alive?(owner) ->
            Process.demonitor(ref, [:flush])
            serve_waiters(s)

          true ->
            case take_connection(s) do
              {:ok, conn, fresh?, opened_at, s1} ->
                s2 = %{s1 | checked_out: Map.put(s1.checked_out, conn, {owner, ref, opened_at})}
                GenServer.reply(from, {:ok, conn, fresh?})
                serve_waiters(s2)

              :none ->
                # Capacity went elsewhere (a racing drain sweep, a failed
                # connect): back at the FRONT, served by the next capacity event
                # or by the caller's own timeout.
                %{s | waiters: :queue.in_r({owner, ref, from}, s.waiters)}
            end
        end
    end
  end

  # Idle-first, then open — the same order checkout uses. `fresh?` distinguishes
  # a brand-new connection (no ping owed) from a parked one (ping owed).
  defp take_connection(s) do
    case :queue.out(s.idle) do
      {{:value, {conn, opened_at}}, rest} ->
        s1 = %{s | idle: rest}

        if expired?(opened_at, s1) do
          Conn.close(conn)
          take_connection(s1)
        else
          {:ok, conn, false, opened_at, s1}
        end

      # Empty arm: residual queue, state unchanged.
      {:empty, _queue} ->
        if total(s) < s.config.pool_size + s.config.max_overflow do
          case Conn.connect(s.config.url, statement_timeout_ms: s.config.statement_timeout_ms) do
            {:ok, conn} -> {:ok, conn, true, now_ms(), s}
            {:error, _reason} -> :none
          end
        else
          :none
        end
    end
  end

  # -- async events ---------------------------------------------------------------

  @impl true
  def handle_info(:drain_deadline, %{draining?: true} = s), do: {:noreply, sweep(s)}
  def handle_info(:drain_deadline, s), do: {:noreply, s}

  # A connection process died — a crash. (The closes we initiate stop the
  # process normally, so a trapped EXIT here is always an unplanned death.)
  def handle_info({:EXIT, conn, _reason}, s) do
    cond do
      Map.has_key?(s.checked_out, conn) ->
        {{_owner, ref, _opened_at}, s1} = Map.pop(s.checked_out, conn)
        Process.demonitor(ref, [:flush])
        # Session state unknown; capacity restored; the holder finds out when
        # its next driver call exits noproc, and with_conn retries once.
        {:noreply, serve_waiters(s1)}

      true ->
        case drop_idle(s, conn) do
          {true, s1} -> {:noreply, serve_waiters(s1)}
          {false, s1} -> {:noreply, s1}
        end
    end
  end

  def handle_info({:DOWN, ref, :process, _pid, _reason}, s) do
    case find_by_owner_ref(s.checked_out, ref) do
      {conn, _owner} ->
        # Granted, then the owner died before using it: the reply is lost, the
        # session state is unknown, so the connection closes instead of parking.
        Conn.close(conn)
        s1 = Map.delete(s.checked_out, conn)
        {:noreply, serve_waiters(%{s | checked_out: s1})}

      nil ->
        case drop_waiter(s, ref) do
          {true, s1} -> {:noreply, s1}
          {false, s1} -> {:noreply, s1}
        end
    end
  end

  @impl true
  def terminate(_reason, s) do
    # Final sweep only: the drain flag had its chance to answer callers while
    # the process still ran handle_call. Closes are casts — they cannot hang on
    # a wedged socket (VitrialSync.Db.Conn.close/1).
    sweep(s)
    :ok
  end

  # Close everything, refund every queued waiter, reset the structures.
  defp sweep(s) do
    Enum.each(:queue.to_list(s.idle), fn {conn, _opened_at} -> Conn.close(conn) end)

    Enum.each(s.checked_out, fn {conn, {_owner, ref, _opened_at}} ->
      Process.demonitor(ref, [:flush])
      Conn.close(conn)
    end)

    reply_waiters_draining(s.waiters)

    %{s | idle: :queue.new(), checked_out: %{}, waiters: :queue.new()}
  end

  defp reply_waiters_draining(waiters) do
    case :queue.out(waiters) do
      {:empty, _} ->
        :ok

      {{:value, {_owner, ref, from}}, rest} ->
        Process.demonitor(ref, [:flush])
        GenServer.reply(from, {:error, :draining})
        reply_waiters_draining(rest)
    end
  end

  # -- state helpers ---------------------------------------------------------------

  defp total(s), do: :queue.len(s.idle) + map_size(s.checked_out)

  # Transient connections live only while checked out (they close on check-in),
  # so overflow is whatever the total carries past pool_size.
  defp overflow(s), do: max(0, total(s) - s.config.pool_size)

  # A connection checked back in while the pool is already at (or above)
  # pool_size persistent slots was opened as overflow: it closes.
  defp transient?(s), do: total(s) >= s.config.pool_size

  defp expired?(_opened_at, s) when s.config.recycle_ms <= 0, do: false
  defp expired?(opened_at, s), do: now_ms() - opened_at >= s.config.recycle_ms

  defp now_ms, do: System.monotonic_time(:millisecond)

  defp peek_idle_conn(s) do
    case :queue.peek(s.idle) do
      {:value, {conn, _opened_at}} -> conn
      :empty -> nil
    end
  end

  defp drop_idle(s, conn) do
    {matching, rest} =
      s.idle
      |> :queue.to_list()
      |> Enum.split_with(fn {c, _opened_at} -> c == conn end)

    case matching do
      [] -> {false, s}
      [_entry] -> {true, %{s | idle: :queue.from_list(rest)}}
    end
  end

  defp find_by_owner_ref(checked_out, ref) do
    Enum.find_value(checked_out, fn {conn, {owner, r, _opened_at}} ->
      if r == ref, do: {conn, owner}
    end)
  end

  defp drop_waiter(s, ref) do
    {matching, rest} =
      s.waiters
      |> :queue.to_list()
      |> Enum.split_with(fn {_owner, r, _from} -> r == ref end)

    case matching do
      [] ->
        {false, s}

      [_dead] ->
        Process.demonitor(ref, [:flush])
        {true, %{s | waiters: :queue.from_list(rest)}}
    end
  end
end
