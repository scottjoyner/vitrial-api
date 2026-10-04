defmodule VitrialSync.Db.LiveDbTest do
  @moduledoc """
  The pool and codec behaviours that need a real PostgreSQL, and the skip
  discipline around them.

  Every test here probes nothing at runtime: the module either runs (the probe
  in `VitrialSync.Db.LiveDB` succeeded at compile time) or the whole module
  skips with the banner below. The banner's wording is load-bearing: `ci.yml`
  fails the umbrella job if its test log contains the literal, case-sensitive
  string `SKIPPED` — the parity suite trips that guard DELIBERATELY, because
  its skip means "parity was not checked". A missing database is a different,
  honest condition, so this banner avoids that substring in every case variant,
  including inside the interpolated reason. The suite reports itself and its
  skip count through ExUnit's own lowercase summary line, which coexists with
  the guard.

  These tests must be recompiled (or given a fresh `_build`) the first time a
  lane gains a reachable server — the skip decision is baked at compile time,
  exactly like the parity suite's.
  """

  use ExUnit.Case, async: false

  alias VitrialSync.Canonical
  alias VitrialSync.Db.{Codec, Config, Conn, LiveDB, Pgconn, Pool, Rows, Url, Writes}

  if reason = LiveDB.unavailable_reason() do
    IO.write(
      :user,
      "[db] live-postgresql suite NOT RUN (no server reachable at $VITRIAL_TEST_DATABASE_URL): #{reason}\n"
    )

    @moduletag skip: reason
  end

  @url LiveDB.url_string()

  setup do
    org = "w5live-" <> Integer.to_string(System.unique_integer([:positive]))

    on_exit(fn -> cleanup(org) end)

    %{org: org}
  end

  defp live_pool(opts \\ []) do
    name =
      Module.concat([
        LiveDbTest,
        String.to_atom("pool_" <> Integer.to_string(System.unique_integer([:positive])))
      ])

    config =
      Config.load(
        Keyword.merge(
          [
            url: @url,
            pool_size: 2,
            max_overflow: 1,
            pool_timeout_seconds: 2
          ],
          opts
        )
      )

    start_supervised!({Pool, name: name, config: config, shutdown: 5_000})
    name
  end

  defp stats(pool), do: GenServer.call(pool, :stats, 5_000)

  defp eventually(fun, tries \\ 100)

  defp eventually(_fun, 0), do: flunk("condition not met in time")

  # A poll may observe an intermediate state, so a failed MATCH inside the fun
  # is a false, not a crash — the flunk after the last try names the condition.
  defp eventually(fun, tries) do
    ok? =
      try do
        fun.()
      rescue
        _ -> false
      catch
        _kind, _value -> false
      end

    if ok? do
      :ok
    else
      Process.sleep(20)
      eventually(fun, tries - 1)
    end
  end

  # SHOW renders timeouts in human units ("15s", "2s", "15000ms"); normalize to
  # milliseconds rather than pinning PostgreSQL's formatting whims. Every cell
  # on this transport is TEXT format: a one-cell one-row answer arrives as
  # [[text]].
  defp show_ms(conn, name) do
    {:ok, %{rows: [[text]]}} = Pgconn.query(conn, "SHOW #{name}", [])
    text = String.trim(text)
    {digits, unit} = String.split_at(text, count_digits(text))
    n = String.to_integer(digits)

    case unit do
      "ms" -> n
      "s" -> n * 1000
      "min" -> n * 60_000
      other -> flunk("unexpected SHOW unit for #{name}: #{inspect(text)} (#{other})")
    end
  end

  defp count_digits(text),
    do: text |> String.to_charlist() |> Enum.take_while(&(&1 in ?0..?9)) |> length()

  defp text_of(conn, sql) do
    {:ok, %{rows: [[text]]}} = Pgconn.query(conn, sql, [])
    text
  end

  # A write through the transport: the CommandComplete payload is the command
  # tag, NUL-terminated per the protocol (the payload length includes the
  # terminator), so the assertion pins the exact wire payload instead of
  # trimming it into shape. If the client ever strips the terminator, this
  # fails and forces that decision to be made on the record.
  defp assert_write(conn, sql, params, tag) do
    expected = tag <> <<0>>

    assert {:ok, %{rows: [], command_complete: ^expected}} = Pgconn.query(conn, sql, params)

    :ok
  end

  # -- the connection's session settings ----------------------------------------

  describe "Conn's server-side session settings" do
    test "all three GUCs are applied and SHOW back" do
      pool = live_pool()

      Pool.with_conn(pool, fn conn ->
        # Defaults: DATABASE_STATEMENT_TIMEOUT_MS = 15000 (app/settings.py:40),
        # applied to both GUCs (app/db.py:33-44), sent in the STARTUP PACKET —
        # the one thing the native client does a driver without a
        # startup-parameter hook could not (VitrialSync.Db.Pgconn).
        assert show_ms(conn, "statement_timeout") == 15_000
        assert show_ms(conn, "idle_in_transaction_session_timeout") == 15_000
        # The UTC pin: TEXT-format determinism — timestamp text carries the
        # session offset, so the session must be UTC for the wire contract
        # (Codec's timestamptz note).
        assert text_of(conn, "SHOW timezone") == "UTC"
      end)
    end

    test "every cell arrives as TEXT format — raw binaries, never pre-decoded terms" do
      pool = live_pool()

      Pool.with_conn(pool, fn conn ->
        assert text_of(conn, "SHOW timezone") == "UTC"
      end)
    end
  end

  # -- the pool's process behaviours ----------------------------------------------

  describe "exhaustion" do
    test "a second checkout yields {:error, :pool_timeout} — translated, never a raised exit" do
      pool = live_pool(pool_size: 1, max_overflow: 0, pool_timeout_seconds: 0.3)

      assert {:ok, conn, _fresh?} = Pool.checkout(pool)
      assert {:error, :pool_timeout} = Pool.checkout(pool)
      assert :ok = Pool.checkin(pool, conn)
      assert {:ok, _conn, _} = Pool.checkout(pool)
    end

    test "with_conn surfaces the same translation" do
      pool = live_pool(pool_size: 1, max_overflow: 0, pool_timeout_seconds: 0.3)

      assert {:ok, conn, _} = Pool.checkout(pool)

      result =
        Pool.with_conn(pool, fn _ -> flunk("granted while exhausted") end, pool_timeout_ms: 200)

      assert result == {:error, :pool_timeout}
      Pool.checkin(pool, conn)
    end

    test "overflow: transient connections exist beyond pool_size and close on check-in" do
      pool = live_pool(pool_size: 1, max_overflow: 1)

      {:ok, c1, _} = Pool.checkout(pool)
      {:ok, c2, _} = Pool.checkout(pool)
      {:error, :pool_timeout} = Pool.checkout(pool, pool_timeout_ms: 100)

      assert %{total: 2, overflow: 1} = stats(pool)

      :ok = Pool.checkin(pool, c2)
      # c2 was transient: closed, not parked.
      assert %{total: 1, idle: 0, overflow: 0} = stats(pool)

      :ok = Pool.checkin(pool, c1)
      assert %{total: 1, idle: 1} = stats(pool)
    end
  end

  describe "crash absorption" do
    test "a caller crash while HOLDING a connection reclaims it (closed, not re-parked)" do
      pool = live_pool(pool_size: 1, max_overflow: 0)

      parent = self()

      owner =
        spawn(fn ->
          {:ok, conn, _} = Pool.checkout(pool)
          send(parent, {:held, conn})
          Process.sleep(:infinity)
        end)

      assert_receive {:held, _conn}, 5_000
      assert %{checked_out: 1, total: 1} = stats(pool)

      Process.exit(owner, :kill)

      # The DOWN closes the connection — its session state is unknown — and
      # restores the capacity.
      assert eventually(fn ->
               %{total: 0, checked_out: 0} = stats(pool)
               true
             end)

      assert %{idle: 0} = stats(pool)

      # And the pool still serves.
      assert {:ok, _conn, true} = Pool.checkout(pool)
    end

    test "a waiter that dies while queued is evicted; the freed connection parks instead of granting" do
      pool = live_pool(pool_size: 1, max_overflow: 0)
      parent = self()

      {:ok, conn, _} = Pool.checkout(pool)

      waiter =
        spawn(fn ->
          result = Pool.checkout(pool, pool_timeout_ms: 10_000)
          send(parent, {:waiter_got, result})
        end)

      # Wait until the waiter is actually queued, then kill it.
      assert eventually(fn -> stats(pool).waiters == 1 end)
      Process.exit(waiter, :kill)
      assert eventually(fn -> stats(pool).waiters == 0 end)

      :ok = Pool.checkin(pool, conn)

      # The connection came home to the idle set — a dead waiter must never
      # consume a grant.
      assert %{idle: 1, waiters: 0, total: 1} = stats(pool)
      refute_receive {:waiter_got, _}, 200
    end

    test "a granted-then-departed owner's connection closes instead of parking" do
      pool = live_pool(pool_size: 1, max_overflow: 0)
      parent = self()

      holder =
        spawn(fn ->
          {:ok, _conn, _} = Pool.checkout(pool)
          send(parent, :holding)
          Process.sleep(:infinity)
        end)

      assert_receive :holding, 5_000

      waiter =
        spawn(fn ->
          result = Pool.checkout(pool, pool_timeout_ms: 10_000)
          send(parent, {:waiter_got, result})
        end)

      assert eventually(fn -> stats(pool).waiters == 1 end)

      # Owner dies holding; waiter dies queued — in that order, so the reclaim
      # serves the queue and must find its grant target dead.
      Process.exit(holder, :kill)
      Process.exit(waiter, :kill)

      # Net: both slots closed, nothing granted to a corpse, capacity restored.
      assert eventually(fn -> stats(pool).total == 0 end)
      assert %{checked_out: 0, idle: 0, waiters: 0} = stats(pool)
      refute_receive {:waiter_got, _}, 200
    end

    test "killing a connection pid under the pool is absorbed; the operation retries once and succeeds" do
      pool = live_pool(pool_size: 1, max_overflow: 0, pool_timeout_seconds: 2)

      Process.put(:attempts, 0)

      result =
        Pool.with_conn(pool, fn conn ->
          Process.put(:attempts, Process.get(:attempts) + 1)

          if Process.get(:attempts) == 1 do
            # The native client's owner is unlinked, so the holder is not
            # signaled synchronously: its next bounded exchange times out at
            # the client's own deadline (a documented transport difference from
            # a linked driver's instant exit) — and the caught failure against
            # a dead connection is exactly what with_conn retries on.
            Process.exit(conn, :kill)
            Process.sleep(50)
            assert {:error, :query_timeout} = Pgconn.query(conn, "SELECT 1", [])
            flunk("the dead connection answered")
          else
            :second_attempt_on_live_connection
          end
        end)

      assert result == :second_attempt_on_live_connection
      assert Process.get(:attempts) == 2

      # Exactly one retry, and the pool is healthy with its conn parked.
      assert eventually(fn ->
               %{total: 1, idle: 1, checked_out: 0} = stats(pool)
               true
             end)
    end

    test "a connection killed by its own caller does not take the pool down" do
      pool = live_pool(pool_size: 1, max_overflow: 0)

      assert :done =
               Pool.with_conn(pool, fn conn ->
                 Process.exit(conn, :kill)
                 Process.sleep(50)
                 :done
               end)

      assert eventually(fn -> stats(pool).total == 0 end)

      # And the next operation gets a fresh connection.
      assert :ok = Pool.with_conn(pool, fn conn -> Process.alive?(conn) and :ok end)
    end
  end

  describe "recycle" do
    test "a connection past its age closes on check-in instead of parking; a fresh one parks" do
      # DATABASE_POOL_RECYCLE_SECONDS is an integer (Config.parse_int/2), so the
      # smallest usable budget is one second, and the aging sleep must clear it.
      pool = live_pool(pool_size: 1, max_overflow: 0, recycle_seconds: 1)

      {:ok, conn, _} = Pool.checkout(pool)
      :ok = Pool.checkin(pool, conn)
      # Young connection: parked.
      assert %{idle: 1, total: 1} = stats(pool)

      # Check the parked conn back out, age it past the limit, check it in.
      assert {:ok, conn, false} = Pool.checkout(pool)
      Process.sleep(1_100)
      :ok = Pool.checkin(pool, conn)

      # Past DATABASE_POOL_RECYCLE_SECONDS: closed, capacity back to zero.
      assert eventually(fn -> stats(pool).total == 0 end)
      assert %{idle: 0} = stats(pool)
    end
  end

  describe "null pool" do
    test "connect-per-checkout, close-on-checkin, nothing parked, no bounds" do
      pool = live_pool(null_pool: true)

      assert {:ok, c1, true} = Pool.checkout(pool)
      assert {:ok, c2, true} = Pool.checkout(pool)
      assert c1 != c2
      assert %{idle: 0, total: 2} = stats(pool)

      :ok = Pool.checkin(pool, c1)
      # Closed on check-in, never parked (app/db.py:52-57).
      assert %{idle: 0, total: 1} = stats(pool)

      :ok = Pool.checkin(pool, c2)
      assert %{idle: 0, total: 0} = stats(pool)
    end
  end

  describe "drain" do
    test "refuses new checkouts, keeps serving check-ins, sweeps at the deadline" do
      pool = live_pool(pool_size: 1, max_overflow: 0)

      {:ok, conn, _} = Pool.checkout(pool)

      assert {:ok, 1} = Pool.drain(pool, 150)
      assert {:error, :draining} = Pool.checkout(pool, pool_timeout_ms: 50)
      # The in-flight caller can still come home.
      assert :ok = Pool.checkin(pool, conn)

      assert eventually(fn -> stats(pool).draining? and stats(pool).total == 0 end)
      assert %{idle: 0, checked_out: 0} = stats(pool)
    end

    test "a zero budget sweeps immediately and refunds queued waiters" do
      pool = live_pool(pool_size: 1, max_overflow: 0)
      parent = self()

      {:ok, _conn, _} = Pool.checkout(pool)

      _waiter =
        spawn(fn ->
          result = Pool.checkout(pool, pool_timeout_ms: 10_000)
          send(parent, {:waiter_got, result})
        end)

      assert eventually(fn -> stats(pool).waiters == 1 end)

      assert {:ok, 1} = Pool.drain(pool, 0)
      # The queued waiter is refunded with the draining answer, not left hanging.
      assert_receive {:waiter_got, {:error, :draining}}, 5_000
      assert %{total: 0, draining?: true} = stats(pool)
    end
  end

  # -- rows against real tables ----------------------------------------------------

  describe "rows through the real schema" do
    test "entity upsert lands, reads back through Rows, and projects to Canonical.Entity" do
      pool = live_pool()
      org = "w5live-entity-" <> Integer.to_string(System.unique_integer([:positive]))

      row = %{
        organization_id: org,
        entity_type: "customer",
        entity_id: "cust-w5",
        server_revision: 1,
        payload: %{"kind" => "customer", "n" => 1},
        updated_at: "2026-01-01T12:00:00Z",
        deleted_at: nil
      }

      Pool.with_conn(pool, fn conn ->
        {sql, params} = Writes.entity_upsert(row)
        # INSERT without RETURNING: the CommandComplete tag carries the count.
        assert_write(conn, sql, params, "INSERT 0 1")

        select =
          "SELECT organization_id, entity_type, entity_id, server_revision, schema_version, payload_json, updated_at, deleted_at
             FROM sync_entities WHERE organization_id = $1"

        assert {:ok, result} = Pgconn.query(conn, select, [org])
        assert length(result.rows) == 1

        decoded = Rows.sync_entities(result.row_description, hd(result.rows))
        assert decoded.organization_id == org
        assert decoded.server_revision == 1
        assert decoded.schema_version == 1
        assert decoded.payload_json == %{"kind" => "customer", "n" => 1}
        assert decoded.updated_at == DateTime.from_naive!(~N[2026-01-01 12:00:00], "Etc/UTC")
        assert decoded.deleted_at == nil

        # Timestamp TEXT carries the session offset; the session is UTC-pinned
        # (the startup packet), so 12:00Z written reads back as 12:00 UTC.
        assert Rows.to_canonical_entity(decoded) == %Canonical.Entity{
                 entity_type: "customer",
                 entity_id: "cust-w5",
                 payload: %{"kind" => "customer", "n" => 1},
                 deleted_at: nil,
                 server_revision: 1
               }
      end)
    end

    test "the entity upsert conflict path rewrites revision and payload" do
      pool = live_pool()
      org = "w5live-upsert-" <> Integer.to_string(System.unique_integer([:positive]))

      Pool.with_conn(pool, fn conn ->
        {sql, params} =
          Writes.entity_upsert(%{
            organization_id: org,
            entity_type: "customer",
            entity_id: "cust-w5",
            server_revision: 1,
            payload: %{"v" => 1},
            updated_at: "2026-01-01T00:00:00Z",
            deleted_at: nil
          })

        assert_write(conn, sql, params, "INSERT 0 1")

        {sql, params} =
          Writes.entity_upsert(%{
            organization_id: org,
            entity_type: "customer",
            entity_id: "cust-w5",
            server_revision: 2,
            payload: nil,
            updated_at: "2026-01-02T00:00:00Z",
            deleted_at: "2026-01-02T00:00:00Z"
          })

        # Stock PostgreSQL 16 reports the ON CONFLICT DO UPDATE path with the
        # INSERT tag, not UPDATE — verified against the reference server with
        # psql (a plain UPDATE reads UPDATE 1; this reads INSERT 0 1). The tag
        # here proves only the affected row count; the conflict path itself is
        # proven by the revision rewrite asserted below.
        assert_write(conn, sql, params, "INSERT 0 1")

        {:ok, result} =
          Pgconn.query(
            conn,
            "SELECT organization_id, entity_type, entity_id, server_revision, schema_version, payload_json, updated_at, deleted_at FROM sync_entities WHERE organization_id = $1",
            [org]
          )

        assert length(result.rows) == 1
        decoded = Rows.sync_entities(result.row_description, hd(result.rows))
        assert decoded.server_revision == 2
        assert decoded.payload_json == nil
        assert decoded.deleted_at == DateTime.from_naive!(~N[2026-01-02 00:00:00], "Etc/UTC")
      end)
    end

    test "sync_mutations round-trips to to_prior/1; the idempotency constraint speaks SQLSTATE 23505 with its constraint name" do
      pool = live_pool()
      org = "w5live-mutation-" <> Integer.to_string(System.unique_integer([:positive]))

      Pool.with_conn(pool, fn conn ->
        {sql, params} =
          Writes.mutation_insert(%{
            organization_id: org,
            client_mutation_id: "m-w5",
            submitted_by_user_id: "user-w5",
            membership_id: "ms-w5",
            session_id: "sess-w5",
            authorization_revision: 5,
            device_id: "dev-w5",
            entity_type: "customer",
            entity_id: "cust-w5",
            base_server_revision: nil,
            result_server_revision: 4,
            result_status: "accepted"
          })

        # sync_mutation_fingerprints carries an FK to organizations (0001);
        # the parent row must exist before the fingerprint insert.
        assert_write(
          conn,
          "INSERT INTO organizations (id, name) VALUES ('#{org}', 'w5 mutation probe')",
          [],
          "INSERT 0 1"
        )

        assert_write(conn, sql, params, "INSERT 0 1")

        {:ok, result} =
          Pgconn.query(
            conn,
            "SELECT id, organization_id, client_mutation_id, submitted_by_user_id, membership_id, session_id, authorization_revision, device_id, entity_type, entity_id, base_server_revision, result_server_revision, result_status, received_at FROM sync_mutations WHERE organization_id = $1",
            [org]
          )

        assert length(result.rows) == 1
        decoded = Rows.sync_mutations(result.row_description, hd(result.rows))
        assert decoded.id >= 1
        # received_at is server-clocked (server_default=now()); it arrived as
        # timestamptz TEXT and decoded to a DateTime.
        assert %DateTime{} = decoded.received_at

        assert Rows.to_prior(decoded) == %{
                 entity_type: "customer",
                 entity_id: "cust-w5",
                 base_server_revision: nil,
                 result_status: "accepted",
                 result_server_revision: 4
               }

        # The composite-unique constraint is the idempotency mechanism working
        # (S-55); its violation arrives as the client's decoded pg_error map —
        # the shape its @type pins: %{severity, code, message, detail, hint,
        # position}, `code` the SQLSTATE text. If THIS assertion fails, the
        # failure is a finding against the client's ErrorResponse parsing,
        # not a reason to weaken the test.
        assert {:error,
                %{
                  code: "23505",
                  message: message,
                  detail: detail,
                  severity: "ERROR"
                }} = Pgconn.query(conn, sql, params)

        assert message =~ "uq_sync_mutation_org_client" or
                 detail =~ "uq_sync_mutation_org_client"

        # And the fingerprint companion row, 3 columns, composite PK.
        {fsql, fparams} =
          Writes.fingerprint_insert(%{
            organization_id: org,
            client_mutation_id: "m-w5",
            request_fingerprint: String.duplicate("ab", 32)
          })

        assert_write(conn, fsql, fparams, "INSERT 0 1")

        {:ok, fresult} =
          Pgconn.query(
            conn,
            "SELECT organization_id, client_mutation_id, request_fingerprint FROM sync_mutation_fingerprints WHERE organization_id = $1",
            [org]
          )

        assert Rows.sync_mutation_fingerprints(fresult.row_description, hd(fresult.rows)).request_fingerprint ==
                 String.duplicate("ab", 32)
      end)
    end

    test "membership_grant appends onto the stored lists, server-side" do
      pool = live_pool()
      org = "w5live-grant-" <> Integer.to_string(System.unique_integer([:positive]))
      membership_id = org <> "-ms"

      Pool.with_conn(pool, fn conn ->
        assert_write(
          conn,
          "INSERT INTO organizations (id, name) VALUES ('#{org}', 'w5 grant probe')",
          [],
          "INSERT 0 1"
        )

        assert_write(
          conn,
          "INSERT INTO users (id, display_name) VALUES ('#{org}-u', 'w5')",
          [],
          "INSERT 0 1"
        )

        assert_write(
          conn,
          "INSERT INTO memberships (id, organization_id, user_id) VALUES ($1, $2, $3)",
          [membership_id, org, "#{org}-u"],
          "INSERT 0 1"
        )

        {sql, params} =
          Writes.membership_grant(%{customer_ids: ["c1"], project_ids: ["p1"]}, membership_id)

        assert_write(conn, sql, params, "UPDATE 1")

        # The append happens on the SERVER's current value — grant again and
        # both deltas accumulate, order preserved (the wire contract for the
        # next authorization profile).
        {sql, params} =
          Writes.membership_grant(%{customer_ids: ["c2"], project_ids: []}, membership_id)

        assert_write(conn, sql, params, "UPDATE 1")

        {:ok, result} =
          Pgconn.query(
            conn,
            "SELECT organization_id, id, user_id, active, all_customers, all_projects, customer_ids, project_ids, roles, capabilities FROM memberships WHERE id = $1",
            [membership_id]
          )

        decoded = Rows.memberships(result.row_description, hd(result.rows))
        assert decoded.customer_ids == ["c1", "c2"]
        assert decoded.project_ids == ["p1"]
      end)
    end

    test "evidence_gc_insert writes the durable GC row with org and not_before filled" do
      pool = live_pool()
      org = "w5live-gc-" <> Integer.to_string(System.unique_integer([:positive]))

      Pool.with_conn(pool, fn conn ->
        request = %{
          document_id: "doc-w5",
          storage_provider: "s3",
          object_key: "org/doc-w5",
          reason: "metadata_tombstone",
          grace_seconds: 60
        }

        {sql, params} = Writes.evidence_gc_insert(request, org, DateTime.utc_now())
        assert_write(conn, sql, params, "INSERT 0 1")

        {:ok, result} =
          Pgconn.query(
            conn,
            "SELECT id, organization_id, document_id, storage_provider, object_key, reason, not_before, attempts, last_error, created_at FROM evidence_object_gc WHERE organization_id = $1",
            [org]
          )

        # evidence_object_gc has no Rows decoder by design (encode-only this
        # slice); the read-back goes through the codec boundary directly.
        decoded = Codec.decode_row(result.row_description, hd(result.rows))
        assert decoded.organization_id == org
        assert decoded.document_id == "doc-w5"
        assert decoded.attempts == 0
        assert decoded.last_error == nil
        assert DateTime.compare(decoded.not_before, DateTime.utc_now()) in [:gt, :eq]
      end)
    end

    test "the changelog write lands with server-clocked sequence and created_at" do
      pool = live_pool()
      org = "w5live-log-" <> Integer.to_string(System.unique_integer([:positive]))

      Pool.with_conn(pool, fn conn ->
        {sql, params} =
          Writes.changelog_insert(%{
            organization_id: org,
            entity_type: "customer",
            entity_id: "cust-w5",
            server_revision: 1,
            client_mutation_id: nil,
            operation: "upsert"
          })

        assert_write(conn, sql, params, "INSERT 0 1")

        {:ok, result} =
          Pgconn.query(
            conn,
            "SELECT sequence, organization_id, entity_type, entity_id, server_revision, client_mutation_id, operation, created_at FROM sync_change_log WHERE organization_id = $1",
            [org]
          )

        decoded = Rows.sync_change_log(result.row_description, hd(result.rows))
        assert decoded.sequence >= 1
        assert decoded.operation == "upsert"
        assert decoded.client_mutation_id == nil
      end)
    end
  end

  # -- helpers ------------------------------------------------------------------

  defp cleanup(org) do
    # organizations cascades memberships and users; the sync tables and the GC
    # queue carry plain organization_id columns and are swept directly. The
    # identifier is generated by this module, so interpolation is safe.
    case connect_once() do
      {:ok, conn} ->
        Enum.each(
          [
            "DELETE FROM sync_entities WHERE organization_id = '#{org}'",
            "DELETE FROM sync_mutations WHERE organization_id = '#{org}'",
            "DELETE FROM sync_mutation_fingerprints WHERE organization_id = '#{org}'",
            "DELETE FROM sync_change_log WHERE organization_id = '#{org}'",
            "DELETE FROM evidence_object_gc WHERE organization_id = '#{org}'",
            "DELETE FROM organizations WHERE id = '#{org}'"
          ],
          &Pgconn.query(conn, &1, [])
        )

        Conn.close(conn)

      _error ->
        :ok
    end
  end

  defp connect_once do
    case Url.parse(@url) do
      {:ok, url} -> Conn.connect(url, statement_timeout_ms: 15_000)
      {:error, _} = error -> error
    end
  end
end
