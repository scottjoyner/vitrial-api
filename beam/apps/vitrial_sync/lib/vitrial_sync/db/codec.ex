defmodule VitrialSync.Db.Codec do
  @moduledoc """
  The value boundary between the estate's terms and PostgreSQL's wire types.

  Two directions, one rule each:

    * **Encode (`encode_param/1`)** — every bind parameter this layer emits goes
      through here. The rule that matters most: **`nil` becomes `:null`,
      everywhere, unconditionally.** It is not a style choice. Binding a literal
      `nil` raises `{:integer_overflow, :int8, nil}` inside
      `deps/epgsql/src/epgsql_codec_integer.erl` — epgsql never sees a clause for
      it — and that exception **kills the connection process**, not just the
      query. Same for any Elixir struct: binding a `DateTime` (or
      `NaiveDateTime`, or any other struct) raises `function_clause` in
      `epgsql_idatetime:timestamp2i/1` and kills the connection with it. One
      missed normalization in one encoder is therefore a production
      connection-kill triggered by data, invisible to the compiler. `encode_param/1`
      is the single choke point, and it **raises `ArgumentError`** on terms it
      cannot encode — a caller-visible error beats a dead connection.
    * **Decode (`decode_cell/2`, `decode_row/2`)** — driver output to estate
      terms. `:null` comes back as `nil` (the inverse of the encode rule), a
      `timestamptz` as a UTC `DateTime`, `json`/`jsonb` bytes through
      `VitrialSync.JSON`, everything else as the raw driver term (integers,
      booleans, binaries).

  ## Type notes, pinned against the driver source and a live server

    * **`timestamptz` — binary decode is timezone-independent epoch arithmetic.**
      The corrected fact: `deps/epgsql/src/epgsql_idatetime.erl:25` decodes
      `timestamptz` as `i2timestamp(N)` — integer microseconds to calendar
      tuple, no timezone input anywhere — so the same stored instant decodes to
      the same UTC wall-clock regardless of session timezone (verified live
      against a server with `timezone=America/New_York`: identical decode from a
      default and a UTC-pinned session, while TEXT reads differed). Decode here
      yields a UTC `DateTime`. Encode takes a `DateTime` (converted to UTC) or a
      naive `{{y, m, d}, {h, m, s}}` tuple; structs are refused before they can
      kill a connection.
    * **`json` / `jsonb` decode to raw binaries** (`'{"a": 1}'` as a binary,
      verified) — decoded here through `VitrialSync.JSON`. Encode takes any
      JSON-able term and emits bytes through `VitrialSync.JSON.encode/1`.
      Round-trip symmetry is contractual here; stored-byte parity with the
      reference's `json.dumps` output is an explicitly open decision for the
      next slice (see `VitrialSync.Db.Writes`).
    * **`numeric` decodes to a raw binary** (`'1.25'`, verified). None of the
      fourteen sync-path tables has a numeric column; the passthrough is pinned
      anyway because the doctrine's answer for numeric is exactly this
      (`DEPENDENCY-DOCTRINE.md §4.2`).
    * **`int2`/`int4`/`int8`/`bool`/text types pass through** as Elixir
      integers/booleans/binaries.

  ## `squery` output is TEXT — do not feed it here

  A simple-protocol `squery` SELECT returns `{:ok, columns, rows}` — the same
  3-tuple shape as `equery` — but its rows are **text-format raw binaries**, not
  driver-decoded terms, and under a non-UTC session timezone its timestamp text
  differs from what the binary decode of the same row yields. Only
  `equery`/extended-protocol output belongs in `decode_row/2`; `squery` is for
  DDL, `SET`s and probes whose values are not consumed.
  """

  alias VitrialSync.JSON

  @doc """
  Normalize one bind parameter to what epgsql can encode without dying.

  Accepts: `nil` (→ `:null`), integers, floats, booleans, binaries, `:null`
  (already normalized), `%DateTime{}` (→ naive UTC erl tuple), maps and lists
  (→ JSON bytes, for the `json`/`jsonb` columns). Raises `ArgumentError` on
  anything else — atoms other than `:null`, tuples, structs of every kind —
  because the alternative is an epgsql `function_clause` that kills the
  connection process.
  """
  @spec encode_param(term()) :: term()
  def encode_param(nil), do: :null
  def encode_param(:null), do: :null
  def encode_param(value) when is_integer(value), do: value
  def encode_param(value) when is_float(value), do: value
  def encode_param(value) when is_boolean(value), do: value
  def encode_param(value) when is_binary(value), do: value

  def encode_param(%DateTime{} = datetime) do
    case shift_to_utc(datetime) do
      {:ok, utc} ->
        utc
        |> DateTime.to_naive()
        |> NaiveDateTime.to_erl()

      {:error, reason} ->
        raise ArgumentError,
              "cannot encode #{inspect(datetime)} as a timestamptz parameter: #{inspect(reason)}. " <>
                "The estate pins UTC on the wire (VitrialSync.Db.Conn)"
    end
  end

  def encode_param(value) when is_map(value) and not is_struct(value), do: JSON.encode!(value)
  def encode_param(value) when is_list(value), do: JSON.encode!(value)

  def encode_param(value) do
    raise ArgumentError,
          "cannot bind #{inspect(value)}: epgsql cannot encode this term, and attempting " <>
            "would kill the connection process. Convert it first (nil binds as :null, " <>
            "timestamps as DateTime or {{y,m,d},{h,m,s}}, JSON as map/list)"
  end

  # A non-UTC zone fails loud through the default UTC-only database rather than
  # silently encoding a wrong instant: the estate pins UTC on the wire
  # (VitrialSync.Db.Conn's SET timezone), so callers hand over UTC DateTimes.
  defp shift_to_utc(%DateTime{time_zone: "Etc/UTC"} = datetime), do: {:ok, datetime}

  defp shift_to_utc(%DateTime{} = datetime),
    do: DateTime.shift_zone(datetime, "Etc/UTC", Calendar.UTCOnlyTimeZoneDatabase)

  @doc """
  Decode one driver cell of type `type` (the epgsql type atom from the column
  description) into an estate term. `:null` → `nil`, always.
  """
  @spec decode_cell(term(), atom()) :: term()
  def decode_cell(:null, _type), do: nil

  def decode_cell(value, type) when type in [:timestamptz, :timestamp] and is_tuple(value),
    do: to_datetime(value)

  def decode_cell(value, type) when type in [:json, :jsonb] and is_binary(value) do
    case JSON.decode(value) do
      {:ok, decoded} ->
        decoded

      {:error, reason} ->
        raise ArgumentError, "undecodable JSON in column of type #{type}: #{inspect(reason)}"
    end
  end

  # int4/int8/int2: integers. bool: booleans. text/varchar/bpchar/name:
  # binaries. numeric: raw binary by doctrine. Anything unknown passes through
  # raw rather than corrupting it with a guess.
  def decode_cell(value, _type), do: value

  @doc """
  Decode one equery row against its column descriptions into a plain map keyed
  by column-name atoms, carrying EVERY column.

  The codec boundary never drops data — a schema drift (a column added or
  renamed under this SELECT) shows up as a changed map, which the tests pin, not
  as a silently mis-read field. Column names are turned into atoms with
  `String.to_atom/1`; that is safe here and only here, because the names
  originate in this layer's own literal SELECT lists (`VitrialSync.Db.Rows`),
  never in client input.
  """
  @spec decode_row([tuple()], tuple()) :: %{optional(atom()) => term()}
  def decode_row(columns, row) when is_tuple(row) do
    columns
    |> Enum.with_index()
    |> Map.new(fn {column, index} ->
      name = elem(column, 1)
      type = elem(column, 2)
      {String.to_atom(name), decode_cell(elem(row, index), type)}
    end)
  end

  # -- timestamptz ------------------------------------------------------------

  # epgsql decodes timestamptz to an erl calendar tuple {{y,m,d},{h,m,s}} with a
  # FLOAT second (epgsql_idatetime:i2timestamp/1). Convert to a UTC DateTime,
  # preserving microseconds from the fraction.
  defp to_datetime({{y, m, d}, {h, mi, s}}) when is_float(s) do
    seconds = trunc(s)
    micro = round((s - seconds) * 1_000_000)
    to_datetime({{y, m, d}, {h, mi, seconds}}, micro)
  end

  defp to_datetime({{_y, _m, _d}, {_h, _mi, _s}} = erl), do: to_datetime(erl, 0)

  defp to_datetime({date, time}, micro) do
    # Built through NaiveDateTime.new/2 rather than NaiveDateTime.from_erl/2's
    # microsecond option, which misroutes on this Elixir (verified: the option
    # reaches Calendar.ISO.valid_time?/4 malformed there, while Time.new/4 with
    # the same pair is fine).
    with {:ok, date} <- Date.from_erl(date),
         {:ok, time} <- Time.new(elem(time, 0), elem(time, 1), elem(time, 2), {micro, 6}),
         {:ok, naive} <- NaiveDateTime.new(date, time),
         {:ok, utc} <- DateTime.from_naive(naive, "Etc/UTC") do
      utc
    else
      {:error, reason} ->
        raise ArgumentError,
              "undecodable timestamp from driver: #{inspect({date, time, micro})} (#{inspect(reason)})"
    end
  end
end
