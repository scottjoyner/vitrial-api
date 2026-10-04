defmodule VitrialSync.Db.RowsTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Canonical
  alias VitrialSync.Db.{Codec, Rows}

  # Literal probe rows in the shape VitrialSync.Db.Pgconn.query/3 returns: a
  # RowDescription field list (name + catalog type_oid) plus a TEXT-format cell
  # list, where NULL is the list's nil and timestamps are server text. Built by
  # hand so the decoder is tested against the WIRE's shapes, not against
  # round-trips of itself.
  defp fields(names, types) when is_tuple(types) do
    names
    |> Enum.zip(Tuple.to_list(types))
    |> Enum.map(fn {name, type} ->
      %{name: Atom.to_string(name), type_oid: Codec.type_oid(type), format: 0}
    end)
  end

  @ts0 "2026-01-01 00:00:00+00"
  @ts123 "2026-01-01 01:02:03+00"

  describe "lossless decode" do
    test "every pinned DECODABLE table's column list matches a full-row decode of a literal probe row" do
      # Schema drift is a TEST FAILURE here, not a silent mis-read: the probe
      # rows below carry exactly the columns the migrations and app/models.py
      # declare, and the decoded maps must carry every one of them.
      # evidence_object_gc is deliberately absent: it is ENCODE-only in this
      # slice — the engine writes GC rows but never reads them back (Rows'
      # moduledoc) — so it has no decoder to probe.
      probes = %{
        organizations:
          {~w(id name authorization_revision created_at)a,
           {:varchar, :varchar, :int8, :timestamptz}, ["org-1", "Org", "3", @ts0]},
        memberships:
          {~w(id organization_id user_id active all_customers all_projects customer_ids project_ids roles capabilities)a,
           {:varchar, :varchar, :varchar, :bool, :bool, :bool, :json, :json, :json, :json},
           ["ms-1", "org-1", "user-1", "t", "f", "f", ~s(["c1"]), "[]", "[]", ~s(["sync"])]},
        canonical_customers:
          {~w(organization_id customer_id deleted_at)a, {:varchar, :varchar, :timestamptz},
           ["org-1", "cust-1", nil]},
        canonical_projects:
          {~w(organization_id project_id customer_id deleted_at)a,
           {:varchar, :varchar, :varchar, :timestamptz}, ["org-1", "proj-1", "cust-1", nil]},
        canonical_project_sectors:
          {~w(organization_id project_sector_id project_id sector_id deleted_at)a,
           {:varchar, :varchar, :varchar, :varchar, :timestamptz},
           ["org-1", "ps-1", "proj-1", "sec-1", nil]},
        canonical_items:
          {~w(organization_id item_id project_id project_sector_id deleted_at)a,
           {:varchar, :varchar, :varchar, :varchar, :timestamptz},
           ["org-1", "item-1", "proj-1", nil, nil]},
        canonical_project_children:
          {~w(organization_id entity_type entity_id project_id deleted_at)a,
           {:varchar, :varchar, :varchar, :varchar, :timestamptz},
           ["org-1", "quotation", "q-1", "proj-1", nil]},
        canonical_item_children:
          {~w(organization_id entity_type entity_id item_id deleted_at)a,
           {:varchar, :varchar, :varchar, :varchar, :timestamptz},
           ["org-1", "evidence", "e-1", "item-1", nil]},
        sync_entities:
          {~w(organization_id entity_type entity_id server_revision schema_version payload_json updated_at deleted_at)a,
           {:varchar, :varchar, :varchar, :int8, :int4, :json, :timestamptz, :timestamptz},
           [
             "org-1",
             "customer",
             "cust-9",
             "4",
             "1",
             ~s({"kind":"customer"}),
             "2026-01-01 01:02:03+00",
             nil
           ]},
        sync_mutations:
          {~w(id organization_id client_mutation_id submitted_by_user_id membership_id session_id authorization_revision device_id entity_type entity_id base_server_revision result_server_revision result_status received_at)a,
           {:int8, :varchar, :varchar, :varchar, :varchar, :varchar, :int8, :varchar, :varchar,
            :varchar, :int8, :int8, :varchar, :timestamptz},
           [
             "11",
             "org-1",
             "m-1",
             "user-1",
             "ms-1",
             "sess-1",
             "5",
             "dev-1",
             "customer",
             "cust-9",
             nil,
             "4",
             "accepted",
             @ts123
           ]},
        sync_change_log:
          {~w(sequence organization_id entity_type entity_id server_revision client_mutation_id operation created_at)a,
           {:int8, :varchar, :varchar, :varchar, :int8, :varchar, :varchar, :timestamptz},
           ["21", "org-1", "customer", "cust-9", "4", "m-1", "upsert", @ts123]},
        sync_mutation_fingerprints:
          {~w(organization_id client_mutation_id request_fingerprint)a,
           {:varchar, :varchar, :varchar}, ["org-1", "m-1", String.duplicate("ab", 32)]},
        evidence_blobs:
          {~w(organization_id document_id item_id filename mime_type sha256 size_bytes storage_provider object_key created_at)a,
           {:varchar, :varchar, :varchar, :varchar, :varchar, :varchar, :int8, :varchar, :varchar,
            :timestamptz},
           [
             "org-1",
             "doc-1",
             "item-1",
             "f.pdf",
             "application/pdf",
             String.duplicate("ab", 32),
             "10",
             "s3",
             "k",
             @ts0
           ]}
      }

      # Thirteen decoders, one encode-only table (the GC queue: the engine
      # writes GC rows but never reads them back this slice), zero gaps.
      assert MapSet.new(Map.keys(probes)) ==
               MapSet.difference(MapSet.new(Rows.tables()), MapSet.new([:evidence_object_gc]))

      assert MapSet.size(MapSet.new(Rows.tables())) == 14

      Enum.each(probes, fn {table, {names, types, cells}} ->
        assert Rows.table_columns(table) == names
        decoded = apply(Rows, table, [fields(names, types), cells])
        assert MapSet.new(Map.keys(decoded)) == MapSet.new(names)
      end)
    end

    test "sync_mutations decodes all fourteen columns (recounted against app/models.py:193-208)" do
      assert length(Rows.table_columns(:sync_mutations)) == 14
    end

    test "memberships decodes its ten columns including the json lists" do
      names =
        ~w(id organization_id user_id active all_customers all_projects customer_ids project_ids roles capabilities)a

      types = {:varchar, :varchar, :varchar, :bool, :bool, :bool, :json, :json, :json, :json}

      cells =
        [
          "ms-1",
          "org-1",
          "user-1",
          "t",
          "f",
          "t",
          ~s(["c2","c1"]),
          ~s(["p1"]),
          "[]",
          ~s(["sync","admin"])
        ]

      decoded = Rows.memberships(fields(names, types), cells)

      assert decoded.id == "ms-1"
      assert decoded.customer_ids == ["c2", "c1"]
      assert decoded.capabilities == ["sync", "admin"]
      assert decoded.all_projects == true
    end

    test "json nulls and values decode as data, not bytes" do
      names =
        ~w(organization_id entity_type entity_id server_revision schema_version payload_json updated_at deleted_at)a

      types = {:varchar, :varchar, :varchar, :int8, :int4, :json, :timestamptz, :timestamptz}

      # payload_json's TEXT is the JSON literal `null` — it decodes to nil as
      # DATA (the "no payload" reading Canonical.payload_object/1 is written
      # against), which is different from the SQL NULL of deleted_at.
      cells = ["org-1", "customer", "c1", "1", "1", "null", @ts0, nil]

      decoded = Rows.sync_entities(fields(names, types), cells)

      assert decoded.payload_json == nil
      assert decoded.deleted_at == nil
    end

    test "decode_result/1 walks a whole Pgconn result map" do
      names = ~w(organization_id customer_id deleted_at)a
      fs = fields(names, {:varchar, :varchar, :timestamptz})

      result = %{
        rows: [["org-1", "c1", nil], ["org-1", "c2", "2026-01-01 00:00:00+00"]],
        row_description: fs,
        command_complete: "SELECT 2" <> <<0>>,
        status: nil
      }

      assert Rows.decode_result({:ok, result}) == [
               %{organization_id: "org-1", customer_id: "c1", deleted_at: nil},
               %{
                 organization_id: "org-1",
                 customer_id: "c2",
                 deleted_at: DateTime.from_naive!(~N[2026-01-01 00:00:00], "Etc/UTC")
               }
             ]

      # The write shape carries no row_description key at all (the client omits
      # it on CommandComplete-without-RowDescription) and no rows.
      assert Rows.decode_result(%{rows: [], command_complete: "INSERT 0 1" <> <<0>>}) == []
    end
  end

  describe "projections" do
    test "organizations → Organization{authorization_revision}; drops id/name/created_at because the push path locks and reads only the revision" do
      org =
        ~w(id name authorization_revision created_at)a
        |> fields({:varchar, :varchar, :int8, :timestamptz})
        |> then(&Rows.organizations(&1, ["org-1", "Named Org", "7", @ts0]))
        |> Rows.to_canonical_organization()

      assert org == %Canonical.Organization{authorization_revision: 7}
    end

    test "memberships → Membership's seven fields; drops id, roles, capabilities (the grant path widens only the id lists)" do
      names =
        ~w(id organization_id user_id active all_customers all_projects customer_ids project_ids roles capabilities)a

      types = {:varchar, :varchar, :varchar, :bool, :bool, :bool, :json, :json, :json, :json}

      cells =
        [
          "ms-1",
          "org-1",
          "user-1",
          "t",
          "f",
          "f",
          ~s(["c1"]),
          ~s(["p1"]),
          ~s([{"x": 1}]),
          ~s(["sync"])
        ]

      membership =
        names
        |> fields(types)
        |> then(&Rows.memberships(&1, cells))
        |> Rows.to_canonical_membership()

      assert membership == %Canonical.Membership{
               organization_id: "org-1",
               user_id: "user-1",
               active: true,
               all_customers: false,
               all_projects: false,
               customer_ids: ["c1"],
               project_ids: ["p1"]
             }
    end

    test "canonical tables drop only organization_id (the World's one-organization design)" do
      customer =
        ~w(organization_id customer_id deleted_at)a
        |> fields({:varchar, :varchar, :timestamptz})
        |> then(&Rows.canonical_customers(&1, ["org-1", "cust-1", nil]))
        |> Rows.to_canonical_customer()

      assert customer == %Canonical.Customer{customer_id: "cust-1", deleted_at: nil}

      project =
        ~w(organization_id project_id customer_id deleted_at)a
        |> fields({:varchar, :varchar, :varchar, :timestamptz})
        |> then(&Rows.canonical_projects(&1, ["org-1", "proj-1", "cust-1", nil]))
        |> Rows.to_canonical_project()

      assert project == %Canonical.Project{
               project_id: "proj-1",
               customer_id: "cust-1",
               deleted_at: nil
             }

      sector =
        ~w(organization_id project_sector_id project_id sector_id deleted_at)a
        |> fields({:varchar, :varchar, :varchar, :varchar, :timestamptz})
        |> then(&Rows.canonical_project_sectors(&1, ["org-1", "ps-1", "proj-1", "sec-1", nil]))
        |> Rows.to_canonical_project_sector()

      assert sector == %Canonical.ProjectSector{
               project_sector_id: "ps-1",
               project_id: "proj-1",
               sector_id: "sec-1",
               deleted_at: nil
             }

      item =
        ~w(organization_id item_id project_id project_sector_id deleted_at)a
        |> fields({:varchar, :varchar, :varchar, :varchar, :timestamptz})
        |> then(&Rows.canonical_items(&1, ["org-1", "item-1", "proj-1", "ps-1", nil]))
        |> Rows.to_canonical_item()

      assert item == %Canonical.Item{
               item_id: "item-1",
               project_id: "proj-1",
               project_sector_id: "ps-1",
               deleted_at: nil
             }

      project_child =
        ~w(organization_id entity_type entity_id project_id deleted_at)a
        |> fields({:varchar, :varchar, :varchar, :varchar, :timestamptz})
        |> then(
          &Rows.canonical_project_children(&1, ["org-1", "quotation", "q-1", "proj-1", nil])
        )
        |> Rows.to_canonical_project_child()

      assert project_child == %Canonical.ProjectChild{
               entity_type: "quotation",
               entity_id: "q-1",
               project_id: "proj-1",
               deleted_at: nil
             }

      item_child =
        ~w(organization_id entity_type entity_id item_id deleted_at)a
        |> fields({:varchar, :varchar, :varchar, :varchar, :timestamptz})
        |> then(&Rows.canonical_item_children(&1, ["org-1", "evidence", "e-1", "item-1", nil]))
        |> Rows.to_canonical_item_child()

      assert item_child == %Canonical.ItemChild{
               entity_type: "evidence",
               entity_id: "e-1",
               item_id: "item-1",
               deleted_at: nil
             }
    end

    test "sync_entities → Entity's five fields; drops organization_id/schema_version/updated_at" do
      names =
        ~w(organization_id entity_type entity_id server_revision schema_version payload_json updated_at deleted_at)a

      types = {:varchar, :varchar, :varchar, :int8, :int4, :json, :timestamptz, :timestamptz}

      cells = ["org-1", "customer", "cust-9", "4", "1", ~s({"kind":"customer"}), @ts123, nil]

      entity =
        names
        |> fields(types)
        |> then(&Rows.sync_entities(&1, cells))
        |> Rows.to_canonical_entity()

      assert entity == %Canonical.Entity{
               entity_type: "customer",
               entity_id: "cust-9",
               payload: %{"kind" => "customer"},
               deleted_at: nil,
               server_revision: 4
             }
    end

    test "evidence_blobs → EvidenceBlob's three fields — queue_blob_gc's whole input" do
      names =
        ~w(organization_id document_id item_id filename mime_type sha256 size_bytes storage_provider object_key created_at)a

      types =
        {:varchar, :varchar, :varchar, :varchar, :varchar, :varchar, :int8, :varchar, :varchar,
         :timestamptz}

      cells =
        [
          "org-1",
          "doc-1",
          "item-1",
          "f.pdf",
          "application/pdf",
          String.duplicate("ab", 32),
          "10",
          "s3",
          "org/doc-1",
          @ts0
        ]

      blob =
        names
        |> fields(types)
        |> then(&Rows.evidence_blobs(&1, cells))
        |> Rows.to_canonical_evidence_blob()

      assert blob == %Canonical.EvidenceBlob{
               document_id: "doc-1",
               storage_provider: "s3",
               object_key: "org/doc-1"
             }
    end

    test "sync_mutations → to_prior/1 produces exactly Batch.Store's five-field prior map" do
      names =
        ~w(id organization_id client_mutation_id submitted_by_user_id membership_id session_id authorization_revision device_id entity_type entity_id base_server_revision result_server_revision result_status received_at)a

      types =
        {:int8, :varchar, :varchar, :varchar, :varchar, :varchar, :int8, :varchar, :varchar,
         :varchar, :int8, :int8, :varchar, :timestamptz}

      cells =
        [
          "11",
          "org-1",
          "m-1",
          "user-1",
          "ms-1",
          "sess-1",
          "5",
          "dev-1",
          "customer",
          "cust-9",
          "2",
          "4",
          "accepted",
          @ts123
        ]

      prior =
        names
        |> fields(types)
        |> then(&Rows.sync_mutations(&1, cells))
        |> Rows.to_prior()

      # Key-for-key the map Batch.put_priors/4 threads in-memory (batch.ex:474-493),
      # so a loaded prior and a threaded one are indistinguishable downstream.
      assert prior == %{
               entity_type: "customer",
               entity_id: "cust-9",
               base_server_revision: 2,
               result_status: "accepted",
               result_server_revision: 4
             }
    end
  end
end
