defmodule VitrialSync.Db.WritesTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Db.Writes

  @now DateTime.from_naive!(~N[2026-10-03 12:00:00], "Etc/UTC")

  defp mutation_row(overrides \\ %{}) do
    Map.merge(
      %{
        organization_id: "org-1",
        client_mutation_id: "m-1",
        submitted_by_user_id: "user-1",
        membership_id: "ms-1",
        session_id: "sess-1",
        authorization_revision: 5,
        device_id: "dev-1",
        entity_type: "customer",
        entity_id: "cust-9",
        base_server_revision: nil,
        result_server_revision: 4,
        result_status: "accepted"
      },
      overrides
    )
  end

  describe "single-statement encoders" do
    test "mutation_insert binds the twelve application columns; id and received_at stay server-clocked" do
      {sql, params} = Writes.mutation_insert(mutation_row())

      assert sql =~ ~r/INSERT INTO sync_mutations/s
      assert sql =~ "base_server_revision"
      refute sql =~ "received_at"
      refute sql =~ ~r/\bid\b.*VALUES/s

      assert params == [
               "org-1",
               "m-1",
               "user-1",
               "ms-1",
               "sess-1",
               5,
               "dev-1",
               "customer",
               "cust-9",
               :null,
               4,
               "accepted"
             ]
    end

    test "fingerprint_insert binds the three composite-PK columns" do
      {sql, params} =
        Writes.fingerprint_insert(%{
          organization_id: "org-1",
          client_mutation_id: "m-1",
          request_fingerprint: String.duplicate("ab", 32)
        })

      assert sql =~ "INSERT INTO sync_mutation_fingerprints"
      assert params == ["org-1", "m-1", String.duplicate("ab", 32)]
    end

    test "entity_upsert conflicts on the composite PK and rewrites the mutable columns" do
      row = %{
        organization_id: "org-1",
        entity_type: "customer",
        entity_id: "cust-9",
        server_revision: 4,
        payload: %{"kind" => "customer"},
        updated_at: "2026-01-01T12:00:00Z",
        deleted_at: nil
      }

      {sql, params} = Writes.entity_upsert(row)

      assert sql =~ "ON CONFLICT (organization_id, entity_type, entity_id)"
      assert sql =~ "DO UPDATE SET server_revision = EXCLUDED.server_revision"

      # ISO-8601 strings from the wire record convert BEFORE binding: a raw
      # string into timestamptz would kill the connection in
      # epgsql_idatetime:timestamp2i/1. UTC conversion of 12:00Z is 12:00 UTC.
      assert params == [
               "org-1",
               "customer",
               "cust-9",
               4,
               ~s({"kind":"customer"}),
               {{2026, 1, 1}, {12, 0, 0}},
               :null
             ]
    end

    test "changelog_insert leaves sequence and created_at to the server" do
      {sql, params} =
        Writes.changelog_insert(%{
          organization_id: "org-1",
          entity_type: "customer",
          entity_id: "cust-9",
          server_revision: 4,
          client_mutation_id: nil,
          operation: "upsert"
        })

      assert sql =~ "INSERT INTO sync_change_log"
      refute sql =~ "sequence"
      # client_mutation_id is nullable — and nil binds as :null, not nil.
      assert params == ["org-1", "customer", "cust-9", 4, :null, "upsert"]
    end

    test "membership_grant appends the delta server-side onto the stored lists" do
      {sql, params} = Writes.membership_grant(%{customer_ids: ["c2"], project_ids: []}, "ms-1")

      assert sql =~ "UPDATE memberships"
      assert sql =~ "customer_ids::jsonb || $1::jsonb"
      assert sql =~ "WHERE id = $3"
      assert params == [~s(["c2"]), "[]", "ms-1"]
    end

    test "evidence_gc_insert fills the two holes: org from context, not_before = now + grace" do
      request = %{
        document_id: "doc-1",
        storage_provider: "s3",
        object_key: "org/doc-1",
        reason: "metadata_tombstone",
        grace_seconds: 86_400
      }

      {sql, params} = Writes.evidence_gc_insert(request, "org-1", @now)

      assert sql =~ "INSERT INTO evidence_object_gc"
      # The Effects request carries neither field (evidence_gc.ex:55-63); the
      # encoder sources organization_id from the caller and stamps not_before
      # inside the transaction, matching app/evidence_gc.py:65-70.
      # The timestamp binds as the encoded naive erl tuple (Codec.encode_param),
      # not as a struct — a struct bind is a connection-kill.
      assert params == [
               "org-1",
               "doc-1",
               "s3",
               "org/doc-1",
               "metadata_tombstone",
               {{2026, 10, 4}, {12, 0, 0}}
             ]
    end

    test "evidence_gc_insert clamps a negative grace to zero, like max(0, grace)" do
      request = %{
        document_id: "d",
        storage_provider: "s3",
        object_key: "k",
        reason: "r",
        grace_seconds: -5
      }

      {_sql, params} = Writes.evidence_gc_insert(request, "org-1", @now)
      assert List.last(params) == {{2026, 10, 3}, {12, 0, 0}}
    end

    test "organization_revision_update writes the final threaded integer" do
      {sql, params} = Writes.organization_revision_update(3, "org-1")
      assert sql =~ "UPDATE organizations SET authorization_revision = $1 WHERE id = $2"
      assert params == [3, "org-1"]
    end
  end

  describe "effects/4 ordering" do
    test "renders every effect in list order, grants and GC last, bump last of all" do
      effects = %VitrialSync.Batch.Effects{
        mutation_rows: [mutation_row()],
        fingerprint_rows: [
          %{organization_id: "org-1", client_mutation_id: "m-1", request_fingerprint: "ff"}
        ],
        entity_upserts: [
          %{
            organization_id: "org-1",
            entity_type: "customer",
            entity_id: "cust-9",
            server_revision: 4,
            payload: nil,
            updated_at: "2026-01-01T00:00:00Z",
            deleted_at: nil
          }
        ],
        changelog_rows: [
          %{
            organization_id: "org-1",
            entity_type: "customer",
            entity_id: "cust-9",
            server_revision: 4,
            client_mutation_id: "m-1",
            operation: "upsert"
          }
        ],
        membership_grants: [%{customer_ids: ["c2"], project_ids: []}],
        evidence_gc_rows: [
          %{
            document_id: "doc-1",
            storage_provider: "s3",
            object_key: "k",
            reason: "metadata_tombstone",
            grace_seconds: 0
          }
        ],
        organization_authorization_revision: 1
      }

      statements = Writes.effects(effects, "org-1", "ms-1", @now)
      kinds = Enum.map(statements, fn {sql, _} -> kind_of(sql) end)

      assert kinds == [
               :mutation,
               :fingerprint,
               :entity,
               :changelog,
               :grant,
               :gc,
               :revision
             ]

      # A zero bump writes nothing — no grant happened, nothing to say.
      effects = %{effects | organization_authorization_revision: 0}

      kinds =
        effects
        |> Writes.effects("org-1", "ms-1", @now)
        |> Enum.map(fn {sql, _} -> kind_of(sql) end)

      refute :revision in kinds
    end

    defp kind_of(sql) do
      cond do
        sql =~ "INSERT INTO sync_mutations" -> :mutation
        sql =~ "INSERT INTO sync_mutation_fingerprints" -> :fingerprint
        sql =~ "INSERT INTO sync_entities" -> :entity
        sql =~ "INSERT INTO sync_change_log" -> :changelog
        sql =~ "UPDATE memberships" -> :grant
        sql =~ "INSERT INTO evidence_object_gc" -> :gc
        sql =~ "UPDATE organizations" -> :revision
        true -> flunk("unrecognized statement: #{sql}")
      end
    end
  end
end
