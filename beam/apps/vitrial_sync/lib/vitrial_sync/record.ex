defmodule VitrialSync.Record do
  @moduledoc """
  One record of a sync push, as it arrives on the wire.

  Mirrors `SyncRecord` in `app/schemas.py:71-79`, including the limits, because
  the limits are behaviour rather than documentation: `baseServerRevision` is
  `ge=0`, so a client that sends `-1` gets a 422 from FastAPI and never reaches
  the push engine. A BEAM record type that accepted `-1` would be a type the
  Python estate cannot represent.

  ## Timestamps stay strings

  `updatedAt` and `deletedAt` are carried as ISO-8601 strings, not `DateTime`
  structs. This is not a shortcut around parsing: `request_fingerprint/1` hashes
  `record.updatedAt.isoformat()`, and `isoformat` on a value that was parsed from
  the wire is not always the string that arrived. `2026-10-02T00:00:00.000000Z`
  round-trips to `2026-10-02T00:00:00+00:00` in Python, so a client that pushed
  with one form and replayed with the other would get two different digests for
  one mutation. Holding the wire string and normalizing through
  `VitrialSync.Timestamp` at the point of hashing keeps that decision in one
  place, which is where vertical 1 already found a defect.
  """

  alias VitrialSync.Timestamp

  @max_records 200
  @max_identifier_length 256
  @max_batch_payload_bytes 2_100_000
  @max_wire_payload_bytes 2_100_000

  defstruct [
    :id,
    :entity_type,
    :entity_id,
    :updated_at,
    :payload,
    :base_server_revision,
    :server_revision,
    :client_mutation_id,
    :deleted_at
  ]

  @type t :: %__MODULE__{
          id: String.t(),
          entity_type: String.t(),
          entity_id: String.t(),
          updated_at: String.t(),
          payload: binary(),
          base_server_revision: non_neg_integer() | nil,
          server_revision: non_neg_integer() | nil,
          client_mutation_id: String.t() | nil,
          deleted_at: String.t() | nil
        }

  @typedoc "Why a record is not a valid `SyncRecord`."
  @type error_reason ::
          :missing_field
          | :identifier_too_long
          | :empty_identifier
          | :payload_too_large
          | :negative_revision

  @doc "The per-batch record ceiling, `MAX_SYNC_RECORDS`."
  @spec max_records() :: pos_integer()
  def max_records, do: @max_records

  @doc "The per-batch aggregate payload ceiling, `MAX_SYNC_V1_BATCH_PAYLOAD_BYTES`."
  @spec max_batch_payload_bytes() :: pos_integer()
  def max_batch_payload_bytes, do: @max_batch_payload_bytes

  @doc """
  Build a record, enforcing the constraints Pydantic enforces.

  The identifier cap is the one that earns its keep. `clientMutationID` is
  `String(256)` in the database and `max_length=256` on the wire, and it is the
  primary key of the idempotency ledger -- a longer one truncated silently would
  let two distinct mutations collide onto one row.
  """
  @spec new(map()) :: {:ok, t()} | {:error, error_reason()}
  def new(attrs) when is_map(attrs) do
    with {:ok, id} <- required_identifier(attrs, :id),
         {:ok, entity_type} <- required_identifier(attrs, :entity_type),
         {:ok, entity_id} <- required_identifier(attrs, :entity_id),
         {:ok, updated_at} <- required_string(attrs, :updated_at),
         {:ok, payload} <- payload(attrs),
         {:ok, base} <- revision(attrs, :base_server_revision),
         {:ok, server} <- revision(attrs, :server_revision),
         {:ok, client_mutation_id} <- optional_identifier(attrs, :client_mutation_id),
         {:ok, deleted_at} <- optional_string(attrs, :deleted_at) do
      {:ok,
       %__MODULE__{
         id: id,
         entity_type: entity_type,
         entity_id: entity_id,
         updated_at: updated_at,
         payload: payload,
         base_server_revision: base,
         server_revision: server,
         client_mutation_id: client_mutation_id,
         deleted_at: deleted_at
       }}
    end
  end

  @doc """
  The fingerprint view of this record.

  `Fingerprint.of/1` takes a plain map so that the canonical encoding is a
  property of the map and not of this struct -- a fingerprint computed from a
  record assembled in a test has to be the same digest as one computed from a
  record that came off the wire.
  """
  @spec mutation(t()) :: VitrialSync.Fingerprint.mutation()
  def mutation(%__MODULE__{} = record) do
    %{
      entity_type: record.entity_type,
      entity_id: record.entity_id,
      base_server_revision: record.base_server_revision,
      updated_at: record.updated_at,
      deleted_at: record.deleted_at,
      payload: record.payload
    }
  end

  @doc """
  Whether the record is a tombstone.

  `deletedAt` being *present* is the test, not being non-empty: `app/sync_service.py`
  branches on `record.deletedAt is not None` throughout, and a client that sends
  an empty-string timestamp is making a different request than one that omits the
  field.
  """
  @spec tombstone?(t()) :: boolean()
  def tombstone?(%__MODULE__{deleted_at: nil}), do: false
  def tombstone?(%__MODULE__{}), do: true

  @doc """
  The normalized `updatedAt` this record will be fingerprinted under.

  Exposed so the timestamp normalization that the fingerprint depends on is
  assertable on its own, rather than only observable as a digest that either
  matches or does not. Raises on an unparseable timestamp, as `Timestamp` does.
  """
  @spec normalized_updated_at(t()) :: String.t()
  def normalized_updated_at(%__MODULE__{updated_at: updated_at}),
    do: Timestamp.normalize(updated_at)

  defp required_identifier(attrs, key) do
    case fetch(attrs, key) do
      {:ok, value} when is_binary(value) -> bounded_identifier(value)
      _ -> {:error, :missing_field}
    end
  end

  defp optional_identifier(attrs, key) do
    case fetch(attrs, key) do
      :error -> {:ok, nil}
      {:ok, nil} -> {:ok, nil}
      {:ok, value} when is_binary(value) -> bounded_optional_identifier(value)
      _ -> {:error, :missing_field}
    end
  end

  # Deliberately NOT `bounded_identifier/1`. `clientMutationID` is
  # `Field(default=None, max_length=256)` in `app/schemas.py:79` -- it has no
  # `min_length`, while `id` and `entityID` both do. So an empty string is a
  # schema-valid record that reaches `_apply_push_once`, where
  # `if not record.clientMutationID` rejects it as
  # `missing_client_mutation_id`. Rejecting it at the type boundary instead
  # would move the rejection out of the step that owns it, and a client sending
  # `""` would get a validation error rather than a per-record outcome.
  defp bounded_optional_identifier(value) when byte_size(value) > @max_identifier_length,
    do: {:error, :identifier_too_long}

  defp bounded_optional_identifier(value), do: {:ok, value}

  defp bounded_identifier(""), do: {:error, :empty_identifier}

  defp bounded_identifier(value) when byte_size(value) > @max_identifier_length,
    do: {:error, :identifier_too_long}

  defp bounded_identifier(value), do: {:ok, value}

  defp required_string(attrs, key) do
    case fetch(attrs, key) do
      {:ok, value} when is_binary(value) and byte_size(value) > 0 -> {:ok, value}
      _ -> {:error, :missing_field}
    end
  end

  defp optional_string(attrs, key) do
    case fetch(attrs, key) do
      :error -> {:ok, nil}
      {:ok, nil} -> {:ok, nil}
      {:ok, value} when is_binary(value) -> {:ok, value}
      _ -> {:error, :missing_field}
    end
  end

  defp payload(attrs) do
    case fetch(attrs, :payload) do
      {:ok, value} when is_binary(value) ->
        if byte_size(value) > @max_wire_payload_bytes,
          do: {:error, :payload_too_large},
          else: {:ok, value}

      _ ->
        {:error, :missing_field}
    end
  end

  # Pydantic's `ge=0` is a real constraint, not a type hint: a negative revision
  # would pass a `%{}` match in a naive implementation and then compare unequal
  # against every stored revision, producing a `stale_revision` rejection for a
  # request that should never have been admitted at all.
  defp revision(attrs, key) do
    case fetch(attrs, key) do
      :error -> {:ok, nil}
      {:ok, nil} -> {:ok, nil}
      {:ok, value} when is_integer(value) and value >= 0 -> {:ok, value}
      {:ok, _value} -> {:error, :negative_revision}
    end
  end

  defp fetch(attrs, key), do: Map.fetch(attrs, key)
end
