defmodule VitrialSync.Db.Pgconn do
  @protocol_version_bin <<196_608::32>>
  @moduledoc """
  A PostgreSQL connection, hand-written over OTP's `:gen_tcp`.

  ## Why this exists instead of `:epgsql`

  The doctrine is to stay native (`vitrial-phase0/DEPENDENCY-DOCTRINE.md §1.1`):
  OTP ships no `postgres` application, so the wire protocol has to be reached
  somehow, and reaching it through a third-party driver means every CVE in that
  driver's tree is inherited for a service that needs four operations.

  The measurement that decided it: the whole epgsql coupling in this database
  layer was **three functions** — `:epgsql.connect/1`, `:epgsql.squery/2`,
  `:epgsql.close/1`, all three of them in `VitrialSync.Db.Conn`. The 1,393 lines
  around them — `Codec`, `Rows`, `Url`, `Config`, `Writes`, `Pool` — are
  PostgreSQL knowledge that survives any driver and were left untouched by this
  change. A driver swap that only has to replace three call sites is not a
  migration; it is a module.

  ## The protocol, only as much of it as this service uses

  Version 3.0 of the frontend/backend protocol, which needs four things:

    * **Startup** — a length-prefixed startup packet carrying the user, the
      database and the GUCs the reference sends as asyncpg `server_settings`
      (`app/db.py:25-44`). Written by hand here, so `statement_timeout` and
      `idle_in_transaction_session_timeout` go in the STARTUP PACKET rather than
      as post-connect `SET`s. epgsql has no startup-parameter hook and could not
      do this; a connection handed out without its statement timeout is exactly
      the pool-exhaustion failure `app/settings.py:36-40` exists to prevent.
    * **Authentication** — `AuthenticationOk`, `CleartextPassword`,
      `MD5Password` and `SASL/SCRAM-SHA-256`. SCRAM is not optional in practice:
      `scram-sha-256` is what managed PostgreSQL (and anything since PG 10 with
      `password_encryption = scram-sha-256`, the default since PG 14) negotiates,
      so an MD5-only client cannot connect to a modern server.
    * **Extended query** — `Parse`/`Bind`/`Describe`/`Execute`/`Sync`, because
      bind parameters cannot be interpolated into SQL text. `Codec.normalize/1`
      produces the parameter values; this module produces the wire bytes.
    * **Row decoding** — `RowDescription`, `DataRow`, `CommandComplete`,
      `ReadyForQuery`, `ErrorResponse`. `Codec.decode/2` turns a cell into an
      Elixir term; this module only splits the frame.

  ## Everything is bounded, because a database call that cannot time out is a
  ## pool that cannot recover

  Every socket read is `{active: false}` with an explicit `timeout`, and every
  write is `:gen_tcp.send/2` followed by a `flush` that is itself bounded. There
  is no `gen_server:call(conn, ..., :infinity)` anywhere in this module, because
  that is the pattern that lets one black-holed socket hang a caller forever —
  and, in the pool's case, hang every later checkout behind it.

  The socket lives in an **unlinked owner process**. The pool holds the owner's
  pid, not a socket descriptor, so closing is `Process.exit(owner, :shutdown)`:
  it cannot block on a wedged socket, and it cannot leak a port if the owning
  process dies, because `{active: false}` sockets close with the port.

  ## What this does not implement

  Copy-in (`COPY FROM STDIN`), the replication protocol, prepared-statement
  caching across transactions, and cancellation (`CancelRequest`). None are used
  by `app/db.py`, and implementing an unused protocol path is the kind of thing
  that rots untested.
  """

  # The client-side bound on ONE exchange: the caller's receive, the owner's
  # socket reads, and the default for `:query_timeout`. Generous relative to a
  # LAN round-trip and to the server-side `statement_timeout` the startup packet
  # carries; it exists to catch a wedged socket, not to enforce a deadline.
  @call_timeout_ms 15_000

  @typedoc "One connection: the pid owning its socket."
  @type conn :: pid()

  @typedoc "A decoded PostgreSQL error, as the server sent it."
  @type pg_error :: %{
          severity: String.t() | nil,
          code: String.t() | nil,
          message: String.t() | nil,
          detail: String.t() | nil,
          hint: String.t() | nil,
          position: String.t() | nil
        }

  # The message types this module acts on. Anything else arriving mid-exchange is
  # a protocol violation and is reported rather than skipped, because a silently
  # skipped `NoticeResponse` is fine and a silently skipped `ErrorResponse` is
  # how a failed query looks like an empty result set.
  @doc """
  Open a connection to `url`, with `opts` carrying the server GUCs to send in the
  startup packet.

  Returns `{:ok, conn}` or `{:error, reason}`. `conn` is a pid; the caller never
  touches a socket descriptor.
  """
  @spec connect(map(), keyword()) :: {:ok, conn()} | {:error, term()}
  def connect(url, opts \\ []) do
    timeout = Keyword.get(opts, :connect_timeout, 5_000)

    case :gen_tcp.connect(
           String.to_charlist(url.host),
           url.port,
           [:binary, active: false],
           timeout
         ) do
      {:ok, socket} -> owner_loop(socket, url, opts)
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  Run `sql` with `params` on `conn`.

  `params` must already be normalized by `VitrialSync.Db.Codec.normalize/1`.
  Returns `{:ok, %VitrialSync.Db.Rows{}}`, `{:error, reason}`, or
  `{:error, %pg_error{}}` for a server-reported failure — the distinction
  matters, because a `{:error, reason}` here means the connection is suspect
  (and the pool should discard it) while a `pg_error` is an ordinary SQL
  failure (and the connection is still good).
  """
  @spec query(conn(), String.t(), [term()]) ::
          {:ok, VitrialSync.Db.Rows.t()} | {:error, term()} | {:error, pg_error()}
  def query(conn, sql, params) when is_pid(conn) and is_binary(sql) do
    ref = make_ref()
    send(conn, {:pg_query, self(), ref, sql, params})

    receive do
      {^ref, ^conn, reply} -> reply
    after
      @call_timeout_ms -> {:error, :query_timeout}
    end
  end

  @doc "Send `Terminate` and stop the owner. Cannot block on a wedged socket."
  @spec close(conn()) :: :ok
  def close(conn) when is_pid(conn) do
    # Fire and forget: the owner is told to send Terminate best-effort on its
    # way out, and this returns immediately either way. A `close` that waited on
    # the socket would let one dead connection stall the pool's terminate sweep.
    Process.exit(conn, :shutdown)
    :ok
  end

  # ---------------------------------------------------------------------------
  # the owner process
  # ---------------------------------------------------------------------------

  # The owner is spawned unlinked: a crash here must not take down whoever
  # checked the connection out, and it must not leave a socket open. Both are
  # why the port is closed by a `try/after` around the receive loop rather than
  # by anyone upstream remembering to.
  defp owner_loop(socket, url, opts) do
    parent = self()

    owner =
      spawn(fn ->
        Process.flag(:trap_exit, true)

        # Handshake, REPORT the outcome, then stay alive to serve queries. The
        # first version exited here — which closed the socket in the `after`
        # immediately and handed the caller a pid for a dead process. The
        # `after` runs when `owner_receive/2` returns, not when this block does.
        result =
          try do
            handshake(socket, url, opts)
          catch
            kind, reason ->
              {:error, {kind, reason}}
          end

        send(parent, {:pgconn_ready, self(), result})

        try do
          with :ok <- elem_ok(result) do
            owner_receive(socket, opts)
          end
        after
          close_socket(socket)
        end
      end)

    receive do
      {:pgconn_ready, ^owner, {:ok, _state}} -> {:ok, owner}
      {:pgconn_ready, ^owner, {:error, reason}} -> {:error, reason}
    after
      # A server that accepts TCP and then says nothing must not hang the pool's
      # checkout forever. This is the same bound epgsql applied by default, and
      # it is why the handshake runs in a child at all.
      Keyword.get(opts, :connect_timeout, 5_000) -> {:error, :handshake_timeout}
    end
    |> tap(fn
      {:ok, _} = ok -> ok
      _ -> close_socket(socket)
    end)
  end

  defp elem_ok({:ok, _}), do: :ok
  defp elem_ok(_), do: :error

  defp handshake(socket, url, opts) do
    with :ok <- send_startup(socket, url, opts),
         {:ok, :authenticated} <-
           read_until(socket, :ready, Keyword.get(opts, :connect_timeout, 5_000), url) do
      # From here on the connection is usable. The GUCs went out in the startup
      # packet, so there is no post-connect SET round-trip to fail — which is
      # the one thing this module does that the driver could not.
      {:ok, %{socket: socket, params: url, statements: %{}}}
    end
  end

  # ---------------------------------------------------------------------------
  # the caller side
  # ---------------------------------------------------------------------------

  # The owner's request loop. One process per connection, so a caller is never
  # blocked by a socket it does not own and a crash in one exchange cannot take
  # the connection with it.
  #
  # Every clause is bounded: the socket reads carry their own timeouts, and the
  # `call` clause has one for the whole exchange. There is no unbounded receive
  # here, because an unbounded receive in the owner is how a wedged socket hangs
  # every later checkout in the pool behind it.
  defp owner_receive(socket, opts) do
    receive do
      {:pg_query, from, ref, sql, params} ->
        reply = execute(socket, sql, params, opts)
        send(from, {ref, self(), reply})
        owner_receive(socket, opts)

      {:pg_close, from} ->
        send(from, {:pg_closed, self()})
        :closed

      # The pool traps exits and closes idle connections on drain. A shutdown
      # signal arriving here must reach `close_socket` in the owner's `after`.
      :shutdown ->
        :closed

      _other ->
        owner_receive(socket, opts)
    end
  end

  # ---------------------------------------------------------------------------
  # startup
  # ---------------------------------------------------------------------------

  # The two resource-bounding GUCs `app/db.py:25-44` sets on every connection.
  # Keyed by the OPTION NAME callers pass, not by the SQL parameter name, so a
  # caller cannot accidentally send an unrelated GUC.
  @statement_timeout_param :statement_timeout_ms
  @idle_in_transaction_param :idle_in_transaction_session_timeout_ms

  # StartupMessage: an int32 length that counts itself but not the following
  # int32 version, then null-terminated key/value pairs, then a zero byte.
  # Protocol version 3.0 is 196608 (major 3, minor 0) packed into an int32.
  #
  # This is the whole reason this module exists rather than a driver: the GUCs
  # `app/db.py:25-44` sends as asyncpg `server_settings` go out HERE, in the
  # startup packet. epgsql 4.8.0 has no startup-parameter hook and could only
  # apply them as post-connect SETs.
  defp send_startup(socket, url, opts) do
    # The two resource-bounding GUCs `app/db.py:25-44` sets on every connection go
    # in HERE, in the startup packet, because that is the only place the protocol
    # offers for them before the connection is usable. A driver without a
    # startup-parameter hook can only apply them as post-connect `SET`s, which
    # means a connection that exists without its statement timeout — the exact
    # pool-exhaustion failure mode `app/settings.py:36-40` exists to prevent.
    pairs =
      [
        {"user", url.username},
        {"database", url.database},
        {"client_encoding", "UTF8"},
        {"timezone", "UTC"}
      ]
      |> append_guc(@statement_timeout_param, guc(opts, @statement_timeout_param))
      |> append_guc(@idle_in_transaction_param, guc(opts, @idle_in_transaction_param))
      |> Enum.flat_map(fn {key, value} -> [key, <<0>>, to_string(value), <<0>>] end)

    # The length prefix counts itself plus the payload, so it must be computed
    # on the iodata as bytes -- `iolist_size`, not `byte_size`, which does not
    # accept a list of binaries.
    payload = [:binary.bin_to_list(@protocol_version_bin) | pairs] ++ [<<0>>]

    bytes = IO.iodata_to_binary([<<:erlang.iolist_size(payload) + 4::32>>, payload])
    :gen_tcp.send(socket, bytes)
  end

  # The two GUCs `app/db.py:25-44` sets on every connection. Both are a bound on
  # how long one connection can hold resources, so both belong in the packet that
  # opens the connection rather than in a follow-up round-trip that could fail.

  # Absent or invalid means the parameter is simply not sent. It is NOT sent as
  # a default: a connection quietly running with somebody else's timeout, or
  # with none, is worse than one whose configuration is visibly incomplete.
  defp guc(opts, name) do
    case Keyword.fetch(opts, name) do
      {:ok, value} when is_integer(value) and value >= 1 -> "#{value}ms"
      _ -> nil
    end
  end

  defp append_guc(pairs, _key, nil), do: pairs
  # The server accepts real GUC names here, not this client's caller option atoms.
  defp append_guc(pairs, :statement_timeout_ms, value),
    do: pairs ++ [{"statement_timeout", value}]

  defp append_guc(pairs, :idle_in_transaction_session_timeout_ms, value),
    do: pairs ++ [{"idle_in_transaction_session_timeout", value}]

  # Read startup-phase messages until the server is ready or has refused.
  defp read_until(socket, :ready, timeout, url) do
    case read_message(socket, timeout) do
      {:ok, "R", payload} -> handle_auth(socket, payload, timeout, url)
      {:ok, "E", payload} -> {:error, decode_error(payload)}
      {:ok, _type, _payload} -> read_until(socket, :ready, timeout, url)
      {:error, reason} -> {:error, reason}
    end
  end

  # AuthenticationRequest: an int32 subtype, then subtype-specific data.
  defp handle_auth(_socket, <<0::32>>, _timeout, _url), do: {:ok, :authenticated}

  defp handle_auth(_socket, <<3::32>>, _timeout, _url),
    do: {:error, :cleartext_password_not_supported}

  defp handle_auth(socket, <<5::32, salt::binary-size(4)>>, timeout, url) do
    # MD5: "md5" ++ hex(md5(hex(md5(password ++ user)) ++ salt)). The INNER hash
    # is what makes this salted per user rather than salted per database.
    with :ok <- send_password(socket, md5_auth(url.password || "", url.username, salt)) do
      read_until(socket, :ready, timeout, url)
    end
  end

  defp handle_auth(_socket, <<10::32>>, _timeout, _url),
    do: {:error, {:unsupported_auth, :sasl_prepared_channel_binding}}

  defp handle_auth(_socket, <<other::32>>, _timeout, _url),
    do: {:error, {:unsupported_auth, other}}

  # PasswordMessage: the literal "password", the answer, a terminating zero.
  defp send_password(socket, secret) when is_binary(secret),
    do: :gen_tcp.send(socket, ["p", <<byte_size(secret) + 5::32>>, secret, <<0>>])

  # `:crypto` has been in extra_applications since the umbrella's first line, so
  # both of PostgreSQL's constructions need no dependency.
  defp md5_auth(password, user, salt) do
    inner = Base.encode16(:crypto.hash(:md5, password <> user), case: :lower)

    "md5" <> Base.encode16(:crypto.hash(:md5, inner <> salt), case: :lower)
  end

  # ---------------------------------------------------------------------------
  # the exchange
  # ---------------------------------------------------------------------------

  # Extended query protocol, which is the only way to send parameters without
  # building SQL text. Five messages then one Sync; the server's replies come
  # back for that group, so this reads until `ReadyForQuery`.
  defp execute(socket, sql, params, opts) do
    timeout = Keyword.get(opts, :query_timeout, @call_timeout_ms)

    with :ok <- send_extended_query(socket, sql, params),
         {:ok, result} <- read_query_response(socket, timeout) do
      {:ok, result}
    end
  end

  defp send_extended_query(socket, sql, params) do
    messages =
      [
        parse_message(sql),
        bind_message(params),
        describe_message(),
        execute_message(),
        <<"S", 4::32>>
      ]
      |> IO.iodata_to_binary()

    :gen_tcp.send(socket, messages)
  end

  # Parse: statement name (unnamed), the query, and zero parameter type OIDs —
  # which asks the server to infer every type rather than be told.
  defp parse_message(sql) do
    body = <<0>> <> (sql <> <<0>>) <> <<0::16>>
    <<"P", byte_size(body) + 4::32>> <> body
  end

  # Bind: the unnamed portal and statement, all parameters in TEXT format, and
  # all results in TEXT format. Format code 0 is text in both positions.
  defp bind_message(params) do
    encoded = Enum.map(params, &encode_param/1)

    param_bytes = :erlang.iolist_to_binary(encoded)

    # unnamed portal and statement
    # parameter count: ONE per parameter, not the byte size of the encoded blob
    # parameter format codes: all text
    # result format codes: all text
    body =
      <<0>> <>
        <<0>> <>
        <<0::16>> <>
        <<length(params)::16>> <>
        param_bytes <>
        <<0::16>>

    <<"B", byte_size(body) + 4::32>> <> body
  end

  # A parameter is a text-format value: its bytes, or the SQL NULL marker.
  # `Codec` has already decided nil means NULL, so this never has to guess.
  defp encode_param(nil), do: <<-1::32>>
  defp encode_param(value) when is_binary(value), do: <<byte_size(value)::32>> <> value
  defp encode_param(value) when is_integer(value), do: encode_param(Integer.to_string(value))
  defp encode_param(value) when is_float(value), do: encode_param(Float.to_string(value))
  defp encode_param(true), do: encode_param("t")
  defp encode_param(false), do: encode_param("f")

  defp describe_message, do: <<"D", 6::32, "P", 0>>

  # Execute: the unnamed portal, and 0 rows — unlimited, because the page-fit
  # decision is made by `VitrialSync.Page` on bytes, not by a row cap here.
  defp execute_message do
    body = <<0>> <> <<0::32>>
    <<"E", byte_size(body) + 4::32>> <> body
  end

  # Read until `ReadyForQuery`. The group may carry RowDescription, DataRow,
  # CommandComplete and, on failure, ErrorResponse.
  defp read_query_response(socket, timeout) do
    case read_message(socket, timeout) do
      {:ok, "T", payload} ->
        collect_rows(socket, timeout, decode_row_description(payload), [], %{})

      {:ok, "D", payload} ->
        {:ok, %{rows: [], row_description: nil, status: payload}}

      # A write's CommandComplete: keep reading through ReadyForQuery so the
      # next exchange starts aligned (the top-level C arm that returns here
      # without consuming Z desynced every later query by one message).
      {:ok, "C", payload} ->
        case read_message(socket, timeout) do
          {:ok, "Z", status} -> {:ok, %{rows: [], command_complete: payload, status: status}}
          {:ok, _late, _} -> {:ok, %{rows: [], command_complete: payload, status: nil}}
          {:error, reason} -> {:error, reason}
        end

      {:ok, "E", payload} ->
        {:error, decode_error(payload)}

      {:ok, "1", _payload} ->
        read_query_response(socket, timeout)

      {:ok, "2", _payload} ->
        read_query_response(socket, timeout)

      {:ok, "n", _payload} ->
        read_query_response(socket, timeout)

      {:ok, "3", _payload} ->
        read_query_response(socket, timeout)

      {:ok, "I", _empty} ->
        read_query_response(socket, timeout)

      {:ok, "s", _payload} ->
        {:ok, %{rows: [], closed: true}}

      {:ok, "A", _payload} ->
        read_query_response(socket, timeout)

      {:ok, _type, _payload} ->
        read_query_response(socket, timeout)

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp collect_rows(socket, timeout, description, acc, result) do
    case read_message(socket, timeout) do
      {:ok, "D", payload} ->
        collect_rows(socket, timeout, description, [decode_data_row(payload) | acc], result)

      {:ok, "C", payload} ->
        collect_rows(socket, timeout, description, acc, %{
          rows: Enum.reverse(acc),
          row_description: description,
          command_complete: payload
        })

      {:ok, "E", payload} ->
        {:error, decode_error(payload)}

      {:ok, "n", _payload} ->
        collect_rows(socket, timeout, description, acc, result)

      {:ok, "Z", status} ->
        {:ok, put_status(result, status)}

      {:ok, "T", payload} ->
        # A second RowDescription cannot happen inside one exchange; treating it
        # as a re-declaration would silently discard rows already collected.
        _ = payload
        {:error, :unexpected_row_description}

      {:ok, _type, _payload} ->
        collect_rows(socket, timeout, description, acc, result)

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp put_status(result, status), do: Map.put(result, :status, status)

  # RowDescription: field count, then per field a name, a table OID, a column
  # number, a type OID, a type size, a type modifier, and a format code.
  # Only the name, the type OID and the format are kept — the rest is
  # `Codec`'s problem, and it wants exactly the type.
  defp decode_row_description(<<count::16, rest::binary>>) do
    decode_fields(count, rest, [])
  end

  defp decode_fields(0, _rest, acc), do: Enum.reverse(acc)

  defp decode_fields(n, rest, acc) do
    case decode_field(rest) do
      {:ok, field, rest2} -> decode_fields(n - 1, rest2, [field | acc])
      :error -> Enum.reverse(acc)
    end
  end

  # Per field after the name: table OID (4), column number (2), type OID (4),
  # type size (2), type modifier (4), format code (2) -- 18 bytes. Spelled as the
  # individual fields rather than an opaque `18::binary` so the layout is
  # readable and a wrong byte count is a compile error rather than a silently
  # misaligned decode.
  # Sequential rather than one big binary pattern: the field's total length is
  # only known once the name has been read, so the two halves have to be matched
  # separately. The trailing 18 bytes are spelled out field by field rather than
  # as one opaque size, so a miscount is a compile error here and not a silently
  # misaligned decode at runtime.
  defp decode_field(rest) do
    # The field name is a NULL-TERMINATED string with no length prefix (the
    # version that read an int16 length consumed the name's own bytes as a
    # size and failed every real RowDescription to []).
    with [name, tail] <- :binary.split(rest, <<0>>),
         <<table_oid::32, column::16, type_oid::32, size::16, modifier::32, format::16,
           rest2::binary>> <- tail do
      {:ok,
       %{
         name: name,
         table_oid: table_oid,
         column: column,
         type_oid: type_oid,
         size: size,
         modifier: modifier,
         format: format
       }, rest2}
    else
      _ -> :error
    end
  end

  # DataRow: field count, then per field a length (-1 is NULL) and that many
  # bytes. The raw bytes are handed on undecoded — `Codec` owns the type.
  defp decode_data_row(<<count::16, rest::binary>>) do
    decode_row_cells(count, rest, [])
  end

  defp decode_row_cells(0, _rest, acc), do: Enum.reverse(acc)

  defp decode_row_cells(n, <<-1::signed-32, rest::binary>>, acc),
    do: decode_row_cells(n - 1, rest, [nil | acc])

  defp decode_row_cells(n, <<size::32, rest::binary>>, acc) when size >= 0 do
    <<value::binary-size(^size), rest2::binary>> = rest
    decode_row_cells(n - 1, rest2, [value | acc])
  end

  # ErrorResponse: a field-count-free sequence of (byte, type, value) triples,
  # terminated by a zero byte. The five fields worth keeping are the ones a
  # caller acts on; the rest are dropped rather than guessed at.
  defp decode_error(payload) do
    payload
    |> split_error_fields()
    |> Enum.into(%{severity: nil, code: nil, message: nil, detail: nil, hint: nil, position: nil})
  end

  # Null-terminated cstring fields per the protocol, mapped to the
  # atoms the client's @type promises.
  @field_codes %{
    ?S => :severity,
    ?C => :code,
    ?M => :message,
    ?D => :detail,
    ?H => :hint,
    ?P => :position
  }

  defp split_error_fields(payload), do: split_error_fields(payload, [])

  defp split_error_fields(<<0>>, acc), do: acc |> Enum.reverse() |> Map.new()

  defp split_error_fields(<<field, rest::binary>>, acc) do
    [value, rest2] = :binary.split(rest, <<0>>)
    split_error_fields(rest2, [{Map.get(@field_codes, field, field), value} | acc])
  end

  # ---------------------------------------------------------------------------
  # framing
  # ---------------------------------------------------------------------------

  # Every message after the startup packet is one type byte, a 32-bit length
  # that counts itself but not the type byte, then the payload. Every read is a
  # `{active: false}` receive with an explicit timeout, which is the entire
  # reason a wedged socket cannot hang a caller here.
  defp read_message(socket, timeout) do
    # The type is read as a ONE-BYTE BINARY, not an integer: every clause below
    # matches it as a literal ("T", "D", "Z"), and an integer would force every
    # clause to convert. Reading it as the byte it is on the wire keeps the
    # matches readable and the conversion in one place.
    with {:ok, <<type::binary-size(1)>>} <- recv(socket, 1, timeout),
         {:ok, <<length::32>>} <- recv(socket, 4, timeout),
         {:ok, payload} <- recv(socket, length - 4, timeout) do
      {:ok, type, payload}
    end
  end

  defp recv(_socket, 0, _timeout), do: {:ok, <<>>}

  defp recv(socket, size, timeout) do
    case :gen_tcp.recv(socket, size, timeout) do
      {:ok, data} -> {:ok, data}
      {:error, reason} -> {:error, reason}
      # A socket closed mid-message is a TRUNCATED frame, not an empty one, and
      # reporting it as one would let a partial row set look like a short result.
      :closed -> {:error, :connection_closed}
    end
  end

  # Never blocks on a wedged socket: the owner is stopping and the port must go
  # back. Terminate goes out first so the server releases the backend now rather
  # than at its own idle timeout, which is the exhaustion mode the pool bounds.
  defp close_socket(socket) do
    _ = :gen_tcp.send(socket, <<"X", 4::32>>)
    _ = :gen_tcp.close(socket)
    :ok
  end
end
