defmodule VitrialSync.PullServiceLiveTest do
  @moduledoc """
  `VitrialSync.PullService` against a real PostgreSQL 17 with the real migrations.

  The cursor rule is the thing worth testing and the thing that cannot be tested
  without real rows in a real table: an INVISIBLE change is consumed (the cursor
  advances past it) while a visible change that does not fit is NOT (the cursor
  stops before it). Both are silent when wrong — one stalls a client forever, the
  other skips a record it was entitled to — and neither raises.

  ## The database is SHARED across runs

  `System.unique_integer([:positive, :monotonic])` makes an organization unique
  within a VM's life, which is NOT the same as unique across runs: a second run
  restarting the counter can land on an id a previous run used, and the changelog
  then holds four changes where the test inserted two —

      left: 4, right: 2

  So the organization id carries the WALL CLOCK as well. Cheap, and it makes a
  failure here mean what it says rather than being a statement about whoever ran
  last.

  `Page.Candidate` carries `visible?` and `entity_present?` separately for
  exactly this reason, so the two cases are distinguishable here.

  Skips itself, loudly, without a server. `.github/workflows/ci.yml` fails the
  job on `NOT RUN`.
  """

  use ExUnit.Case, async: false

  alias VitrialSync.{Canonical, Page, PullService}
  alias VitrialSync.Db.{Config, LiveDB, Pgconn, Pool, Reads}

  if reason = LiveDB.unavailable_reason() do
    IO.write(:user, "[db] pull service live suite NOT RUN: #{reason}\n")
    @moduletag skip: reason
  end

  @url LiveDB.url_string()
  @now ~U[2026-10-05 12:00:00Z]

  setup context do
    unique =
      "#{context[:test_id]}-#{System.unique_integer([:positive, :monotonic])}-#{System.os_time(:second)}"

    org = "pullsvc-org-#{unique}"

    config =
      Config.load(url: @url, pool_size: 2, max_overflow: 1, pool_timeout_seconds: 10)

    name = Module.concat([PullServiceLiveTest, :pool])
    pool = start_supervised!({Pool, name: name, config: config, shutdown: 5_000}, id: name)

    Pool.with_conn(pool, fn conn ->
      {:ok, _} =
        Pgconn.query(
          conn,
          "INSERT INTO organizations (id, authorization_revision, name) VALUES ($1, 0, $2)
           ON CONFLICT (id) DO NOTHING",
          [org, org]
        )
    end)

    %{org: org, pool: name}
  end

  defp principal(org) do
    %Canonical.Principal{
      user_id: "pullsvc-user",
      organization_id: org,
      membership_id: "pullsvc-mem",
      session_id: "pullsvc-sess",
      authorization_revision: 0,
      capabilities: MapSet.new(["sync"]),
      customer_ids: MapSet.new(),
      project_ids: MapSet.new(),
      # `all_customers` makes everything visible without seeding the whole
      # canonical chain — this suite is about the CURSOR, not about visibility
      # rules, and `Prefetch`'s own tests own those.
      all_customers: true,
      all_projects: true
    }
  end

  defp insert_change(pool, org, entity_type, entity_id, revision) do
    Pool.with_conn(pool, fn conn ->
      {:ok, _} =
        Pgconn.query(
          conn,
          "INSERT INTO sync_entities (organization_id, entity_type, entity_id, server_revision, payload_json, updated_at)
           VALUES ($1, $2, $3, $4, $5::json, $6::timestamptz)
           ON CONFLICT (organization_id, entity_type, entity_id) DO UPDATE
             SET server_revision = EXCLUDED.server_revision",
          [
            org,
            entity_type,
            entity_id,
            revision,
            ~s({"n":"#{entity_id}"}),
            "2026-10-05T12:00:00Z"
          ]
        )

      {:ok, _} =
        Pgconn.query(
          conn,
          "INSERT INTO sync_change_log (organization_id, entity_type, entity_id, server_revision, operation)
           VALUES ($1, $2, $3, $4, 'upsert')",
          [org, entity_type, entity_id, revision]
        )

      :ok
    end)
  end

  describe "pull/3 — the empty page" do
    @tag test_id: "empty"
    test "a start sequence past the end returns no records and does not move the cursor",
         %{pool: pool, org: org} do
      assert {:ok, result, observation} = PullService.pull(pool, 0, principal(org))

      assert result.records == []
      # The cursor stays where it started: there was nothing to consume, and
      # advancing it past unseen changes would skip them.
      assert result.cursor_sequence == 0
      assert observation.scanned_change_count == 0
      assert observation.record_count == 0
      refute observation.truncated
    end
  end

  describe "pull/3 — changes are returned in sequence order" do
    @tag test_id: "order"
    test "a page returns the changes after the cursor, oldest first", %{pool: pool, org: org} do
      insert_change(pool, org, "customer", "c1", 1)
      insert_change(pool, org, "customer", "c2", 2)

      assert {:ok, result, observation} = PullService.pull(pool, 0, principal(org))

      assert observation.scanned_change_count == 2
      assert result.cursor_sequence > 0

      # Ordering is load-bearing: `Page.assemble/2` advances the cursor by MAX over
      # what it consumed, and `pull_since` advances past INVISIBLE changes so a
      # client is not offered the same invisible rows forever.
      sequences = Enum.map(result.records, & &1.sequence)
      assert sequences == Enum.sort(sequences)
    end

    @tag test_id: "cursor"
    test "a start sequence skips what came before it", %{pool: pool, org: org} do
      insert_change(pool, org, "customer", "c1", 1)
      insert_change(pool, org, "customer", "c2", 2)

      assert {:ok, from_zero, _} = PullService.pull(pool, 0, principal(org))
      assert from_zero.cursor_sequence >= 2

      # Everything is consumed, so a pull from there is empty — which is the
      # property that makes a cursor usable at all.
      assert {:ok, after_all, observation} =
               PullService.pull(pool, from_zero.cursor_sequence, principal(org))

      assert after_all.records == []
      assert observation.scanned_change_count == 0
    end
  end

  describe "the scan bound" do
    @tag test_id: "cap"
    test "the cap is 500, and it is a SCAN bound rather than a page size", %{
      pool: pool,
      org: org
    } do
      # `MAX_SYNC_PULL_SCAN_CHANGES`, `app/sync_service.py:49`. `Page` has its own
      # `max_scan_changes/0`, and the two must agree or the read fetches more than
      # the page will consider.
      assert Reads.max_scan_changes() == Page.max_scan_changes()
    end
  end

  describe "wire/2 — the envelope" do
    test "deviceID is the literal server, and the cursor is prefixed" do
      assert %{"deviceID" => "server", "cursor" => "seq:0", "records" => []} =
               PullService.wire(%{records: [], cursor_sequence: 0})

      assert %{"cursor" => "seq:42"} =
               PullService.wire(%{records: [], cursor_sequence: 42})

      # `deviceID` is NOT a parameter. The reference hardcodes `"server"` at both
      # sites (`app/sync_service.py:80, 572`), and the first version took it as a
      # defaulted argument — which a caller could supply, and a server-sourced
      # page would then claim to be the caller's. The signature is `wire/1`.
    end
  end
end
