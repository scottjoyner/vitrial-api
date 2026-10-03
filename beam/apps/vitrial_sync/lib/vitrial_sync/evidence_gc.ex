defmodule VitrialSync.EvidenceGC do
  @moduledoc """
  S-62, admission step 8: the evidence GC enqueue.

  The block at `app/sync_service.py:375-383`:

      if record.entityType == "evidence" and record.deletedAt is not None:
          blob = await db.get(EvidenceBlob, (principal.organization_id, record.entityID))
          if blob is not None:
              await queue_blob_gc(db, blob, reason="metadata_tombstone")

  and `queue_blob_gc` (`app/evidence_gc.py:56-70`), which copies the blob's
  storage handle onto a new `EvidenceObjectGC` row.

  ## Why this is a decision and not a write

  It never rejects. A record that reaches step 8 has already been authorized, so
  the only thing left to decide is *whether* a GC row is produced and with what
  shape. The reference's comment on the call is the reason it matters at all:
  *"Queue physical deletion in the same transaction as the canonical metadata
  tombstone. The collector rechecks references before touching object storage."*
  The same transaction is the guarantee -- an accepted tombstone and its GC row
  commit together or not at all -- and a pure function can only hand the row back
  for the caller to insert inside that transaction.

  ## The grace period

  `queue_blob_gc` computes `not_before = now + max(0, grace)`, where `grace`
  defaults to `settings.evidence_gc_grace_seconds` (86400, one day). The clock is
  the collector's, not this function's: it is the only thing `not_before` means.
  So the decision returns the grace in **seconds** and the caller stamps the time,
  which is what keeps this a pure function and keeps a test from depending on what
  time it is.

  ## The absent-blob case is not a rejection

  `if blob is not None` -- an `evidence` tombstone with no `EvidenceBlob` row
  simply produces no GC row and commits normally. The metadata tombstone still
  lands. That is the reference, and it is the right shape: a blob row can be
  absent because the evidence was never uploaded to object storage, which is a
  normal state rather than an error.
  """

  alias VitrialSync.{Canonical, Record}

  @entity_type "evidence"
  @default_grace_seconds 86_400
  @reason "metadata_tombstone"

  @typedoc """
  The `EvidenceObjectGC` row the reference would insert.

  `not_before` is absent because it is a wall-clock stamp: the caller computes
  `now + grace_seconds` inside the transaction, where "now" is meaningful.
  """
  @type request :: %{
          document_id: String.t(),
          storage_provider: String.t(),
          object_key: String.t(),
          reason: String.t(),
          grace_seconds: non_neg_integer()
        }

  @doc "The default grace, `settings.evidence_gc_grace_seconds`."
  @spec default_grace_seconds() :: pos_integer()
  def default_grace_seconds, do: @default_grace_seconds

  @doc "The reason the sync path always passes."
  @spec reason() :: String.t()
  def reason, do: @reason

  @doc """
  Decide step 8 for one record.

  Returns `{:ok, request}` when a GC row should be enqueued, and `{:ok, nil}`
  when there is nothing to enqueue -- which is every non-evidence record, every
  evidence upsert, and an evidence tombstone with no blob row. There is no
  rejection arm, and that is faithful: this step cannot refuse anything.

      iex> alias VitrialSync.{Canonical, EvidenceGC, Record}
      iex> record = %Record{entity_type: "evidence", entity_id: "e1", deleted_at: "2026-10-02T00:00:00Z"}
      iex> EvidenceGC.decide(record, Canonical.empty_world())
      {:ok, nil}

      iex> alias VitrialSync.{Canonical, EvidenceGC, Record}
      iex> record = %Record{entity_type: "evidence", entity_id: "e1", deleted_at: "2026-10-02T00:00:00Z"}
      iex> world = %Canonical.World{evidence_blobs: [
      ...>   %Canonical.EvidenceBlob{document_id: "e1", storage_provider: "s3", object_key: "org/e1.pdf"}
      ...> ]}
      iex> EvidenceGC.decide(record, world)
      {:ok, %{document_id: "e1", storage_provider: "s3", object_key: "org/e1.pdf", reason: "metadata_tombstone", grace_seconds: 86400}}
  """
  @spec decide(Record.t(), Canonical.World.t(), non_neg_integer()) :: {:ok, request() | nil}
  def decide(%Record{} = record, %Canonical.World{} = world, grace_seconds \\ @default_grace_seconds) do
    if record.entity_type == @entity_type and not is_nil(record.deleted_at) do
      case Canonical.evidence_blob(world, record.entity_id) do
        nil ->
          {:ok, nil}

        blob ->
          {:ok,
           %{
             document_id: blob.document_id,
             storage_provider: blob.storage_provider,
             object_key: blob.object_key,
             reason: @reason,
             grace_seconds: max(0, grace_seconds)
           }}
      end
    else
      {:ok, nil}
    end
  end
end