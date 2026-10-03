defmodule VitrialSync.BatchTest do
  @moduledoc """
  The batch walk, as unit tests.

  Several of these cases are UNIT-ONLY on purpose, and the reason matters more
  than the cases: the differential oracle's slice of `_apply_push_once` cuts
  the loop body at the step-4 `try:` (`parity_oracle.py`, "the steps 1-3
  slice"), so everything on the ACCEPT path -- revision assignment, the
  canonical row, the membership grant, the changelog -- is past the boundary
  the oracle can express. The same-batch duplicate and the intra-batch
  staleness cases here are the only guards on commit visibility inside one
  batch (`tests/test_push_preread_deferred.py:240-279` is the reference's own
  test for the same fact); the parity batch section covers the slice-visible
  half of them.

  The flagship case uses a RESTRICTED principal (no `all_customers`, empty id
  sets) deliberately. Under the `ownership_test` default of organization-wide
  scope, the wrong semantics -- a scope frozen at entry -- still pass, because
  `can_access_customer?` is vacuously true. The restricted principal is the
  pin, not decoration: it is the exact scenario `VitrialSync.EntityOrder`'s
  moduledoc and `ownership.ex`'s "two halves" section describe.
  """

  use ExUnit.Case, async: true

  alias VitrialSync.{Batch, CanonicalJSON, Fingerprint, MutationEvent, Record}

  alias VitrialSync.Canonical.{
    Customer,
    Entity,
    EvidenceBlob,
    Item,
    ItemChild,
    Membership,
    Organization,
    Principal,
    Project,
    ProjectSector,
    World
  }

  @ts "2026-10-02T12:00:00Z"

  # -- fixtures ---------------------------------------------------------------

  defp record(id, entity_type, entity_id, overrides \\ []) do
    attrs =
      Keyword.merge(
        [
          id: id,
          entity_type: entity_type,
          entity_id: entity_id,
          updated_at: @ts,
          payload: ~s({"id": "#{entity_id}"}),
          base_server_revision: nil,
          client_mutation_id: "m-" <> id
        ],
        overrides
      )

    {:ok, record} = Record.new(Map.new(attrs))
    record
  end

  defp principal(capabilities) do
    %Principal{
      user_id: "u1",
      organization_id: "org",
      membership_id: "mem1",
      session_id: "s1",
      authorization_revision: 4,
      all_customers: true,
      all_projects: true,
      capabilities: MapSet.new(["sync" | capabilities])
    }
  end

  # The half of the scope rules `principal/1` cannot test: nothing is
  # pre-authorized, so every access must come from the threaded working scope.
  defp restricted_principal(capabilities) do
    %Principal{
      user_id: "u1",
      organization_id: "org",
      membership_id: "mem1",
      session_id: "s1",
      authorization_revision: 4,
      all_customers: false,
      all_projects: false,
      customer_ids: MapSet.new(),
      project_ids: MapSet.new(),
      capabilities: MapSet.new(["sync" | capabilities])
    }
  end

  defp org_world(overrides \\ []) do
    struct!(
      %World{
        organization: %Organization{authorization_revision: 7},
        membership: %Membership{organization_id: "org", user_id: "u1", active: true}
      },
      overrides
    )
  end

  defp store(world, opts \\ []) do
    %Batch.Store{
      priors: Keyword.get(opts, :priors, %{}),
      fingerprints: Keyword.get(opts, :fingerprints, %{}),
      world: world,
      max_revision: Keyword.get(opts, :max_revision, 0)
    }
  end

  defp prior(entity_type, entity_id, base, status, revision) do
    %{
      entity_type: entity_type,
      entity_id: entity_id,
      base_server_revision: base,
      result_status: status,
      result_server_revision: revision
    }
  end

  defp run!(records, principal, store) do
    assert {:ok, run} = Batch.run(records, "dev1", principal, store)
    run
  end

  # -- the flagship -----------------------------------------------------------

  describe "mid-batch scope widening, the reference's flagship" do
    test "a restricted principal creates a customer and then a project under it" do
      principal = restricted_principal(["customer.create", "project.create"])

      records = [
        record("r1", "customer", "c1", client_mutation_id: "m1"),
        record("r2", "project", "p1",
          client_mutation_id: "m2",
          payload: ~s({"id": "p1", "customerID": "c1"})
        )
      ]

      run = run!(records, principal, store(org_world()))

      # Both accepted, one revision each: the project is authorized against the
      # customer the SAME batch created -- the canonical row threaded by
      # record 1 (active_customer) AND the working scope widened by record 1
      # (customer_in_scope). Freeze either and record 2 is refused.
      assert %{0 => customer, 1 => project} = run.results
      assert %{status: :accepted, reason: "committed", result_revision: 1} = customer
      assert %{status: :accepted, reason: "committed", result_revision: 2} = project

      # The grant delta and the bump, one each per create.
      assert run.effects.membership_grants == [
               %{customer_ids: ["c1"], project_ids: []},
               %{customer_ids: [], project_ids: ["p1"]}
             ]

      assert run.effects.organization_authorization_revision == 9
      assert length(run.effects.changelog_rows) == 2

      # The mutation rows carry the PRINCIPAL's revision from the token; the
      # organization's is the separately threaded integer above. Two axes.
      assert Enum.all?(run.effects.mutation_rows, &(&1.authorization_revision == 4))

      # Events in PROCESSING order: customer first, though the client sent
      # them parent-first anyway here.
      assert Enum.map(run.events, & &1["entityType"]) == ["customer", "project"]
    end

    test "the membership grant is gated on containment, but the scope widening is not" do
      # The seeded-membership edge (`app/ownership.py:1003` vs `:865`): the id
      # is already in the membership, so the reference grants no bump -- yet it
      # STILL widens the scope object, unconditionally on the create arm. That
      # split is why the widening derives from the plan's create arms and not
      # from the gated grant delta.
      principal = restricted_principal(["customer.create", "project.create"])

      world =
        org_world(
          membership: %Membership{
            organization_id: "org",
            user_id: "u1",
            active: true,
            customer_ids: ["c1"]
          }
        )

      records = [
        record("r1", "customer", "c1", client_mutation_id: "m1"),
        record("r2", "project", "p1",
          client_mutation_id: "m2",
          payload: ~s({"id": "p1", "customerID": "c1"})
        )
      ]

      run = run!(records, principal, store(world))

      assert %{0 => customer, 1 => project} = run.results
      assert customer.status == :accepted
      # The customer grant delta is EMPTY (the membership already holds c1),
      # so c1 produces no bump. The project is a genuinely new id and still
      # grants.
      assert run.effects.membership_grants == [%{customer_ids: [], project_ids: ["p1"]}]
      assert run.effects.organization_authorization_revision == 8
      # And the project is nonetheless authorized -- the entry scope object
      # was widened unconditionally on the customer create arm
      # (`app/ownership.py:865`), which is the half the gated grant delta
      # cannot express. Under a restricted principal, this line IS the pin.
      assert project.status == :accepted
    end

    test "child-first arrival processes the parent first and reports ids in original order" do
      principal = principal(["customer.create", "project.create"])

      records = [
        record("r1", "project", "p1", payload: ~s({"id": "p1", "customerID": "c1"})),
        record("r2", "customer", "c1")
      ]

      run = run!(records, principal, store(org_world()))

      # Processing order: the customer (order 0) ran first, so it took
      # revision 1 and the project took 2 -- even though the project was the
      # client's first record.
      assert %{0 => project, 1 => customer} = run.results
      assert project.result_revision == 2
      assert customer.result_revision == 1
      assert Enum.map(run.events, & &1["entityType"]) == ["customer", "project"]

      # The response is in ORIGINAL order regardless.
      run = Batch.finish(run, nil)
      assert run.result.accepted_record_ids == ["r1", "r2"]
      assert run.result.rejected_record_ids == []
    end
  end

  # -- the guards -------------------------------------------------------------

  describe "whole-request guards" do
    test "no sync capability refuses the whole request" do
      # Built by hand: every fixture principal carries "sync".
      no_sync = %Principal{organization_id: "org", capabilities: MapSet.new(["customer.create"])}

      assert Batch.run([], "dev1", no_sync, store(org_world())) ==
               {:error, :capability_missing}
    end

    test "no organization row refuses the whole request" do
      assert Batch.run([], "dev1", principal(["sync"]), store(%World{})) ==
               {:error, :organization_unavailable}
    end

    test "an empty batch settles with nothing to do" do
      run = run!([], principal(["sync"]), store(org_world()))

      assert run.results == %{}
      assert run.events == []
      assert run.effects.mutation_rows == []
      assert run.effects.fingerprint_rows == []
      assert run.effects.entity_upserts == []
      assert run.effects.changelog_rows == []
      assert run.effects.membership_grants == []
      assert run.effects.evidence_gc_rows == []
      assert run.effects.organization_authorization_revision == 7
    end
  end

  # -- step 1 -----------------------------------------------------------------

  describe "step 1: a record with no idempotency key" do
    test "settles rejected and writes no rows" do
      records = [record("r1", "customer", "c1", client_mutation_id: nil)]

      run = run!(records, principal(["customer.create"]), store(org_world()))

      assert %{0 => outcome} = run.results
      assert outcome.status == :rejected
      assert outcome.reason == "missing_client_mutation_id"
      assert outcome.result_revision == nil
      assert run.effects.mutation_rows == []
      assert run.effects.fingerprint_rows == []
      # The id-less record still produced its event.
      assert length(run.events) == 1
    end
  end

  # -- step 2 -----------------------------------------------------------------

  describe "step 2: the replay trio, seeded" do
    test "replay, spent id and collision, each against its own prior" do
      replay_record =
        record("r1", "customer", "c1", client_mutation_id: "m-replay", base_server_revision: 0)

      spent_record =
        record("r2", "customer", "c2", client_mutation_id: "m-spent", base_server_revision: 0)

      collide_record =
        record("r3", "customer", "c3", client_mutation_id: "m-collide", base_server_revision: 0)

      priors = %{
        "m-replay" => prior("customer", "c1", 0, "accepted", 7),
        "m-spent" => prior("customer", "c2", 0, "rejected", 3),
        "m-collide" => prior("customer", "c3", 0, "accepted", 4)
      }

      fingerprints = %{
        "m-replay" => Fingerprint.of(Record.mutation(replay_record)),
        "m-spent" => Fingerprint.of(Record.mutation(spent_record)),
        "m-collide" => String.duplicate("f", 64)
      }

      run =
        run!(
          [replay_record, spent_record, collide_record],
          principal(["customer.create"]),
          store(org_world(), priors: priors, fingerprints: fingerprints)
        )

      assert %{0 => replay, 1 => spent, 2 => collision} = run.results

      assert %{status: :accepted, reason: "idempotent_replay", result_revision: 7} = replay

      assert %{status: :rejected, reason: "rejected_mutation_id_reuse", result_revision: 3} =
               spent

      assert %{status: :rejected, reason: "mutation_id_collision", result_revision: 4} = collision

      # Step 2 writes nothing: the existing rows ARE the record.
      assert run.effects.mutation_rows == []
      assert run.effects.fingerprint_rows == []
    end

    test "a same-batch duplicate sees the row the first record wrote" do
      # tests/test_push_preread_deferred.py:240-279: the second record must
      # observe the mutation row the first wrote -- and must not overwrite its
      # fingerprint. One fingerprint wins, not one per record.
      first = record("r1", "customer", "c1", client_mutation_id: "dup")

      second =
        record("r2", "customer", "c1",
          client_mutation_id: "dup",
          payload: ~s({"id": "c1", "extra": true})
        )

      run = run!([first, second], principal(["customer.create"]), store(org_world()))

      assert %{0 => accepted, 1 => collision} = run.results
      assert accepted.status == :accepted
      assert collision.status == :rejected
      assert collision.reason == "mutation_id_collision"
      # The collision reports the revision the id is already bound to.
      assert collision.result_revision == 1

      assert length(run.effects.mutation_rows) == 1
      assert length(run.effects.fingerprint_rows) == 1

      run = Batch.finish(run, nil)
      assert run.result.accepted_record_ids == ["r1"]
      assert run.result.rejected_record_ids == ["r2"]
    end
  end

  # -- commit visibility --------------------------------------------------------

  describe "intra-batch staleness is judged against the new revision" do
    test "an update after a same-batch create matches the create's revision" do
      records = [
        record("r1", "customer", "c1", client_mutation_id: "m1"),
        record("r2", "customer", "c1", client_mutation_id: "m2", base_server_revision: 1)
      ]

      run = run!(records, principal(["customer.create", "customer.edit"]), store(org_world()))

      assert %{0 => create, 1 => update} = run.results
      assert create.result_revision == 1
      assert update.status == :accepted
      assert update.result_revision == 2
    end

    test "a stale base against the create's new revision reports that revision" do
      records = [
        record("r1", "customer", "c1", client_mutation_id: "m1"),
        record("r2", "customer", "c1", client_mutation_id: "m2", base_server_revision: 0)
      ]

      run = run!(records, principal(["customer.create", "customer.edit"]), store(org_world()))

      assert %{1 => stale} = run.results
      assert stale.reason == "stale_revision"
      # The pre-batch world has no entity: only the threaded create makes the
      # guard see revision 1 here. result_revision nil would mean the walk
      # never let the second record see the first's commit.
      assert stale.result_revision == 1
    end

    test "rejections do not consume revisions" do
      world =
        org_world(
          entities: [%Entity{entity_type: "customer", entity_id: "c1", server_revision: 3}]
        )

      records = [
        record("r1", "customer", "c1", base_server_revision: 2),
        record("r2", "customer", "c2")
      ]

      run = run!(records, principal(["customer.create"]), store(world))

      assert %{0 => stale, 1 => create} = run.results
      assert stale.reason == "stale_revision"
      assert create.status == :accepted
      # The stale rejection wrote rows but took no revision: the create after
      # it is revision 1, not 2.
      assert create.result_revision == 1
    end
  end

  # -- steps 5-8 ----------------------------------------------------------------

  describe "steps 5-8: the authority arms" do
    test "an authority rejection writes the rejected row at the current revision" do
      world =
        org_world(
          entities: [%Entity{entity_type: "item", entity_id: "i1", server_revision: 3}],
          item_children: [
            %ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"}
          ]
        )

      records = [
        record("r1", "item", "i1",
          base_server_revision: 3,
          deleted_at: @ts,
          payload: ~s({"id": "i1"})
        )
      ]

      run = run!(records, principal([]), store(world))

      assert %{0 => outcome} = run.results
      assert outcome.status == :rejected
      assert outcome.reason == "authorization_rejected"
      assert outcome.result_revision == 3

      assert [
               %{
                 result_status: "rejected",
                 result_server_revision: 3,
                 client_mutation_id: "m-r1",
                 entity_type: "item"
               }
             ] = run.effects.mutation_rows

      assert run.effects.changelog_rows == []
    end

    test "a tombstone stores no payload and logs a delete" do
      world =
        org_world(
          customers: [%Customer{customer_id: "c1"}],
          entities: [%Entity{entity_type: "customer", entity_id: "c1", server_revision: 1}]
        )

      records = [
        record("r1", "customer", "c1", base_server_revision: 1, deleted_at: @ts)
      ]

      # The store's max_revision is the caller's pre-commit read of
      # max(server_revision) -- here 1, the seeded entity.
      run = run!(records, principal(["customer.edit"]), store(world, max_revision: 1))

      assert %{0 => outcome} = run.results
      assert outcome.status == :accepted
      assert outcome.result_revision == 2

      assert [%{payload: nil, deleted_at: @ts, server_revision: 2}] = run.effects.entity_upserts
      assert [%{operation: "delete", server_revision: 2}] = run.effects.changelog_rows
    end

    test "evidence GC is enqueued only when a blob row exists" do
      item_children = [%ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"}]
      entity = %Entity{entity_type: "evidence", entity_id: "e1", server_revision: 2}
      item = %Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}
      sector = %ProjectSector{project_sector_id: "ps1", project_id: "p1", sector_id: "s1"}

      payload = ~s({"id": "e1", "itemID": "i1"})

      record =
        record("r1", "evidence", "e1", base_server_revision: 2, deleted_at: @ts, payload: payload)

      # The item-child authority walks the whole chain: the evidence's item,
      # the item's sector, and the sector's project must all be canonical.
      project = %Project{project_id: "p1", customer_id: "c1"}

      with_blob =
        org_world(
          projects: [project],
          project_sectors: [sector],
          items: [item],
          item_children: item_children,
          entities: [entity],
          evidence_blobs: [
            %EvidenceBlob{document_id: "e1", storage_provider: "s3", object_key: "org/e1"}
          ]
        )

      run = run!([record], principal(["item.evidence.manage"]), store(with_blob))
      assert %{0 => outcome} = run.results
      assert outcome.status == :accepted

      assert [%{document_id: "e1", reason: "metadata_tombstone", grace_seconds: 86_400}] =
               run.effects.evidence_gc_rows

      without_blob =
        org_world(
          projects: [project],
          project_sectors: [sector],
          items: [item],
          item_children: item_children,
          entities: [entity]
        )

      run = run!([record], principal(["item.evidence.manage"]), store(without_blob))

      assert run.results[0].status == :accepted
      assert run.effects.evidence_gc_rows == []
    end
  end

  # -- delivery ---------------------------------------------------------------

  describe "a delivery batch" do
    setup do
      # The same passing fixture `delivery_test` uses for an authorized
      # creation: canonical chain, the approved quotation entity, the payload
      # and its opening event.
      world =
        org_world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1"}],
          items: [%Item{item_id: "i1", project_id: "p1"}],
          entities: [
            %Entity{entity_type: "quotation", entity_id: "q1", payload: quotation_payload()}
          ]
        )

      payload =
        Map.merge(
          %{
            "id" => "d1",
            "status" => "engineeringReview",
            "projectID" => "p1",
            "customerID" => "c1",
            "quotationID" => "q1",
            "quotationNumber" => "Q-1",
            "quotationRevision" => 1,
            "currencyCode" => "EUR",
            "pricingVersion" => 3,
            "quotedTotal" => "100.00",
            "startedAt" => "2026-01-01T00:00:00Z",
            "updatedAt" => "2026-01-01T00:00:00Z",
            "events" => [
              %{
                "id" => "e1",
                "toStatus" => "engineeringReview",
                "occurredAt" => "2026-01-01T00:00:00Z",
                "actorID" => "u1",
                "organizationID" => "org",
                "membershipID" => "mem1",
                "sessionID" => "s1",
                "authorizationRevision" => 4
              }
            ]
          },
          %{}
        )

      record =
        record("r1", "delivery_execution", "d1",
          client_mutation_id: "m1",
          payload: CanonicalJSON.encode(payload)
        )

      %{world: world, record: record}
    end

    test "widens no scope and grants no bump", %{world: world, record: record} do
      run = run!([record], principal(["delivery.manage"]), store(world))

      assert %{0 => outcome} = run.results
      assert outcome.status == :accepted
      assert outcome.result_revision == 1

      # Delivery writes a project-child row and nothing else: no grant, no
      # bump (`app/delivery_execution.py:849` -- apply_delivery_execution_
      # ownership never touches the scope object).
      assert run.effects.membership_grants == []
      assert run.effects.organization_authorization_revision == 7
      assert length(run.effects.changelog_rows) == 1
    end

    test "is refused outside the working scope", %{world: world, record: record} do
      run = run!([record], restricted_principal(["delivery.manage"]), store(world))

      assert run.results[0].status == :rejected
      assert run.results[0].reason == "delivery_rejected"
    end
  end

  # -- the wire response --------------------------------------------------------

  describe "finish/2 and the wire response" do
    test "next_cursor comes from the caller's post-commit read, nil as seq:0" do
      principal = restricted_principal(["customer.create", "project.create"])

      records = [
        record("r1", "customer", "c1", client_mutation_id: "m1"),
        record("r2", "project", "p1",
          client_mutation_id: "m2",
          payload: ~s({"id": "p1", "customerID": "c1"})
        )
      ]

      run = run!(records, principal, store(org_world()))
      assert run.result == nil

      assert %{result: %{next_cursor: "seq:0"}} = Batch.finish(run, nil)
      assert %{result: %{next_cursor: "seq:41"}} = Batch.finish(run, 41)
    end

    test "Result.wire renders the three SyncResult keys" do
      world =
        org_world(
          entities: [%Entity{entity_type: "customer", entity_id: "c1", server_revision: 3}]
        )

      records = [
        record("r-stale", "customer", "c1", base_server_revision: 2),
        record("r-new", "customer", "c2")
      ]

      run = run!(records, principal(["customer.create"]), store(world)) |> Batch.finish(0)

      assert Batch.Result.wire(run.result) == %{
               "acceptedRecordIDs" => ["r-new"],
               "rejectedRecordIDs" => ["r-stale"],
               "nextCursor" => "seq:0"
             }
    end
  end

  # -- events -------------------------------------------------------------------

  describe "outcome events" do
    test "carry exactly the eight wire fields, in processing order" do
      principal = principal(["customer.create", "project.create"])

      records = [
        record("r1", "project", "p1", payload: ~s({"id": "p1", "customerID": "c1"})),
        record("r2", "customer", "c1")
      ]

      run = run!(records, principal, store(org_world()))

      assert length(run.events) == 2
      # Processing order, not original order: the customer ran first.
      assert Enum.map(run.events, & &1["entityType"]) == ["customer", "project"]

      for event <- run.events do
        assert MapSet.new(Map.keys(event)) ==
                 MapSet.new([
                   "mutationRef",
                   "deviceRef",
                   "entityRef",
                   "entityType",
                   "resultStatus",
                   "reason",
                   "baseServerRevision",
                   "resultServerRevision"
                 ])
      end

      [customer_event, project_event] = run.events
      assert customer_event["resultServerRevision"] == 1
      assert project_event["resultServerRevision"] == 2
      assert customer_event["deviceRef"] == MutationEvent.correlation_ref("dev1")
      assert customer_event["mutationRef"] == MutationEvent.correlation_ref("m-r2")
    end
  end

  # -- delivery fixture -----------------------------------------------------------

  defp quotation_payload do
    %{
      "id" => "q1",
      "status" => "approved",
      "projectID" => "p1",
      "customerID" => "c1",
      "number" => "Q-1",
      "currencyCode" => "EUR",
      "pricingVersion" => 3,
      "events" => [%{"kind" => "approved"}],
      "lines" => [%{"itemID" => "i1", "totalPrice" => "100.00"}]
    }
  end
end
