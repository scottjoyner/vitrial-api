defmodule VitrialSync.Db.ReadsLiveTest do
  @moduledoc """
  `VitrialSync.Db.Reads` against a real PostgreSQL.

  Every read here is a SELECT whose column list is spelled out rather than `*`,
  and `Codec.decode_row/2` matches POSITIONALLY. That combination has one failure
  mode neither reading nor compiling can see: a column added to a table, or
  reordered, makes the decode silently return the wrong value for a field. The
  only check that catches it is a real server answering the real query and the
  decoded map carrying the values that went in.

  Skips itself, loudly, when no server is reachable — see
  `VitrialSync.Db.LiveDB`. `scripts/beam_gate.sh` has no database, and CI's
  BEAM job supplies one; the guard in `.github/workflows/ci.yml` fails the job if
  this file skips, so "it skipped" can never again read as "it passed".
  """

  use ExUnit.Case, async: false

  alias VitrialSync.Db.{Config, LiveDB, Pgconn, Pool, Reads}

  if reason = LiveDB.unavailable_reason() do
    IO.write(
      :user,
      "[db] reads live suite NOT RUN (no server reachable at $VITRIAL_TEST_DATABASE_URL): #{reason}\n"
    )

    @moduletag skip: reason
  end

  @url LiveDB.url_string()

  setup do
    org = "readslive-" <> Integer.to_string(System.unique_integer([:positive]))

    # `Config.load/1` is not optional sugar: `Pool` reads `s.config.pool_size`
    # and `s.config.pool_timeout_ms`, so a bare keyword list reaches
    # `handle_call/3` as a raw map and every call raises BadMapError. The working
    # shape is copied from `live_db_test.exs`.
    config =
      Config.load(
        url: @url,
        pool_size: 2,
        max_overflow: 1,
        pool_timeout_seconds: 5
      )

    name = Module.concat([ReadsLiveTest, :pool])

    pool =
      start_supervised!({Pool, name: name, config: config, shutdown: 5_000}, id: name)

    %{org: org, pool: pool}
  end

  defp with_conn(pool, fun), do: Pool.with_conn(pool, fun)

  defp insert(conn, sql, params) do
    {:ok, %{rows: _}} = Pgconn.query(conn, sql, params)
  end

  # Each test truncates its own rows in `on_exit`-free fashion at the end of the
  # happy path; a test that fails before reaching it leaves rows behind, which is
  # harmless because every organization id is unique. What is NOT harmless is a
  # connection left mid-transaction, so the pool is stopped per-test via
  # `start_supervised!` rather than shared.
  defp truncate(conn, org) do
    for table <- [
          "sync_change_log",
          "sync_mutation_fingerprints",
          "sync_mutations",
          "sync_entities"
        ] do
      {:ok, _} = Pgconn.query(conn, "DELETE FROM #{table} WHERE organization_id = $1", [org])
    end
  end

  describe "sync_entities/2" do
    test "keys by the triple Batch looks rows up by", %{pool: pool, org: org} do
      with_conn(pool, fn conn ->
        insert(
          conn,
          "INSERT INTO sync_entities (organization_id, entity_type, entity_id, server_revision, payload_json, updated_at)
           VALUES ($1, $2, $3, $4, $5::json, $6::timestamptz)",
          [org, "customer", "c1", 3, ~s({"kind":"customer"}), "2026-01-01 12:00:00"]
        )

        assert {:ok, entities} = Reads.sync_entities(conn, org)
        row = entities[{org, "customer", "c1"}]

        # The point of the test: the decoded VALUE, not just the row's existence.
        # A column list that drifted would decode positionally and still return a
        # row — with the wrong field.
        assert row.organization_id == org
        assert row.entity_type == "customer"
        assert row.entity_id == "c1"
        assert row.server_revision == 3
        assert row.payload_json == %{"kind" => "customer"}
        assert row.updated_at == DateTime.from_naive!(~N[2026-01-01 12:00:00], "Etc/UTC")

        truncate(conn, org)
      end)
    end

    test "an organization with no entities is an empty map, not an error", %{pool: pool} do
      with_conn(pool, fn conn ->
        assert {:ok, entities} = Reads.sync_entities(conn, "no-such-org")
        assert entities == %{}
      end)
    end
  end

  describe "sync_mutation/3" do
    test "returns the prior row for a known clientMutationID", %{pool: pool, org: org} do
      with_conn(pool, fn conn ->
        insert(
          conn,
          "INSERT INTO sync_mutations (organization_id, client_mutation_id, submitted_by_user_id, membership_id, session_id, authorization_revision, device_id, entity_type, entity_id, base_server_revision, result_status, result_server_revision)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
          [
            org,
            "cm-1",
            "user-1",
            "mem-1",
            "sess-1",
            4,
            "dev-1",
            "customer",
            "c1",
            0,
            "accepted",
            7
          ]
        )

        assert {:ok, prior} = Reads.sync_mutation(conn, org, "cm-1")
        assert prior.entity_type == "customer"
        assert prior.result_status == "accepted"
        assert prior.result_server_revision == 7

        truncate(conn, org)
      end)
    end

    test "an unknown id is nil, because that is how a FIRST-TIME mutation is known",
         %{pool: pool, org: org} do
      with_conn(pool, fn conn ->
        # Not `{:error, ...}`: "no prior" is the ordinary answer to the question
        # `Admission` asks, and an error here would make a first push look broken.
        assert {:ok, nil} = Reads.sync_mutation(conn, org, "never-seen")
      end)
    end
  end

  describe "max_sequence/2" do
    test "reads the changelog high-water mark", %{pool: pool, org: org} do
      with_conn(pool, fn conn ->
        insert(
          conn,
          "INSERT INTO sync_change_log (organization_id, entity_type, entity_id, server_revision, operation)
           VALUES ($1, $2, $3, $4, $5), ($1, $2, $6, $7, $5)",
          [org, "customer", "c1", 1, "upsert", "c2", 2]
        )

        assert {:ok, max} = Reads.max_sequence(conn, org)
        assert is_integer(max)
        assert max >= 2

        truncate(conn, org)
      end)
    end

    test "an empty changelog is 0, via COALESCE in the query rather than in Elixir",
         %{pool: pool} do
      with_conn(pool, fn conn ->
        # The nextCursor source. A NULL max and an empty result set are the same
        # answer, and `Cursor.encode/1` must never be handed nil.
        assert {:ok, 0} = Reads.max_sequence(conn, "no-such-org")
      end)
    end
  end

  describe "canonical/2" do
    test "loads the whole chain the World needs", %{pool: pool, org: org} do
      with_conn(pool, fn conn ->
        # Seeded first, and deliberately: `canonical/2` selects the organization by
        # id, so asserting on a row that was never inserted proves the selector
        # returns nil rather than proving the SELECT works. The failure this
        # assertion was written for — `Batch.organization_guard/1` refusing every
        # push because `World.organization` was nil — needs the row to EXIST.
        {:ok, _} =
          Pgconn.query(
            conn,
            "INSERT INTO organizations (id, authorization_revision, name) VALUES ($1, 0, $2)",
            [org, org]
          )

        assert {:ok, world} = Reads.canonical(conn, org)
        assert world.organization, "the organization's row must be present"
        assert world.organization.authorization_revision == 0

        # Structure, not contents: the tables exist and decode. The intra-batch
        # visibility rules are `Prefetch`'s, tested there.
        for key <- [
              :customers,
              :projects,
              :project_sectors,
              :items,
              :project_children,
              :item_children,
              :evidence_blobs
            ] do
          assert is_list(Map.fetch!(world, key)),
                 "#{key} must be a list, got #{inspect(Map.get(world, key))}"
        end
      end)
    end
  end
end
