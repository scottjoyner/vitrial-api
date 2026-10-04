defmodule VitrialSync.Db.Codec do
  @moduledoc """
  The value boundary between the estate's terms and PostgreSQL's wire types.

  The transport (`VitrialSync.Db.Pgconn`) speaks the extended query protocol
  with **every value in TEXT format — parameter format code 0 sending, result
  format code 0 receiving** — so there is exactly one encoding on each side of
  this module and no binary-path branch to get wrong:

    * **Encode (`encode_param/1`)** — every bind parameter this layer emits goes
      through here and comes out as a term `Pgconn` can put on the wire:
      `nil` (the SQL NULL marker), an integer, a float, a boolean, or a binary.
      A `DateTime` becomes UTC text (`2026-01-01 12:00:00.000000+00`), a map or
      list becomes JSON bytes. Anything else **raises `ArgumentError`** — a
      caller-visible error beats a garbage bind, and a bind this module refused
      can never reach the wire half-encoded.
    * **Decode (`decode_cell/2`, `decode_row/2`)** — a text-format cell plus its
      `RowDescription` field (`%{type_oid: oid, ...}`, the shape `Pgconn`
      hands back) to an estate term: SQL NULL → `nil`, `timestamptz`/`timestamp`
      text → a UTC `DateTime`, `json`/`jsonb` bytes through `VitrialSync.JSON`,
      `int2`/`int4`/`int8`/`oid` text → integers, `bool` `t`/`f` → booleans,
      `float4`/`float8` → floats, and `numeric` as the raw binary, per the
      doctrine. Unknown type OIDs pass the cell through raw rather than
      corrupting it with a guess.

  ## Type notes, pinned against the wire and a live server

    * **Every value is TEXT — including everything that looks numeric.** The
      server's `RowDescription` carries the column's type OID; that OID, not a
      guess from the bytes, decides the parse. The OIDs below are the catalog's
      pinned built-in constants (`pg_type`, stable across every supported
      server), not something discovered per session — the estate binds zero
      user-defined types.
    * **`timestamptz` text carries the session's offset** (`…+00` here, because
      the connection pins UTC through the startup packet —
      `VitrialSync.Db.Pgconn`'s `statement_timeout`/GUC pairing). The parser
      honours whatever offset arrives and normalizes to UTC, so a connection
      that somehow lost the pin decodes correctly rather than wrongly.
    * **`json` / `jsonb` decode through `VitrialSync.JSON`.** Encode takes any
      JSON-able term and emits bytes through the same module. Round-trip
      symmetry is contractual here; stored-byte parity with the reference's
      `json.dumps` output is an explicitly open decision for the next slice
      (see `VitrialSync.Db.Writes`).
    * **`numeric` decodes to a raw binary** (`"1.25"`). None of the fourteen
      sync-path tables has a numeric column; the passthrough is pinned anyway
      because the doctrine's answer for numeric is exactly this
      (`DEPENDENCY-DOCTRINE.md §4.2`).
  """

  alias VitrialSync.JSON

  # The built-in type OIDs this layer parses. Catalog constants, pinned by
  # PostgreSQL itself (see `pg_type.dat` in any server source tree) — they do
  # not vary by version or installation for these types.
  @oid_bool 16
  @oid_name 19
  @oid_int8 20
  @oid_int2 21
  @oid_int4 23
  @oid_text 25
  @oid_oid 26
  @oid_json 114
  @oid_float4 700
  @oid_float8 701
  @oid_varchar 1043
  @oid_bpchar 1042
  @oid_timestamp 1114
  @oid_timestamptz 1184
  @oid_numeric 1700
  @oid_uuid 2950
  @oid_jsonb 3802

  @int_oids [@oid_int2, @oid_int4, @oid_int8, @oid_oid]
  @float_oids [@oid_float4, @oid_float8]
  @json_oids [@oid_json, @oid_jsonb]
  @timestamp_oids [@oid_timestamp, @oid_timestamptz]
  # text/varchar/bpchar/name/uuid and numeric have no clause of their own: the
  # catch-all passthrough below IS their decode, and unknown OIDs ride it too.

  @doc "The catalog type OID for one of the names this layer knows, for probes."
  @spec type_oid(atom()) :: pos_integer() | nil
  def type_oid(:bool), do: @oid_bool
  def type_oid(:name), do: @oid_name
  def type_oid(:int8), do: @oid_int8
  def type_oid(:int2), do: @oid_int2
  def type_oid(:int4), do: @oid_int4
  def type_oid(:text), do: @oid_text
  def type_oid(:oid), do: @oid_oid
  def type_oid(:json), do: @oid_json
  def type_oid(:float4), do: @oid_float4
  def type_oid(:float8), do: @oid_float8
  def type_oid(:varchar), do: @oid_varchar
  def type_oid(:bpchar), do: @oid_bpchar
  def type_oid(:timestamp), do: @oid_timestamp
  def type_oid(:timestamptz), do: @oid_timestamptz
  def type_oid(:numeric), do: @oid_numeric
  def type_oid(:uuid), do: @oid_uuid
  def type_oid(:jsonb), do: @oid_jsonb
  def type_oid(_), do: nil

  @doc """
  Normalize one bind parameter to a term `Pgconn` can put on the wire in TEXT
  format.

  Accepts: `nil` (the SQL NULL), integers, floats, booleans, binaries,
  `%DateTime{}`/`%NaiveDateTime{}` (→ UTC text), maps and lists (→ JSON bytes,
  for the `json`/`jsonb` columns). Raises `ArgumentError` on anything else —
  atoms, tuples, structs of every other kind — because a term this module
  refuses must never reach the wire half-encoded.
  """
  @spec encode_param(term()) :: term()
  def encode_param(nil), do: nil
  def encode_param(value) when is_integer(value), do: value
  def encode_param(value) when is_float(value), do: value
  def encode_param(value) when is_boolean(value), do: value
  def encode_param(value) when is_binary(value), do: value

  def encode_param(%DateTime{} = datetime) do
    case shift_to_utc(datetime) do
      {:ok, utc} ->
        timestamp_text(utc)

      {:error, reason} ->
        raise ArgumentError,
              "cannot encode #{inspect(datetime)} as a timestamptz parameter: #{inspect(reason)}. " <>
                "The estate pins UTC on the wire (VitrialSync.Db.Pgconn)"
    end
  end

  # A naive datetime is accepted as UTC — the estate's only honest reading of a
  # value that carries no zone — and documented as such rather than guessed at
  # silently: the doc says exactly what acceptance means.
  def encode_param(%NaiveDateTime{} = naive), do: timestamp_text_utc(naive)

  # JSON bytes as a flat BINARY: the wire encoder binds binaries verbatim, and
  # JSON.encode!/1 builds an IO-list of small chunks (one element per token) —
  # handing that through would put character integers on the wire un-framed.
  def encode_param(value) when is_map(value) and not is_struct(value),
    do: IO.iodata_to_binary(JSON.encode!(value))

  def encode_param(value) when is_list(value), do: IO.iodata_to_binary(JSON.encode!(value))

  def encode_param(value) do
    raise ArgumentError,
          "cannot bind #{inspect(value)}: not a wire-encodable term. Convert it first " <>
            "(nil binds as NULL, timestamps as DateTime/NaiveDateTime, JSON as map/list)"
  end

  # A non-UTC zone fails loud through the default UTC-only database rather than
  # silently encoding a wrong instant: the estate pins UTC on the wire
  # (VitrialSync.Db.Pgconn's startup packet), so callers hand over UTC DateTimes.
  defp shift_to_utc(%DateTime{time_zone: "Etc/UTC"} = datetime), do: {:ok, datetime}

  defp shift_to_utc(%DateTime{} = datetime),
    do: DateTime.shift_zone(datetime, "Etc/UTC", Calendar.UTCOnlyTimeZoneDatabase)

  # UTC timestamptz text: "2026-01-01 12:00:00.000000+00". Six fractional
  # digits always — PostgreSQL's input parser accepts 0..6, and the fixed
  # width keeps the bound form deterministic and diffable.
  defp timestamp_text(%DateTime{} = datetime), do: timestamp_text_utc(DateTime.to_naive(datetime))

  defp timestamp_text_utc(naive) do
    %{year: y, month: mo, day: d, hour: h, minute: mi, second: s, microsecond: {micro, _}} =
      naive

    :erlang.iolist_to_binary(
      :io_lib.format(
        ~c"~4..0B-~2..0B-~2..0B ~2..0B:~2..0B:~2..0B.~6..0B+00",
        [y, mo, d, h, mi, s, micro]
      )
    )
  end

  @doc """
  Decode one text-format cell of the type named by `field` (a `RowDescription`
  field map, `type_oid` the part that matters) into an estate term.
  SQL NULL (`nil`) → `nil`, always.
  """
  @spec decode_cell(term(), %{optional(atom()) => term()}) :: term()
  def decode_cell(nil, _field), do: nil

  def decode_cell(cell, %{type_oid: oid}) when is_binary(cell) and is_integer(oid),
    do: decode_text(cell, oid)

  # A field map missing type_oid is a caller bug, not a decoding case.
  def decode_cell(_cell, field) do
    raise ArgumentError, "not a RowDescription field with a type_oid: #{inspect(field)}"
  end

  defp decode_text(cell, oid) when oid in @int_oids, do: String.to_integer(cell)

  defp decode_text(cell, @oid_bool) do
    case cell do
      "t" -> true
      "f" -> false
      other -> raise ArgumentError, "undecodable boolean text: #{inspect(other)}"
    end
  end

  defp decode_text(cell, oid) when oid in @float_oids do
    case Float.parse(cell) do
      {value, ""} -> value
      _ -> raise ArgumentError, "undecodable float text: #{inspect(cell)}"
    end
  end

  defp decode_text(cell, oid) when oid in @timestamp_oids, do: parse_timestamp_text(cell)

  defp decode_text(cell, oid) when oid in @json_oids do
    case JSON.decode(cell) do
      {:ok, decoded} ->
        decoded

      {:error, reason} ->
        raise ArgumentError,
              "undecodable JSON in column of type #{oid}: #{inspect(reason)} (cell: #{inspect(cell)})"
    end
  end

  # text/varchar/bpchar/name/uuid, plus numeric — the doctrine's numeric answer
  # is the raw binary, and it rides the text passthrough. Unknown OIDs land in
  # the same clause: raw, never a guess.
  defp decode_text(cell, _oid), do: cell

  @doc """
  Decode one row — a `RowDescription` field list paired with its text-format
  cell list — into a plain map keyed by column-name atoms, carrying EVERY
  column.

  The codec boundary never drops data — a schema drift (a column added or
  renamed under this SELECT) shows up as a changed map, which the tests pin, not
  as a silently mis-read field. A field/cell count mismatch raises: zipping a
  short row quietly would be exactly the silent mis-read this boundary exists
  to prevent. Column names are turned into atoms with `String.to_atom/1`; that
  is safe here and only here, because the names originate in this layer's own
  literal SELECT lists (`VitrialSync.Db.Rows`), never in client input.
  """
  @spec decode_row([%{optional(atom()) => term()}], [term()]) :: %{
          optional(atom()) => term()
        }
  def decode_row(fields, cells) when is_list(fields) and is_list(cells) do
    if length(fields) != length(cells) do
      raise ArgumentError,
            "row/column mismatch: #{length(cells)} cells against #{length(fields)} fields"
    end

    fields
    |> Enum.zip(cells)
    |> Map.new(fn {field, cell} ->
      {String.to_atom(field.name), decode_cell(cell, field)}
    end)
  end

  # -- timestamptz / timestamp text -------------------------------------------

  # PostgreSQL's ISO DateStyle output: "2026-01-02 03:04:05.123456+00". The
  # fraction is 1-6 digits with trailing zeros trimmed by the server, and the
  # offset is present on timestamptz (it is the SESSION's offset) and absent on
  # timestamp. BC dates and Julian weirdness do not occur in this estate and
  # fail loud here rather than decoding wrong.
  @timestamp_regex ~r|^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:([+-])(\d{2})(?::?(\d{2}))?)?$|

  defp parse_timestamp_text(text) do
    case Regex.run(@timestamp_regex, text) do
      # The optional minutes group only participates when the server sent one
      # ("+05:30"); "+00" yields ten groups and must not fall through to the
      # no-offset clause — that miss WAS a live test failure.
      [_, y, mo, d, h, mi, s, frac, sign, off_h, off_m] ->
        offset = offset_seconds(sign, off_h, off_m)
        from_parts(y, mo, d, h, mi, s, frac, offset)

      [_, y, mo, d, h, mi, s, frac, sign, off_h] ->
        offset = offset_seconds(sign, off_h, "0")
        from_parts(y, mo, d, h, mi, s, frac, offset)

      # No fraction AND no offset: groups 7-10 never participate, so the run
      # has seven elements (this was counted as eight and failed a live test).
      [_, y, mo, d, h, mi, s] ->
        # `timestamp` (no zone): the estate reads naive values as UTC.
        from_parts(y, mo, d, h, mi, s, nil, 0)

      [_, y, mo, d, h, mi, s, frac] ->
        from_parts(y, mo, d, h, mi, s, frac, 0)

      _ ->
        raise ArgumentError, "undecodable timestamp text: #{inspect(text)}"
    end
  end

  defp offset_seconds(sign, off_h, off_m) do
    seconds = String.to_integer(off_h) * 3600 + String.to_integer(off_m || "0") * 60

    case sign do
      "+" -> seconds
      "-" -> -seconds
    end
  end

  # Built through NaiveDateTime.new/2 rather than NaiveDateTime.from_erl/2's
  # microsecond option, which misroutes on this Elixir (verified: the option
  # reaches Calendar.ISO.valid_time?/4 malformed there, while Time.new/4 with
  # the same pair is fine).
  defp from_parts(y, mo, d, h, mi, s, frac, offset_seconds) do
    # A cell with NO fraction decodes precision-0 (== a plain naive); a fraction
    # of any width normalizes to 6 digits — matching DateTime's own rule, so a
    # decoded value compares equal to its DateTime.from_naive! original.
    micro =
      if frac in [nil, ""] do
        {0, 0}
      else
        {String.to_integer(binary_part(frac <> "000000", 0, 6)), 6}
      end

    with {:ok, date} <-
           Date.new(String.to_integer(y), String.to_integer(mo), String.to_integer(d)),
         {:ok, time} <-
           Time.new(
             String.to_integer(h),
             String.to_integer(mi),
             String.to_integer(s),
             micro
           ),
         {:ok, naive} <- NaiveDateTime.new(date, time),
         {:ok, utc} <- DateTime.from_naive(naive, "Etc/UTC") do
      DateTime.add(utc, -offset_seconds, :second)
    else
      {:error, reason} ->
        raise ArgumentError,
              "undecodable timestamp text: #{inspect({y, mo, d, h, mi, s, frac})} (#{inspect(reason)})"
    end
  end
end
