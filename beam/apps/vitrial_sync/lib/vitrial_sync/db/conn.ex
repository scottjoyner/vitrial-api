defmodule VitrialSync.Db.Conn do
  @moduledoc """
  One PostgreSQL connection: open it, prove it is alive, close it.

  This module and `VitrialSync.Db.Pool` are the only places in the estate that
  may call `:epgsql` directly — that is the dependency-doctrine boundary
  (`vitrial-phase0/DEPENDENCY-DOCTRINE.md §4.2`), and it is what keeps the
  epgsql-to-postgrex switch a one-module change.

  ## The server-side timeouts, re-achieved as post-connect SETs

  `app/db.py:25-44` sends `statement_timeout` and
  `idle_in_transaction_session_timeout` as asyncpg `server_settings`, i.e. in the
  startup packet. **epgsql 4.8.0 has no startup-parameter hook**: the startup
  packet carries only user, database, replication and application_name
  (`deps/epgsql/src/commands/epgsql_cmd_connect.erl:59-78`). The same two GUCs
  are therefore applied as post-connect `SET`s, one statement each, before the
  connection is considered open. If a `SET` fails the connection is closed and
  the connect is reported failed — a connection running without its statement
  timeout is the pool-exhaustion failure mode `app/settings.py:36-40` exists to
  prevent, and must never be handed out quietly.

  The connect is bounded: epgsql applies a 5,000 ms default connect timeout
  (`epgsql_cmd_connect.erl:91`), carried here explicitly.

  ## The health ping is bounded — this is a design decision, not a detail

  Every one of epgsql's synchronous commands calls `gen_server:call(...,
  infinity)` (`deps/epgsql/src/epgsql_sock.erl:132-133`; same for
  `get_parameter`/`set_async_receiver`/`get_cmd_status` at `:144-151`). The 5,000
  ms timeout above bounds the CONNECT only. On a silently-dropped idle socket —
  exactly the scenario `pool_pre_ping` and `pool_recycle` exist for, short of an
  RST — `squery(conn, "SELECT 1")` would hang forever, and no server-side
  `statement_timeout` can bound it because no server is answering.

  So `ping/2` never runs the query in the interested process: it spawns an
  unlinked throwaway process that issues the `SELECT 1`, and waits on it under a
  deadline (`@health_ping_timeout_ms`). On deadline the throwaway is killed and
  the caller gets `{:error, :ping_timeout}`. The dead socket is then closed by
  whoever owned the ping. Without that bound, one black-holed connection would
  hang a caller forever — or, if the ping ran inside the pool `GenServer`, hang
  the *entire pool* and every later checkout behind it.

  ## Closing cannot hang

  `:epgsql.close/1` is `catch gen_server:cast(C, stop), ok`
  (`deps/epgsql/src/epgsql.erl`) — an async cast, immune to a wedged socket. The
  pool's terminate sweep and drain sweep rely on that: they fire closes and
  return, they do not wait on a dead socket.
  """

  alias VitrialSync.Db.Url

  # The liveness deadline, in milliseconds. Named here because it is a real
  # bound with a real cost: a healthy ping round-trip is sub-millisecond on a
  # LAN, so 2,000 ms is two orders of magnitude of slack, and a ping slower than
  # this is a connection worth closing and replacing rather than waiting on. It
  # is not part of the deployed env surface (the seven DATABASE_* names are the
  # contract the compose files set); if it ever needs tuning in the field, that
  # is a deliberate env addition, not a quiet default change.
  @health_ping_timeout_ms 2_000
  @connect_timeout_ms 5_000

  @gucs [
    # Server-side ceiling, so a pathological query is killed by PostgreSQL
    # rather than holding the connection until the client gives up
    # (app/db.py:26-34, app/settings.py:36-40).
    {"statement_timeout", :statement_timeout_ms},
    # Bound idle-in-transaction sessions too: an abandoned transaction holds
    # its snapshot and blocks vacuum (app/db.py:36-43).
    {"idle_in_transaction_session_timeout", :statement_timeout_ms}
  ]

  @doc """
  Open a connection and apply the server-side session settings.

  Returns `{:ok, conn}` or `{:error, reason}` (the raw epgsql connect error, or
  `{:error, {:guc_failed, name, reason}}` when a post-connect `SET` failed — the
  connection is closed before returning in that case, never handed out half
  configured).
  """
  @spec connect(Url.t(), keyword()) :: {:ok, pid()} | {:error, term()}
  def connect(%Url{} = url, opts \\ []) do
    timeout_ms = Keyword.get(opts, :connect_timeout_ms, @connect_timeout_ms)

    epgsql_opts = %{
      host: String.to_charlist(url.host),
      port: url.port,
      username: String.to_charlist(url.username),
      password: String.to_charlist(url.password),
      database: String.to_charlist(url.database),
      timeout: timeout_ms
    }

    # Linked to the caller (epgsql_sock:start_link/0, deps/epgsql/src/epgsql.erl):
    # callers of this function must be prepared to handle a connection crash via
    # {'EXIT', ConnPid, Reason} — the pool traps exits for exactly this. On
    # connect failure epgsql_sock stops normally, so a refused connection is
    # returned as a plain {:error, reason}, not as an exit.
    with {:ok, conn} <- :epgsql.connect(epgsql_opts),
         :ok <- apply_gucs(conn, opts) do
      {:ok, conn}
    else
      {:error, _reason} = error -> error
    end
  end

  defp apply_gucs(conn, opts) do
    with :ok <- set_gucs(conn, opts),
         :ok <- pin_timezone(conn) do
      :ok
    else
      {:error, {name, reason}} ->
        close(conn)
        {:error, {:guc_failed, name, reason}}
    end
  end

  defp set_gucs(conn, opts) do
    Enum.reduce_while(@gucs, :ok, fn {name, opt_key}, :ok ->
      case Keyword.fetch(opts, opt_key) do
        {:ok, value} when is_integer(value) and value >= 1 ->
          case squery_ok?(conn, "SET #{name} = '#{value}ms'") do
            :ok -> {:cont, :ok}
            {:error, reason} -> {:halt, {:error, {name, reason}}}
          end

        _ ->
          # Absent or invalid timeout setting: refuse to open unconfigured.
          {:halt, {:error, {name, :missing_or_invalid}}}
      end
    end)
  end

  # `SET timezone = 'UTC'`, with its TRUE justification — the corrected fact,
  # stated so the old wrong one cannot come back: epgsql's *binary* timestamptz
  # decode is pure epoch arithmetic with no timezone input
  # (deps/epgsql/src/epgsql_idatetime.erl:25, `i2timestamp(N)`), so binary
  # decode is timezone-INDEPENDENT and this pin does nothing for it. What the
  # pin buys is TEXT-format determinism: a `squery` read, a log line, a psql
  # debugging session, or any server-side rendering of a timestamp comes out
  # identically on every node instead of tracking the server host's timezone.
  # Verified against a server with timezone=America/New_York: the same stored
  # instant decoded identically from a default and a UTC-pinned session, while
  # TEXT reads differed ("2026-01-01 07:00:00-05" vs "2026-01-01 12:00:00+00").
  defp pin_timezone(conn) do
    case squery_ok?(conn, "SET timezone = 'UTC'") do
      :ok -> :ok
      {:error, reason} -> {:error, {"timezone", reason}}
    end
  end

  # Single-statement squery only: a multi-statement string returns a LIST of
  # responses, and this module never issues one. (squery responses are TEXT
  # format — raw binaries, never driver-decoded terms. Nothing here consumes
  # their values; they are only checked for the error arm.)
  defp squery_ok?(conn, sql) do
    case :epgsql.squery(conn, sql) do
      {:error, _} = error -> {:error, error}
      _ -> :ok
    end
  end

  @doc """
  The bounded liveness ping: `SELECT 1` under a deadline.

  Returns `:ok`, or `{:error, reason}` — `{:error, :ping_timeout}` when the
  deadline passed (the throwaway process is killed; the socket is then closed by
  the caller, since it is exactly the kind of connection this check exists to
  catch), the server's error tuple when the query failed, or
  `{:error, {:ping_crashed, reason}}` when the throwaway died. See the
  moduledoc for why the deadline is not optional.
  """
  @spec ping(pid(), pos_integer()) :: :ok | {:error, term()}
  def ping(conn, timeout_ms \\ @health_ping_timeout_ms) when is_pid(conn) do
    me = self()

    {worker, ref} =
      spawn_monitor(fn ->
        case :epgsql.squery(conn, "SELECT 1") do
          {:ok, _columns, _rows} -> send(me, {:ping, :ok})
          {:ok, _count} -> send(me, {:ping, :ok})
          {:error, _} = error -> send(me, {:ping, error})
          other -> send(me, {:ping, {:error, {:unexpected_ping_reply, other}}})
        end
      end)

    receive do
      {:ping, :ok} ->
        Process.demonitor(ref, [:flush])
        :ok

      {:ping, {:error, _} = error} ->
        Process.demonitor(ref, [:flush])
        error

      {:DOWN, ^ref, :process, ^worker, reason} ->
        {:error, {:ping_crashed, reason}}
    after
      timeout_ms ->
        Process.exit(worker, :kill)
        Process.demonitor(ref, [:flush])
        {:error, :ping_timeout}
    end
  end

  @doc "The health-ping deadline, exposed for tests and the pool's accounting."
  @spec health_ping_timeout_ms() :: pos_integer()
  def health_ping_timeout_ms, do: @health_ping_timeout_ms

  @doc """
  Close a connection. This cannot hang: `:epgsql.close/1` is a cast
  (`deps/epgsql/src/epgsql.erl`), so the drain and terminate sweeps fire closes
  at a wedged socket and return. Calling this on an already-dead pid is fine.
  """
  @spec close(pid()) :: :ok
  def close(conn) when is_pid(conn) do
    :epgsql.close(conn)
    :ok
  end
end
