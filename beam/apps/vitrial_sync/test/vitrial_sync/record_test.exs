defmodule VitrialSync.RecordTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Record, Timestamp}

  @valid %{
    id: "r1",
    entity_type: "customer",
    entity_id: "c1",
    updated_at: "2026-10-02T12:00:00Z",
    payload: ~s({"a": 1})
  }

  describe "new/1" do
    test "accepts a well-formed record" do
      assert {:ok, record} = Record.new(@valid)
      assert record.id == "r1"
      assert record.entity_type == "customer"
      assert record.base_server_revision == nil
      assert record.server_revision == nil
      assert record.client_mutation_id == nil
      assert record.deleted_at == nil
    end

    test "requires the mandatory fields" do
      for key <- [:id, :entity_type, :entity_id, :updated_at, :payload] do
        attrs = Map.delete(@valid, key)
        assert {:error, :missing_field} = Record.new(attrs), "expected #{key} to be required"
      end
    end

    test "id and entityID reject the empty string" do
      for key <- [:id, :entity_id] do
        assert {:error, :empty_identifier} = Record.new(Map.put(@valid, key, ""))
      end
    end

    test "clientMutationID ACCEPTS the empty string, because Pydantic has no min_length" do
      # app/schemas.py:79 is max_length only. Rejecting "" here would move the
      # rejection out of the admission step that owns it, and a client sending
      # "" would get a validation error instead of a per-record outcome.
      assert {:ok, record} = Record.new(Map.put(@valid, :client_mutation_id, ""))
      assert record.client_mutation_id == ""
    end

    test "identifiers are capped at 256 bytes, the width of the String(256) key" do
      long = String.duplicate("x", 257)

      for key <- [:id, :entity_id, :client_mutation_id] do
        assert {:error, :identifier_too_long} = Record.new(Map.put(@valid, key, long))
      end

      at_limit = String.duplicate("x", 256)
      assert {:ok, _} = Record.new(Map.put(@valid, :client_mutation_id, at_limit))
    end

    test "a negative revision is rejected, matching Pydantic's ge=0" do
      for key <- [:base_server_revision, :server_revision] do
        assert {:error, :negative_revision} = Record.new(Map.put(@valid, key, -1))
      end
    end

    test "a non-integer revision is rejected rather than coerced" do
      assert {:error, :negative_revision} =
               Record.new(Map.put(@valid, :base_server_revision, 1.0))

      assert {:error, :negative_revision} =
               Record.new(Map.put(@valid, :base_server_revision, "3"))
    end

    test "a payload over the wire ceiling is rejected" do
      oversized = String.duplicate("a", 2_100_001)
      assert {:error, :payload_too_large} = Record.new(Map.put(@valid, :payload, oversized))
    end

    test "the ceilings match app/schemas.py" do
      assert Record.max_records() == 200
      assert Record.max_batch_payload_bytes() == 2_100_000
    end
  end

  describe "tombstone?/1" do
    test "is about presence, not emptiness" do
      # app/sync_service.py branches on `record.deletedAt is not None`, so an
      # empty-string timestamp is a different request from an absent one.
      {:ok, plain} = Record.new(@valid)
      {:ok, tombstone} = Record.new(Map.put(@valid, :deleted_at, ""))
      {:ok, dated} = Record.new(Map.put(@valid, :deleted_at, "2026-10-02T00:00:00Z"))

      refute Record.tombstone?(plain)
      assert Record.tombstone?(tombstone)
      assert Record.tombstone?(dated)
    end
  end

  describe "mutation/1" do
    test "carries exactly the fingerprinted fields" do
      {:ok, record} =
        Record.new(
          Map.merge(@valid, %{
            client_mutation_id: "m1",
            base_server_revision: 3,
            deleted_at: "2026-10-02T00:00:00Z",
            server_revision: 99,
            id: "ignored"
          })
        )

      mutation = Record.mutation(record)

      assert Map.keys(mutation) |> Enum.sort() ==
               [
                 :base_server_revision,
                 :deleted_at,
                 :entity_id,
                 :entity_type,
                 :payload,
                 :updated_at
               ]
    end

    test "serverRevision and id are excluded, because the server assigns them" do
      # Including them would make a mutation's fingerprint depend on what the
      # server last said about it rather than on what the client asked for.
      {:ok, plain} = Record.new(@valid)
      {:ok, with_server_state} = Record.new(Map.put(@valid, :server_revision, 42))

      assert VitrialSync.Fingerprint.of(Record.mutation(plain)) ==
               VitrialSync.Fingerprint.of(Record.mutation(with_server_state))
    end
  end

  describe "normalized_updated_at/1" do
    test "renders the text Python's isoformat would produce" do
      {:ok, record} = Record.new(@valid)
      assert Record.normalized_updated_at(record) == "2026-10-02T12:00:00+00:00"
    end

    test "an offset style does not change the normalization" do
      for spelling <- [
            "2026-10-02T14:00:00+02:00",
            "2026-10-02T12:00:00Z",
            "2026-10-02T12:00:00+00:00"
          ] do
        assert Timestamp.normalize(spelling) == "2026-10-02T12:00:00+00:00"
      end
    end
  end
end
