defmodule VitrialSync.MutationEvent do
  @moduledoc """
  The `sync.mutation_result` event body, as data.

  `_outcome_event` (`app/sync_service.py:174-191`) appends one of these per
  record, in processing order, and after the commit the reference iterates the
  list twice: once to feed the `sync_mutations_total` counter keyed on the
  stable `reason` token (`app/metrics.py:396-402`, incremented at
  `app/sync_service.py:454-456`), and once to `log_event`. This module builds
  the same maps and nothing more -- the counting and the logging are caller
  work, returned here as data, because a pure function cannot observe anything
  by incrementing a counter.

  The keys are the wire's, not this estate's convention: the reference emits
  eight string-keyed fields, and dual-run compares these maps literally, so an
  atom-keyed or snake_cased variant here would be a defect dressed as style.
  """

  alias VitrialSync.{Admission, CanonicalJSON, Record}

  @typedoc "One outcome event, exactly `_outcome_event`'s eight fields."
  @type t :: %{
          required(:mutationRef) => String.t() | nil,
          required(:deviceRef) => String.t() | nil,
          required(:entityRef) => String.t() | nil,
          required(:entityType) => String.t(),
          required(:resultStatus) => String.t(),
          required(:reason) => String.t(),
          required(:baseServerRevision) => non_neg_integer() | nil,
          required(:resultServerRevision) => non_neg_integer() | nil
        }

  @doc """
  `correlation_ref/1` (`app/observability.py:68-73`): a stable, non-reversible
  reference -- the first 16 hex digits of the SHA-256 of the value, prefixed.

  Falsy is `nil`, not a digest of nothing: the Python branches on
  `if not value`, so both an absent `clientMutationID` and a `nil` device ref
  pass through as `nil`. An empty string is falsy too and maps to `nil` here.
  """
  @spec correlation_ref(String.t() | nil) :: String.t() | nil
  def correlation_ref(nil), do: nil

  def correlation_ref(value) when is_binary(value) do
    if value == "" do
      nil
    else
      "sha256:" <> String.slice(CanonicalJSON.sha256_hex(value), 0, 16)
    end
  end

  @doc """
  The event for one settled record.

  `record` is the record as it arrived and `outcome` the settled per-record
  decision -- the same struct `VitrialSync.Batch` keys its results by, so the
  event and the result cannot disagree about the verdict they report.
  `device_id` is the batch's `deviceID`.

      iex> alias VitrialSync.{Admission, MutationEvent, Record}
      iex> record = %Record{id: "r1", entity_type: "customer", entity_id: "c1", base_server_revision: nil, client_mutation_id: "m1"}
      iex> outcome = %Admission.Outcome{status: :accepted, reason: "committed", result_revision: 4}
      iex> event = MutationEvent.of(record, "dev1", outcome)
      iex> event["reason"]
      "committed"
      iex> event["resultStatus"]
      "accepted"
      iex> byte_size(event["mutationRef"])
      23
      iex> String.starts_with?(event["mutationRef"], "sha256:")
      true
  """
  @spec of(Record.t(), String.t() | nil, Admission.Outcome.t()) :: t()
  def of(%Record{} = record, device_id, %Admission.Outcome{} = outcome) do
    %{
      "mutationRef" => correlation_ref(record.client_mutation_id),
      "deviceRef" => correlation_ref(device_id),
      "entityRef" => correlation_ref(record.entity_id),
      "entityType" => record.entity_type,
      "resultStatus" => to_string(outcome.status),
      "reason" => outcome.reason,
      "baseServerRevision" => record.base_server_revision,
      "resultServerRevision" => outcome.result_revision
    }
  end
end
