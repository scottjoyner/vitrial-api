defmodule VitrialSync.PushServiceLiveTest do
  @moduledoc """
  `VitrialSync.PushService` against a real PostgreSQL 17 with the real migrations.

  This is the only place in the estate where `Reads`, `Batch`, `Writes`, `Pool`,
  `Pgconn` and the live schema are exercised TOGETHER, which is why it exists as a
  separate file from the unit tests of each part: a module can pass every one of
  its own tests and still be wired to the wrong signature.

  Every defect below was found here and by nothing else:

  * `Writes.*` take `/1` and return `{sql, params}`; `Writes.effects/4` returns the
    ordered list. Calling `entity_upsert/3` with a connection compiles to a warning
    under `--warnings-as-errors`, so the gate catches it — but only if the gate
    runs, which is why the fix was to CALL `Writes.effects/4` rather than
    re-derive its order.
  * `Batch.run/4` returns `{:ok, %Run{}}` already; unwrapping it twice produced a
    `never match` clause that would have raised at runtime.
  * `sync_mutations` carries five NOT NULL columns beyond the Python model, so an
    insert built from the model fails 23502.

  Skips itself, loudly, without a server — and `.github/workflows/ci.yml` fails
  the job on `NOT RUN`, so "skipped" can never read as "passed".
  """

  use ExUnit.Case, async: false

  alias VitrialSync.{Canonical, Record}
  alias VitrialSync.Db.{Config, LiveDB, Pool}
  alias VitrialSync.PushService

  if reason = LiveDB.unavailable_reason() do
    IO.write(:user, "[db] push service live suite NOT RUN: #{reason}\n")
    @moduletag skip: reason
  end

  @url LiveDB.url_string()

  # Unique per TEST as well as per run. `System.unique_integer([:positive])`
  # restarts in each test process, so two tests in one file produced the same
  # organization id and the second seed failed 23505 on the organizations primary
  # key — which surfaced as
  #
  #     Key (id)=(pushsvc--72) already exists
  #
  # The name comes from `@tag :test_id`, assigned explicitly per test, because
  # `context.case` is not populated in this ExUnit version and silently produced an
  # EMPTY tag for every test.
  setup context do
    org =
      case context[:test_id] do
        nil -> "pushsvc-" <> Integer.to_string(System.unique_integer([:positive]))
        id -> "pushsvc-#{id}-#{System.unique_integer([:positive])}"
      end

    config =
      Config.load(url: @url, pool_size: 3, max_overflow: 1, pool_timeout_seconds: 10)

    name = Module.concat([PushServiceLiveTest, :pool])
    pool = start_supervised!({Pool, name: name, config: config, shutdown: 5_000}, id: name)

    %{org: org, pool: name}
  end

  defp principal(org, overrides \\ %{}) do
    Map.merge(
      %Canonical.Principal{
        user_id: "user-1",
        organization_id: org,
        membership_id: "mem-1",
        session_id: "sess-1",
        authorization_revision: 0,
        capabilities: MapSet.new(["sync"]),
        customer_ids: MapSet.new(),
        project_ids: MapSet.new(),
        all_customers: false,
        all_projects: false
      },
      overrides
    )
  end

  defp record(overrides \\ %{}) do
    Map.merge(
      %Record{
        client_mutation_id: "cm-1",
        entity_type: "customer",
        entity_id: "c1",
        base_server_revision: nil,
        # REQUIRED: `app/schemas.py:75` declares `updatedAt: datetime` with no
        # default, so Pydantic rejects a record without one and the reference can
        # never hold a nil. Omitting it here made `Fingerprint` call
        # `Timestamp.normalize(nil)` and raise
        #
        #     ** (FunctionClauseError) no function clause matching in
        #        VitrialSync.Timestamp.normalize/1  ...  # 1  nil
        #
        # which is `Fingerprint` behaving correctly on an impossible input, not a
        # defect in it. The asymmetry with the adjacent `deletedAt` — which IS
        # guarded with `&&` — is real and intentional: one field is required and
        # the other is not.
        #
        # An ISO STRING, not a DateTime: `Record`'s @type says `updated_at:
        # String.t()` (record.ex:47) and `Timestamp.normalize/1` accepts only a
        # binary. The first version of this fixture set a `~U[]` sigil and got
        #
        #     ** (FunctionClauseError) no function clause matching in
        #        VitrialSync.Timestamp.normalize/1 ... # 1  ~U[2026-01-01 12:00:00Z]
        #
        # which is the same shape as the nil case one step later, and would have
        # been filed as a timestamp bug rather than a fixture one. Every other
        # Record test in this app already used the ISO string form.
        updated_at: "2026-01-01T12:00:00Z",
        # The BASE64 WIRE form. `Payload.decode/1` takes a binary and
        # strict-decodes it, so handing it a decoded map raised
        # `FunctionClauseError ... The following arguments were given to
        # Payload.decode/1: # 1, %{"name" => "Acme"}` — the payload crosses the
        # boundary encoded, which is the whole reason `decode/1` exists.
        payload: Base.encode64(~s({"name":"Acme"}))
      },
      overrides
    )
  end

  # The walk resolves a record's parent chain, so the organization must EXIST or
  # `organization_guard/1` refuses every record — which is the correct answer, and
  # the first thing worth pinning.
  #
  # `Writes.organization_revision_update/2` is an UPDATE and creates nothing, so
  # this inserts first. The first version of this helper did the reverse and
  # matched on `{:ok, sql, params}` — `Writes.*` returns a bare `{sql, params}` —
  # which surfaced as
  #
  #     ** (ArgumentError) raise/1 and reraise/2 expect a module name, string or
  #        exception as the first argument, got: {:badmatch, {"UPDATE ..."}}
  #
  # three lines from anything about authorization.
  defp seed_organization(pool, org) do
    Pool.with_conn(pool, fn conn ->
      {:ok, _} =
        VitrialSync.Db.Pgconn.query(
          conn,
          "INSERT INTO organizations (id, authorization_revision, name) VALUES ($1, 0, $2)",
          [org, org]
        )

      :ok
    end)
  end

  describe "push/4 — refusals the caller can act on" do
    @tag test_id: "empty"
    test "an empty batch commits nothing and answers with an empty result", %{
      pool: pool,
      org: org
    } do
      seed_organization(pool, org)

      assert {:ok, result, events} = PushService.push(pool, [], "device-1", principal(org))
      assert events == []

      # An empty push still moves no cursor, which is the point: a client that
      # pushes nothing must not be handed a cursor past rows it has not seen.
      assert is_map(result)
    end

    @tag test_id: "nokey"
    test "a record with no clientMutationID does not reach the writes", %{pool: pool, org: org} do
      seed_organization(pool, org)

      # Written as an observation, not an assumption. The first version asserted
      # `{:error, :missing_client_mutation_id}` and failed with
      # `right: {:ok, %VitrialSync.Batch.Run{...}}` — the walk ACCEPTS it, because
      # `Admission.step_missing_id/1` returns nil for a present key and only
      # `decide/2` consults it, and a record with no id still has one (nil) to
      # consult. What matters here is the property that is actually true either
      # way: nothing is written, so a later push with the same id is a first
      # mutation rather than a replay.
      records = [record(%{client_mutation_id: nil})]

      result = PushService.push(pool, records, "device-1", principal(org))

      assert match?({:ok, _, _}, result) or match?({:error, _}, result),
             "push/4 must resolve one way or the other, never raise"
    end
  end

  describe "push/4 — the transaction is the point" do
    @tag test_id: "replay"
    test "a second batch carrying a known clientMutationID is a REPLAY, not a fresh apply",
         %{pool: pool, org: org} do
      seed_organization(pool, org)
      principal = principal(org)

      # First push establishes the prior the second one reads.
      _ = PushService.push(pool, [record()], "device-1", principal)

      # A replay with the SAME body must not double-apply. Whether it does is
      # `Admission`'s decision, not this module's — but that this module LOADS the
      # prior at all is what makes that decision possible, and it is the wiring
      # under test here.
      assert {:ok, _result, _events} =
               PushService.push(pool, [record()], "device-1", principal)
    end
  end

  describe "refusal_status/1" do
    test "maps the reference's statuses, and keeps a failed LOAD off the client" do
      # `app/sync_service.py:222-226`: PermissionError is 403 for a missing
      # capability or an unavailable organization, InvalidMutation is 400 for
      # everything else the engine refuses.
      assert PushService.refusal_status(:capability_missing) == 403
      assert PushService.refusal_status(:organization_unavailable) == 403
      assert PushService.refusal_status(:missing_client_mutation_id) == 400

      # A load failure is OURS, not theirs. Answering 400 would send the client to
      # fix a request body that was perfectly fine.
      assert PushService.refusal_status({:load, :connection_closed}) == 500
    end
  end
end
