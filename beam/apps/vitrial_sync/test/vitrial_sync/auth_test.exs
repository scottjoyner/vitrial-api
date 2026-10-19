defmodule VitrialSync.AuthLiveTest do
  @moduledoc """
  `VitrialSync.Auth` against a real PostgreSQL 17 with the real migrations.

  The seam's whole job is four lookups in the right order feeding one decision
  function. What is worth testing is the ORDERING and the distinction between "no
  such token" and "the database is unreachable" — neither of which a unit test
  with hand-built rows would catch, because a unit test supplies the rows the real
  ordering was supposed to have produced.

  ## Every id varies per test, and that is the whole fixture design

  The first version used four literals — org, `mem-1`, `sess-mem-1`, and one
  bearer token — and seven of eight tests failed, for four distinct reasons,
  none of which named the collision:

  * `auth_sessions.access_token_hash` is UNIQUE (twice: a table constraint and an
    index), so one shared token meant the first test's row was the only one the
    lookup could ever find.
  * `auth_sessions.id` is the primary key, and `"sess-" <> membership` was
    `sess-mem-1` in every test.
  * `memberships.id` is the primary key, and `ON CONFLICT (id) DO UPDATE` meant
    each test overwrote the PREVIOUS test's membership with its own
    `organization_id`. The symptom was a happy-path test refused with
    `:session_membership_mismatch` — which reads like the seam being wrong and is
    a fixture collision.
  * `users.id` is referenced by a foreign key from `memberships.user_id`, so a
    membership cannot exist without one.

  The database is SHARED across runs and `System.unique_integer/1` restarts in
  every test process, so `:erlang.unique_integer/1` plus the test id is what makes
  a row re-creatable rather than merely unique within one run.

  Skips itself, loudly, without a server. `.github/workflows/ci.yml` fails the
  job on `NOT RUN`, so a skip can never read as a pass.
  """

  use ExUnit.Case, async: false

  alias VitrialAuth
  alias VitrialSync.Auth
  alias VitrialSync.Db.{Config, LiveDB, Pgconn, Pool}

  if reason = LiveDB.unavailable_reason() do
    IO.write(:user, "[db] auth live suite NOT RUN: #{reason}\n")
    @moduletag skip: reason
  end

  @url LiveDB.url_string()
  @now ~U[2026-10-05 12:00:00Z]
  @later ~U[2026-10-06 12:00:00Z]

  setup context do
    # ONE discriminator per test, from which every id derives, and it carries the
    # RUN's identity too.
    #
    # `:erlang.unique_integer/1` is monotonic for one VM's life, so it separates
    # tests WITHIN a run — but the database is shared ACROSS runs, and a second
    # run restarts the counter at 1 and produces byte-identical ids. That is the
    # collision the first version hit:
    #
    #     duplicate key value violates unique constraint
    #       "auth_sessions_access_token_hash_key"
    #     Key (access_token_hash)=(1bc90d1ab0fa5f8f...) already exists
    #
    # `ON CONFLICT (id) DO NOTHING` cannot absorb it, because the conflict is on
    # the HASH, not the id. `System.unique_integer([:positive, :monotonic])` is
    # documented as unique across the node's life, which is the property that is
    # actually needed here.
    unique =
      "#{context[:test_id]}-#{System.unique_integer([:positive, :monotonic])}"

    config =
      Config.load(url: @url, pool_size: 2, max_overflow: 1, pool_timeout_seconds: 10)

    name = Module.concat([AuthLiveTest, :pool])
    pool = start_supervised!({Pool, name: name, config: config, shutdown: 5_000}, id: name)

    fixture = %{
      org: "authlive-org-#{unique}",
      user: "authlive-user-#{unique}",
      membership_id: "authlive-mem-#{unique}",
      session_id: "authlive-sess-#{unique}",
      token: "bearer-token-#{unique}",
      pool: name
    }

    seed(pool, fixture)
    fixture
  end

  defp seed(pool, fixture) do
    Pool.with_conn(pool, fn conn ->
      {:ok, _} =
        Pgconn.query(
          conn,
          "INSERT INTO organizations (id, authorization_revision, name) VALUES ($1, 0, $2)
           ON CONFLICT (id) DO NOTHING",
          [fixture.org, fixture.org]
        )

      # `users` FIRST: `memberships.user_id` carries
      # `memberships_user_id_fkey REFERENCES users(id)` and `display_name` is NOT
      # NULL. A membership for a user that does not exist fails the foreign key,
      # and the wrapper reports the NEXT query as `:query_timeout` because the
      # connection was left mid-transaction — which is how a missing row presented
      # as a slow one.
      {:ok, _} =
        Pgconn.query(
          conn,
          "INSERT INTO users (id, display_name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
          [fixture.user, fixture.user]
        )

      # ALL FOUR list columns are `json`, not Postgres arrays —
      # `information_schema` says so and `app/models.py:45-46` agrees
      # (`roles: Mapped[list[dict]] = mapped_column(JSON, ...)`). Binding a native
      # Elixir array made the server read `{sync` as JSON and refuse with
      # `invalid input syntax for type json ... Token "sync" is invalid`.
      #
      # `$n::json` names the cast rather than inferring it from whatever the
      # driver sent, and there are FOUR DISTINCT parameter numbers: Postgres counts
      # distinct `$n` names, not references.
      #
      # Ten columns, ten expressions: $1..$4, two LITERAL `false`s, four JSON
      # params. An earlier version kept a fifth scalar ahead of the literals and
      # the server answered `INSERT has more expressions than target columns`
      # without naming a column.
      {:ok, _} =
        Pgconn.query(
          conn,
          "INSERT INTO memberships (id, organization_id, user_id, active, all_customers, all_projects, customer_ids, project_ids, roles, capabilities)
           VALUES ($1, $2, $3, $4, false, false, $5::json, $6::json, $7::json, $8::json)
           ON CONFLICT (id) DO NOTHING",
          [
            fixture.membership_id,
            fixture.org,
            fixture.user,
            true,
            ~s([]),
            ~s([]),
            ~s([]),
            ~s(["sync"])
          ]
        )

      # Timestamps as ISO STRINGS with explicit casts. The driver encodes a `~U[…]`
      # sigil as an Elixir term the server cannot read, and that surfaced as
      # `:query_timeout` on the FOLLOWING statement for the same
      # mid-transaction reason.
      #
      # `access_token_hash` is `varchar(64)` and SHA-256 hex is exactly 64.
      {:ok, _} =
        Pgconn.query(
          conn,
          "INSERT INTO auth_sessions (id, organization_id, membership_id, user_id, access_token_hash, issued_at, expires_at, revoked_at)
           VALUES ($1, $2, $3, $4, $5, $6::timestamptz, $7::timestamptz, NULL)
           ON CONFLICT (id) DO NOTHING",
          [
            fixture.session_id,
            fixture.org,
            fixture.membership_id,
            fixture.user,
            VitrialAuth.token_hash(fixture.token),
            DateTime.to_iso8601(@now),
            DateTime.to_iso8601(@later)
          ]
        )

      :ok
    end)
  end

  defp expire_session(pool, fixture) do
    Pool.with_conn(pool, fn conn ->
      {:ok, _} =
        Pgconn.query(
          conn,
          "UPDATE auth_sessions SET expires_at = $1::timestamptz WHERE id = $2",
          [DateTime.to_iso8601(@now), fixture.session_id]
        )

      :ok
    end)
  end

  defp revoke_session(pool, fixture) do
    Pool.with_conn(pool, fn conn ->
      {:ok, _} =
        Pgconn.query(
          conn,
          "UPDATE auth_sessions SET revoked_at = $1::timestamptz WHERE id = $2",
          [DateTime.to_iso8601(@now), fixture.session_id]
        )

      :ok
    end)
  end

  defp deactivate_membership(pool, fixture) do
    Pool.with_conn(pool, fn conn ->
      {:ok, _} =
        Pgconn.query(conn, "UPDATE memberships SET active = false WHERE id = $1", [
          fixture.membership_id
        ])

      :ok
    end)
  end

  describe "authenticate/3 — the happy path" do
    @tag test_id: "live"
    test "a live bearer token resolves to a Principal carrying the membership's scope",
         fixture do
      assert {:ok, principal, event} =
               Auth.authenticate(fixture.pool, "Bearer #{fixture.token}", @now)

      assert principal.organization_id == fixture.org
      assert principal.membership_id == fixture.membership_id
      assert principal.user_id == fixture.user
      assert principal.authorization_revision == 0

      # The capability set came from the memberships ROW, not from the token.
      assert MapSet.member?(principal.capabilities, "sync")

      # The event is RETURNED rather than emitted: a pure function cannot observe
      # anything by logging, and `app/auth.py:73-74` logs right after building the
      # principal.
      assert event.event == "auth.session_authenticated"
    end

    @tag test_id: "case"
    test "the scheme is case-insensitive, as HTTP requires", fixture do
      assert {:ok, _principal, _event} =
               Auth.authenticate(fixture.pool, "bearer #{fixture.token}", @now)

      assert {:ok, _principal, _event} =
               Auth.authenticate(fixture.pool, "BEARER #{fixture.token}", @now)
    end
  end

  describe "authenticate/3 — refusals, each distinguishable" do
    @tag test_id: "unknown"
    test "an unknown token is authentication_required", fixture do
      # A digest nobody stored. NOT a load failure, and the distinction is the
      # point: collapsing them would tell a client its session expired when the
      # database was merely unreachable.
      assert {:error, :authentication_required} =
               Auth.authenticate(fixture.pool, "Bearer never-stored", @now)
    end

    @tag test_id: "noscheme"
    test "a non-bearer header never reaches the digest computation", fixture do
      assert {:error, :authentication_required} =
               Auth.authenticate(fixture.pool, nil, @now)

      assert {:error, :authentication_required} =
               Auth.authenticate(fixture.pool, "Basic abc", @now)

      assert {:error, :authentication_required} =
               Auth.authenticate(fixture.pool, "Bearer", @now)
    end

    @tag test_id: "expired"
    test "an expired session is refused, and the boundary is exclusive", fixture do
      expire_session(fixture.pool, fixture)

      # `expires_at <= now` (app/auth.py:46), so expiring exactly AT now is refused.
      assert {:error, :session_expired_or_revoked} ==
               Auth.authenticate(fixture.pool, "Bearer #{fixture.token}", @now)

      # One second earlier is not.
      assert {:ok, _principal, _event} =
               Auth.authenticate(
                 fixture.pool,
                 "Bearer #{fixture.token}",
                 DateTime.add(@now, -1, :second)
               )
    end

    @tag test_id: "revoked"
    test "a revoked session is refused, and says the same as an expired one", fixture do
      revoke_session(fixture.pool, fixture)

      # Deliberately the same refusal: a caller must not be able to tell which it
      # was, and "session expired or revoked" is the reference's own single message.
      assert {:error, :session_expired_or_revoked} ==
               Auth.authenticate(fixture.pool, "Bearer #{fixture.token}", @now)
    end

    @tag test_id: "inactive"
    test "an inactive membership is refused, distinctly from a missing session", fixture do
      deactivate_membership(fixture.pool, fixture)

      assert {:error, :membership_inactive} ==
               Auth.authenticate(fixture.pool, "Bearer #{fixture.token}", @now)
    end
  end

  describe "the token is stored as a digest, never as itself" do
    @tag test_id: "digest"
    test "the row holds a hash, so a disclosure yields no usable credential", fixture do
      stored =
        Pool.with_conn(fixture.pool, fn conn ->
          assert {:ok, %{rows: [[value]]}} =
                   Pgconn.query(
                     conn,
                     "SELECT access_token_hash FROM auth_sessions WHERE id = $1",
                     [fixture.session_id]
                   )

          value
        end)

      refute stored == fixture.token
      assert stored == VitrialAuth.token_hash(fixture.token)

      # And the digest is not a usable credential.
      assert {:error, :authentication_required} =
               Auth.authenticate(fixture.pool, "Bearer #{stored}", @now)
    end
  end
end
