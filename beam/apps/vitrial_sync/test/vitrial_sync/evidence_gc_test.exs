defmodule VitrialSync.EvidenceGCTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Canonical, EvidenceGC, Record}
  alias VitrialSync.Canonical.EvidenceBlob

  defp record(attrs) do
    struct!(
      %Record{id: "r1", entity_type: "evidence", entity_id: "e1", updated_at: "t"},
      attrs
    )
  end

  defp blob do
    %EvidenceBlob{document_id: "e1", storage_provider: "s3", object_key: "org/e1.pdf"}
  end

  describe "S-62: when a GC row is produced" do
    test "an evidence tombstone with a blob enqueues one" do
      world = %Canonical.World{evidence_blobs: [blob()]}

      assert EvidenceGC.decide(record(deleted_at: "2026-10-02T00:00:00Z"), world) ==
               {:ok,
                %{
                  document_id: "e1",
                  storage_provider: "s3",
                  object_key: "org/e1.pdf",
                  reason: "metadata_tombstone",
                  grace_seconds: 86_400
                }}
    end

    test "the reason is always metadata_tombstone on this path" do
      # The sync path is the only caller that passes a reason, and it passes
      # this one (`app/sync_service.py:383`). Evidence PUT enqueues with a
      # different reason and does not come through here.
      world = %Canonical.World{evidence_blobs: [blob()]}

      assert {:ok, request} = EvidenceGC.decide(record(deleted_at: "t"), world)
      assert request.reason == "metadata_tombstone"
      assert EvidenceGC.reason() == "metadata_tombstone"
    end
  end

  describe "when nothing is produced" do
    test "an evidence upsert enqueues nothing, even with a blob present" do
      world = %Canonical.World{evidence_blobs: [blob()]}

      assert EvidenceGC.decide(record(%{}), world) == {:ok, nil}
    end

    test "an evidence tombstone with no blob row enqueues nothing and does not reject" do
      # `if blob is not None`. A blob can legitimately be absent because the
      # evidence was never uploaded to object storage, and the metadata
      # tombstone still has to land.
      assert EvidenceGC.decide(record(deleted_at: "t"), Canonical.empty_world()) == {:ok, nil}
    end

    test "another entity's tombstone enqueues nothing, blob or not" do
      world = %Canonical.World{evidence_blobs: [blob()]}

      for type <- ["customer", "item", "measurement", "quotation", "delivery_execution"] do
        assert EvidenceGC.decide(record(%{entity_type: type, deleted_at: "t"}), world) ==
                 {:ok, nil},
               type
      end
    end

    test "a blob belonging to a different document is not used" do
      world = %Canonical.World{
        evidence_blobs: [
          %EvidenceBlob{document_id: "e2", storage_provider: "s3", object_key: "k"}
        ]
      }

      assert EvidenceGC.decide(record(deleted_at: "t"), world) == {:ok, nil}
    end
  end

  describe "the grace period" do
    test "defaults to 86400 seconds, which is settings.evidence_gc_grace_seconds" do
      assert EvidenceGC.default_grace_seconds() == 86_400

      world = %Canonical.World{evidence_blobs: [blob()]}

      assert {:ok, %{grace_seconds: 86_400}} =
               EvidenceGC.decide(record(deleted_at: "t"), world)
    end

    test "is returned in seconds rather than stamped, because the clock is the collector's" do
      # queue_blob_gc computes `not_before = now + max(0, grace)`. Returning the
      # grace and letting the caller stamp inside the transaction keeps this
      # function pure and keeps a test from depending on what time it is.
      world = %Canonical.World{evidence_blobs: [blob()]}

      assert {:ok, %{grace_seconds: 30}} =
               EvidenceGC.decide(record(deleted_at: "t"), world, 30)
    end

    test "a negative grace is clamped to zero, matching max(0, grace)" do
      world = %Canonical.World{evidence_blobs: [blob()]}

      assert {:ok, %{grace_seconds: 0}} =
               EvidenceGC.decide(record(deleted_at: "t"), world, -5)
    end
  end

  describe "what the decision never does" do
    test "it has no rejection arm, because the reference cannot raise here" do
      # Step 8 runs after authority has passed, and the block it transcribes is
      # two `if`s with no raise. The caller is the only thing that can reject.
      assert EvidenceGC.decide(record(deleted_at: "t"), Canonical.empty_world()) == {:ok, nil}
    end
  end
end
