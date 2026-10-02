defmodule VitrialSync.Page do
  @moduledoc """
  Assembling one page of a pull response, and deciding what the cursor may
  advance past.

  Reproduces the page loop in `pull_since` (`app/sync_service.py:462-556`) and
  `pull_page_accepts` (`app/sync_service.py:63-79`).

  ## The cursor discipline is the whole point

  Three outcomes, and conflating any two of them is a data-loss bug rather than a
  performance one:

    * **Invisible change -- CONSUMED.** The cursor advances past it. It can never
      be delivered to this principal, so leaving it unconsumed would stall every
      subsequent pull at the same position forever.
    * **Change whose entity is gone -- CONSUMED.** Same argument.
    * **Visible, deliverable, but does not fit this page -- NOT CONSUMED, and
      the loop stops.** The next request must observe that change again rather
      than silently skip it.

  The third case is why the loop `break`s rather than `continue`s. Skipping a
  full change would leave later, smaller changes deliverable, and the cursor
  would have to advance past a record the client never received.

  ## Why the response size is measured, not estimated

  Three ceilings apply, and they are not interchangeable:

    * `MAX_SYNC_RECORDS` (200) bounds the record count
    * `MAX_SYNC_V1_BATCH_PAYLOAD_BYTES` (2_100_000) bounds the **sum of payload
      bytes**, which is what the iOS client budgets against
    * `MAX_SYNC_PULL_RESPONSE_BYTES` (2_250_000) bounds the **serialized batch**,
      because base64 expansion and per-record framing are not covered by the
      other two

  All three have to be checked, and the third has to be checked against the bytes
  that will actually go on the wire. Estimating it is how the response grows past
  the ceiling that exists to stop it.

  ## The wire escaper is NOT the canonical escaper

  This module escapes non-ASCII as **raw UTF-8**. `VitrialSync.CanonicalJSON`
  escapes it as `\\uXXXX`. Both are correct, for different consumers:

    * the **fingerprint** canonicalizes through Python's `json.dumps`, whose
      default is `ensure_ascii=True`
    * the **response** is serialized by Pydantic, which emits raw UTF-8

  Reusing the canonical escaper here would over-count every non-ASCII identifier
  by four bytes per character, which is the difference between a page that fits
  and a page that does not -- decided by data content rather than by size.

  Sizes are accumulated as records are appended rather than recomputed per
  candidate. Re-measuring the batch on every one of up to 200 candidates is
  quadratic in page size for no reason: the record bytes do not depend on the
  cursor, and only the cursor's own length varies.
  """

  alias VitrialSync.{CanonicalJSON, Record}

  @max_records 200
  @max_batch_payload_bytes 2_100_000
  @max_response_bytes 2_250_000

  # The scan ceiling bounds the QUERY, not the response. It is here because the
  # caller needs it to size the scan, and keeping it beside the response ceilings
  # makes the distinction legible rather than leaving two unrelated numbers in
  # different files.
  @max_scan_changes 500

  @wire_prefix "{\"deviceID\":\"server\",\"cursor\":\""
  @wire_mid "\",\"records\":["
  @wire_suffix "]}"

  defmodule Candidate do
    @moduledoc """
    One change-log row, joined to the facts the page decision needs.

    `visible?` and `entity_present?` are separate because they are separate
    lookups in the reference implementation (`resolve_visible` and
    `page.entities.get(...)`), and because they are the two cases the cursor
    discipline treats identically but for different reasons.
    """

    @type t :: %__MODULE__{
            sequence: non_neg_integer(),
            entity_type: String.t(),
            entity_id: String.t(),
            payload_json: map(),
            server_revision: non_neg_integer(),
            client_mutation_id: String.t() | nil,
            updated_at: String.t(),
            deleted_at: String.t() | nil,
            visible?: boolean(),
            entity_present?: boolean()
          }

    defstruct [
      :sequence,
      :entity_type,
      :entity_id,
      :payload_json,
      :server_revision,
      :client_mutation_id,
      :updated_at,
      :deleted_at,
      visible?: true,
      entity_present?: true
    ]
  end

  defmodule PageBuilder do
    @moduledoc """
    Running totals for one page, so the fit test is O(1) per candidate.

    `record_bytes` and `payload_bytes` are carried because re-serializing the page
    on each of up to 200 candidates would be quadratic for no benefit: record
    bytes do not depend on the cursor, and only the cursor's own length varies.
    """

    defstruct cursor_sequence: 0,
              records: [],
              record_bytes: 0,
              payload_bytes: 0,
              count: 0,
              stopped_at: nil
  end

  @doc "Per-page record ceiling, `MAX_SYNC_RECORDS`."
  @spec max_records() :: pos_integer()
  def max_records, do: @max_records

  @doc "Aggregate payload ceiling, `MAX_SYNC_V1_BATCH_PAYLOAD_BYTES`."
  @spec max_batch_payload_bytes() :: pos_integer()
  def max_batch_payload_bytes, do: @max_batch_payload_bytes

  @doc "Serialized response ceiling, `MAX_SYNC_PULL_RESPONSE_BYTES`."
  @spec max_response_bytes() :: pos_integer()
  def max_response_bytes, do: @max_response_bytes

  @doc "Per-pull scan ceiling, `MAX_SYNC_PULL_SCAN_CHANGES`."
  @spec max_scan_changes() :: pos_integer()
  def max_scan_changes, do: @max_scan_changes

  @doc """
  Assemble one page from scanned changes, in ascending sequence order.

  Returns the records to send, the sequence the response cursor should carry, and
  the change the loop stopped at (`nil` if it ran out of changes).
  """
  @spec assemble(non_neg_integer(), [Candidate.t()]) :: %{
          records: [Record.t()],
          cursor_sequence: non_neg_integer(),
          stopped_at: non_neg_integer() | nil
        }
  def assemble(start_sequence, candidates) when is_integer(start_sequence) do
    %PageBuilder{cursor_sequence: start_sequence}
    |> walk(candidates)
    |> result()
  end

  defp walk(%PageBuilder{stopped_at: stopped_at} = builder, _rest) when not is_nil(stopped_at) do
    builder
  end

  defp walk(builder, []) do
    builder
  end

  defp walk(builder, [%Candidate{} = candidate | rest]) do
    cond do
      # Consumed: cannot be delivered to this principal, so the cursor must move
      # past it. Leaving it unconsumed would stall every later pull at the same
      # position forever.
      not candidate.visible? ->
        walk(consume(builder, candidate.sequence), rest)

      not candidate.entity_present? ->
        walk(consume(builder, candidate.sequence), rest)

      true ->
        record = to_record(candidate)

        if accepts?(builder, record, candidate.sequence) do
          walk(append(builder, record, candidate.sequence), rest)
        else
          # NOT consumed, and nothing after it is considered either -- skipping a
          # full change would leave later smaller changes deliverable, and the
          # cursor would advance past a record the client never received.
          %PageBuilder{builder | stopped_at: candidate.sequence}
        end
    end
  end

  defp result(%PageBuilder{} = builder) do
    %{
      records: Enum.reverse(builder.records),
      cursor_sequence: builder.cursor_sequence,
      stopped_at: builder.stopped_at
    }
  end

  defp consume(%PageBuilder{cursor_sequence: current} = builder, sequence) do
    %PageBuilder{builder | cursor_sequence: max(current, sequence)}
  end

  defp append(%PageBuilder{} = builder, record, sequence) do
    %PageBuilder{
      builder
      | records: [record | builder.records],
        record_bytes: builder.record_bytes + record_byte_size(record),
        payload_bytes: builder.payload_bytes + byte_size(record.payload),
        count: builder.count + 1,
        cursor_sequence: max(builder.cursor_sequence, sequence)
    }
  end

  # -- the page-fit predicate ---------------------------------------------------

  defp accepts?(%PageBuilder{} = builder, record, sequence) do
    cond do
      builder.count >= @max_records ->
        false

      builder.payload_bytes + byte_size(record.payload) > @max_batch_payload_bytes ->
        false

      true ->
        response_bytes_with(builder, record, sequence) <= @max_response_bytes
    end
  end

  # Total wire size if `record` were appended, computed from the running totals
  # rather than by re-serializing the page.
  defp response_bytes_with(%PageBuilder{} = builder, record, sequence) do
    @wire_prefix
    |> then(&byte_size/1)
    |> Kernel.+(escaped_byte_size(VitrialSync.Cursor.encode(sequence)))
    |> Kernel.+(byte_size(@wire_mid))
    |> Kernel.+(builder.record_bytes + record_byte_size(record))
    |> Kernel.+(max(builder.count, 0))
    |> Kernel.+(byte_size(@wire_suffix))
  end

  @doc """
  The exact number of bytes the serialized `SyncBatch` will occupy.

  Exposed because it is the only place the wire shape is defined, and every
  ceiling decision depends on it being right. A test that asserts a byte count is
  a test of the response contract, not of an arithmetic detail.
  """
  @spec batch_byte_size([Record.t()], non_neg_integer()) :: non_neg_integer()
  def batch_byte_size(records, sequence) do
    counts =
      Enum.reduce(records, %{bytes: 0, count: 0}, fn record, acc ->
        %{
          bytes: acc.bytes + record_byte_size(record),
          count: acc.count + 1
        }
      end)

    byte_size(@wire_prefix) +
      escaped_byte_size(VitrialSync.Cursor.encode(sequence)) +
      byte_size(@wire_mid) +
      counts.bytes +
      max(counts.count - 1, 0) +
      byte_size(@wire_suffix)
  end

  @doc """
  The exact number of bytes one serialized `SyncRecord` will occupy.

  Field order and null handling follow `SyncRecord` in `app/schemas.py:71-79`:
  declaration order, and every field present including the nulls. Pydantic does
  not omit nulls by default, and a size that assumed it did would under-count
  every record in the page.
  """
  @spec record_byte_size(Record.t()) :: non_neg_integer()
  def record_byte_size(%Record{} = record) do
    # The eight key/value separators plus the enclosing braces, all fixed.
    quoted(record.id) +
      quoted(record.entity_type) +
      quoted(record.entity_id) +
      quoted(record.updated_at) +
      quoted(record.payload) +
      number(record.base_server_revision) +
      number(record.server_revision) +
      nullable_quoted(record.client_mutation_id) +
      nullable_quoted(record.deleted_at) +
      record_overhead()
  end

  # Every key, comma, colon and brace in a serialized SyncRecord, and NOTHING
  # else -- no value quotes. The quotes belong to `quoted/1` and
  # `nullable_quoted/1`, and folding them in here as well double-counts one
  # quote per field.
  #
  # Measured against Pydantic: 130 bytes for the shape, so a baseline record
  # (5 quoted strings, 2 nulls, 1 revision digit, 1 quoted id) is 130 + 48 + 13
  # = 191, which is what `SyncRecord.model_dump_json()` produces.
  @record_keys "{\"id\":,\"entityType\":,\"entityID\":,\"updatedAt\":,\"payload\":,\"baseServerRevision\":,\"serverRevision\":,\"clientMutationID\":,\"deletedAt\":}"

  defp record_overhead, do: byte_size(@record_keys)

  defp quoted(value), do: 2 + escaped_byte_size(value)

  defp nullable_quoted(nil), do: 4
  defp nullable_quoted(value), do: 2 + escaped_byte_size(value)

  defp number(nil), do: 4
  # Integer.digits/2 returns a LIST of digits, not a count -- sizing a
  # revision by it produces an arithmetic error on the first non-zero
  # revision rather than a wrong size.
  defp number(value) when is_integer(value), do: value |> Integer.to_string() |> byte_size()

  @doc false
  @spec escaped_byte_size(binary()) :: non_neg_integer()
  def escaped_byte_size(string) when is_binary(string), do: byte_size(escape(string))

  # The WIRE escaper: raw UTF-8 for everything printable, short forms for the
  # five control characters that have them, \u00XX for the rest.
  #
  # Deliberately NOT `VitrialSync.CanonicalJSON`'s escaper, which emits \uXXXX for
  # every non-ASCII code point to match Python's `json.dumps(ensure_ascii=True)`.
  # The response is serialized by Pydantic, which does not do that. See the
  # moduledoc.
  defp escape(string) do
    for <<char::utf8 <- string>>, into: "" do
      escape_char(char)
    end
  end

  defp escape_char(0x22), do: "\\\""
  defp escape_char(0x5C), do: "\\\\"
  defp escape_char(0x08), do: "\\b"
  defp escape_char(0x0C), do: "\\f"
  defp escape_char(0x0A), do: "\\n"
  defp escape_char(0x0D), do: "\\r"
  defp escape_char(0x09), do: "\\t"

  # 0x7F is DEL, and it is NOT escaped here -- but it IS escaped by
  # `VitrialSync.CanonicalJSON`. Both are right: Pydantic's wire form passes DEL
  # through raw, while `json.dumps(ensure_ascii=True)` emits `\u007f`. Carrying
  # the canonical rule into this escaper over-counts every identifier containing
  # a DEL by five bytes.
  defp escape_char(char) when char < 0x20 do
    "\\u" <> (char |> Integer.to_string(16) |> String.downcase() |> String.pad_leading(4, "0"))
  end

  defp escape_char(char), do: <<char::utf8>>

  defp to_record(%Candidate{} = candidate) do
    # `entity.payload_json or {}` in the reference: a null payload column is an
    # empty object, not a null and not a decode failure.
    payload = Base.encode64(CanonicalJSON.encode(candidate.payload_json || %{}))

    %Record{
      id: "server:#{candidate.sequence}",
      entity_type: candidate.entity_type,
      entity_id: candidate.entity_id,
      updated_at: candidate.updated_at,
      payload: payload,
      base_server_revision: nil,
      server_revision: candidate.server_revision,
      client_mutation_id: candidate.client_mutation_id,
      deleted_at: candidate.deleted_at
    }
  end
end
