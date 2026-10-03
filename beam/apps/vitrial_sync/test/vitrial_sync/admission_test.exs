defmodule VitrialSync.AdmissionTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Admission, Record}

  @ts "2026-10-02T12:00:00Z"
  @good ~s({"a": 1})

  setup do
    %{
      record: fn overrides ->
        attrs =
          Map.merge(
            %{
              id: "rid",
              entity_type: "customer",
              entity_id: "c1",
              updated_at: @ts,
              payload: @good
            },
            Map.new(overrides)
          )

        {:ok, record} = Record.new(attrs)
        record
      end,
      context: fn prior, fingerprint, entity_exists, current ->
        %{
          prior: prior,
          prior_fingerprint: fingerprint,
          entity_exists: entity_exists,
          current_server_revision: current
        }
      end,
      prior: fn entity_type, entity_id, base, status, revision ->
        %{
          entity_type: entity_type,
          entity_id: entity_id,
          base_server_revision: base,
          result_status: status,
          result_server_revision: revision
        }
      end
    }
  end

  describe "step 1: the idempotency key" do
    test "a nil key is rejected and nothing is recorded", %{record: record, context: context} do
      outcome = Admission.decide(record.(client_mutation_id: nil), context.(nil, nil, false, nil))

      assert outcome.status == :rejected
      assert outcome.reason == "missing_client_mutation_id"
      assert outcome.result_revision == nil
    end

    test "and no SyncMutation row is written for it", %{record: record, context: context} do
      # The reference `continue`s at sync_service.py:243-249 BEFORE reaching
      # `_record_mutation`, so a record with no key produces no mutation row.
      # Reporting `writes_mutation_row?: true` would hand the caller a row
      # carrying nil/"" into `SyncMutation.client_mutation_id`, which is NOT NULL
      # under a unique constraint (app/models.py:197,209) — the row is not merely
      # unwanted, it is uninsertable.
      #
      # The parity corpus CANNOT catch this: `step1_client_mutation_id_absent`
      # yields a valid oracle outcome, so an outcome-shape comparison passes while
      # the emitted effects are wrong. Hence a hand-written regression here.
      for key <- [nil, ""] do
        outcome =
          Admission.decide(record.(client_mutation_id: key), context.(nil, nil, false, nil))

        assert outcome.reason == "missing_client_mutation_id"

        assert outcome.writes_mutation_row? == false,
               "client_mutation_id=#{inspect(key)} must not produce a mutation row"
      end
    end

    test "an empty-string key is rejected the same way, because Pydantic allows it through",
         %{record: record, context: context} do
      # clientMutationID has max_length but NO min_length in app/schemas.py, so
      # "" is a schema-valid record. Rejecting it at the type boundary would move
      # the rejection out of the step that owns it.
      outcome = Admission.decide(record.(client_mutation_id: ""), context.(nil, nil, false, nil))

      assert outcome.reason == "missing_client_mutation_id"
    end

    test "the missing-key check runs before everything else", %{record: record, context: context} do
      # Even with a prior row that would otherwise be a replay, a record with no
      # key never reaches the idempotency branch.
      prior = %{
        entity_type: "customer",
        entity_id: "c1",
        base_server_revision: 0,
        result_status: "accepted",
        result_server_revision: 1
      }

      outcome =
        Admission.decide(record.(client_mutation_id: ""), context.(prior, "deadbeef", true, 1))

      assert outcome.reason == "missing_client_mutation_id"
    end
  end

  describe "step 2: a prior mutation with this clientMutationID" do
    setup %{record: record} do
      accepted = %{
        entity_type: "customer",
        entity_id: "c1",
        base_server_revision: 0,
        result_status: "accepted",
        result_server_revision: 4
      }

      rejected = %{accepted | result_status: "rejected", result_server_revision: nil}

      %{
        accepted: accepted,
        rejected: rejected,
        fingerprint: fn attrs -> VitrialSync.Fingerprint.of(Record.mutation(record.(attrs))) end
      }
    end

    test "the same bytes, previously accepted, is an idempotent replay",
         %{record: record, context: context, accepted: accepted, fingerprint: fingerprint} do
      attrs = [client_mutation_id: "r1", base_server_revision: 0]

      outcome =
        Admission.decide(
          record.(attrs),
          context.(accepted, fingerprint.(attrs), true, 4)
        )

      assert outcome.status == :accepted
      assert outcome.reason == "idempotent_replay"
      assert outcome.result_revision == 4
      refute outcome.writes_mutation_row?
    end

    test "the same bytes, previously rejected, reports the spent id",
         %{record: record, context: context, rejected: rejected, fingerprint: fingerprint} do
      attrs = [client_mutation_id: "r2", base_server_revision: 0]

      outcome =
        Admission.decide(
          record.(attrs),
          context.(rejected, fingerprint.(attrs), true, 4)
        )

      # Not "idempotent_replay": nothing replayed, and saying otherwise is the
      # false statement the three-way split exists to prevent.
      assert outcome.status == :rejected
      assert outcome.reason == "rejected_mutation_id_reuse"
      refute outcome.writes_mutation_row?
    end

    test "S-55a: a rejected mutation stays rejected even with corrected inputs",
         %{record: record, context: context, rejected: rejected, fingerprint: fingerprint} do
      # Stored under base 0 and rejected. The client fixes its base to the
      # current revision 3 and retries -- under the SAME id. Still rejected.
      identical = [client_mutation_id: "r3", base_server_revision: 3]

      outcome =
        Admission.decide(
          record.(identical),
          context.(rejected, fingerprint.(identical), true, 3)
        )

      assert outcome.status == :rejected
      assert outcome.reason == "rejected_mutation_id_reuse"
    end

    test "different payload bytes under the same id is a collision",
         %{record: record, context: context, accepted: accepted, fingerprint: fingerprint} do
      stored = [client_mutation_id: "r4", base_server_revision: 0]
      resent = [client_mutation_id: "r4", base_server_revision: 0, payload: ~s({"a": 2})]

      outcome =
        Admission.decide(record.(resent), context.(accepted, fingerprint.(stored), true, 4))

      assert outcome.status == :rejected
      assert outcome.reason == "mutation_id_collision"
      # The collision still reports the prior row's revision, so a client can see
      # what the id is already bound to.
      assert outcome.result_revision == 4
    end

    test "the fingerprint binds updatedAt", %{
      record: record,
      context: context,
      accepted: accepted,
      fingerprint: fingerprint
    } do
      stored = [client_mutation_id: "r5", base_server_revision: 0]

      changed = [
        client_mutation_id: "r5",
        base_server_revision: 0,
        updated_at: "2026-10-02T12:00:01Z"
      ]

      outcome =
        Admission.decide(record.(changed), context.(accepted, fingerprint.(stored), true, 4))

      assert outcome.reason == "mutation_id_collision"
    end

    test "the fingerprint binds deletedAt", %{
      record: record,
      context: context,
      accepted: accepted,
      fingerprint: fingerprint
    } do
      stored = [client_mutation_id: "r6", base_server_revision: 0]

      changed = [
        client_mutation_id: "r6",
        base_server_revision: 0,
        deleted_at: "2026-10-02T00:00:00Z"
      ]

      outcome =
        Admission.decide(record.(changed), context.(accepted, fingerprint.(stored), true, 4))

      assert outcome.reason == "mutation_id_collision"
    end

    test "the fingerprint binds baseServerRevision, so a rebase is not a replay",
         %{record: record, context: context, accepted: accepted, fingerprint: fingerprint} do
      stored = [client_mutation_id: "r7", base_server_revision: 0]
      rebased = [client_mutation_id: "r7", base_server_revision: 8]

      outcome =
        Admission.decide(record.(rebased), context.(accepted, fingerprint.(stored), true, 8))

      assert outcome.reason == "mutation_id_collision"
    end

    test "with no fingerprint row the weaker identity ignores payload, updatedAt and deletedAt",
         %{record: record, context: context, accepted: accepted} do
      # A row from before fingerprinting existed. Comparing only
      # (entityType, entityID, baseServerRevision) is what the reference does,
      # and tightening it would reject legitimate replays of every mutation ever
      # recorded.
      for overrides <- [
            [payload: ~s({"totally": "different"})],
            [updated_at: "1999-01-01T00:00:00Z"],
            [deleted_at: "2026-10-02T00:00:00Z"]
          ] do
        attrs = Keyword.merge([client_mutation_id: "r8", base_server_revision: 0], overrides)
        outcome = Admission.decide(record.(attrs), context.(accepted, nil, true, 4))

        assert outcome.status == :accepted
        assert outcome.reason == "idempotent_replay"
      end
    end

    test "the weak identity still rejects a different entity or revision",
         %{record: record, context: context, accepted: accepted} do
      different_entity =
        Admission.decide(
          record.(client_mutation_id: "r9", entity_id: "c2", base_server_revision: 0),
          context.(accepted, nil, true, 4)
        )

      different_revision =
        Admission.decide(
          record.(client_mutation_id: "r10", base_server_revision: 9),
          context.(accepted, nil, true, 9)
        )

      assert different_entity.reason == "mutation_id_collision"
      assert different_revision.reason == "mutation_id_collision"
    end
  end

  describe "step 3: optimistic concurrency" do
    test "a create must omit baseServerRevision", %{record: record, context: context} do
      assert Admission.decide(record.(client_mutation_id: "m1"), context.(nil, nil, false, nil)).status ==
               :accepted

      assert Admission.decide(
               record.(client_mutation_id: "m2", base_server_revision: 0),
               context.(nil, nil, false, nil)
             ).reason == "stale_revision"
    end

    test "an update must match the stored revision exactly",
         %{record: record, context: context} do
      assert Admission.decide(
               record.(client_mutation_id: "m3", base_server_revision: 3),
               context.(nil, nil, true, 3)
             ).status == :accepted

      stale =
        Admission.decide(
          record.(client_mutation_id: "m4", base_server_revision: 2),
          context.(nil, nil, true, 3)
        )

      assert stale.reason == "stale_revision"
      assert stale.result_revision == 3
    end

    test "a null base against an existing row is stale", %{record: record, context: context} do
      outcome = Admission.decide(record.(client_mutation_id: "m5"), context.(nil, nil, true, 3))

      assert outcome.reason == "stale_revision"
      assert outcome.result_revision == 3
    end

    test "zero matches zero, and is not treated as absent", %{record: record, context: context} do
      outcome =
        Admission.decide(
          record.(client_mutation_id: "m6", base_server_revision: 0),
          context.(nil, nil, true, 0)
        )

      assert outcome.status == :accepted
    end

    test "a rejection here SPENDS the clientMutationID", %{record: record, context: context} do
      # This is what makes S-55a true. The row is written, so the id is gone and
      # the client has to mint a new one for the retry.
      outcome =
        Admission.decide(
          record.(client_mutation_id: "m7", base_server_revision: 2),
          context.(nil, nil, true, 3)
        )

      assert outcome.writes_mutation_row?
    end

    test "a tombstone goes through the same guard", %{record: record, context: context} do
      outcome =
        Admission.decide(
          record.(
            entity_type: "item",
            entity_id: "i1",
            payload: "{}",
            client_mutation_id: "m8",
            base_server_revision: 1,
            deleted_at: "2026-10-02T00:00:00Z"
          ),
          context.(nil, nil, true, 7)
        )

      assert outcome.reason == "stale_revision"
      assert outcome.result_revision == 7
    end
  end

  describe "step 4: payload decode" do
    test "a decodable object is admitted for commit", %{record: record, context: context} do
      outcome =
        Admission.decide(record.(client_mutation_id: "p1"), context.(nil, nil, false, nil))

      assert outcome.status == :accepted
      assert outcome.reason == "committed"
      assert outcome.payload == %{"a" => 1}
      assert outcome.writes_mutation_row?
    end

    test "base64 and raw encodings of the same object are both admitted",
         %{record: record, context: context} do
      raw = Admission.decide(record.(client_mutation_id: "p2"), context.(nil, nil, false, nil))

      encoded =
        Admission.decide(
          record.(client_mutation_id: "p3", payload: Base.encode64(@good)),
          context.(nil, nil, false, nil)
        )

      assert raw.payload == encoded.payload
    end

    test "an undecodable payload is InvalidMutation, with the class name verbatim",
         %{record: record, context: context} do
      for payload <- [Base.encode64("[1,2]"), <<0xFF, 0xFE, " junk">>, ""] do
        outcome =
          Admission.decide(
            record.(client_mutation_id: "p4", payload: payload),
            context.(nil, nil, false, nil)
          )

        assert outcome.status == :rejected
        # S-71: the reason is a token from app/rejection_codes.py, not the Python
        # class name. `InvalidMutation` maps to `invalid_mutation`, and dual-run
        # compares these tokens literally.
        assert outcome.reason == "invalid_mutation"
        assert outcome.writes_mutation_row?
      end
    end
  end

  describe "replay_reason/2" do
    test "the three-way split, stated directly" do
      assert Admission.replay_reason(false, false) == "mutation_id_collision"
      assert Admission.replay_reason(false, true) == "mutation_id_collision"
      assert Admission.replay_reason(true, true) == "idempotent_replay"
      assert Admission.replay_reason(true, false) == "rejected_mutation_id_reuse"
    end
  end

  describe "stale_revision?/2" do
    test "is the same predicate the decision uses", %{record: record, context: context} do
      for {base, exists, current, expected} <- [
            {nil, false, nil, false},
            {0, false, nil, true},
            {5, false, nil, true},
            {3, true, 3, false},
            {2, true, 3, true},
            {nil, true, 3, true},
            {0, true, 0, false}
          ] do
        decision = record.(client_mutation_id: "s1", base_server_revision: base)
        context = context.(nil, nil, exists, current)

        assert Admission.stale_revision?(decision, context) == expected,
               "base=#{inspect(base)} exists=#{exists} current=#{inspect(current)}"
      end
    end
  end

  describe "decide/5 must not reinterpret a step-2 outcome" do
    test "an accepted replay stays a replay and is not re-decided by Authority",
         %{record: record, context: context, prior: prior} do
      # The reference `continue`s for all THREE step-2 reasons
      # (sync_service.py:292) before it reaches the authority steps. decide/5 used
      # to match only `:rejected` and hand every accepted outcome to Authority
      # with `payload: nil`, so an accepted replay came back
      # `{:rejected, "authorization_rejected"}` — a false statement to the client,
      # because the mutation WAS applied and this response is the original answer
      # rather than a fresh rejection.
      record = record.(client_mutation_id: "replay-1", base_server_revision: 3)
      ctx = context.(prior.("customer", "c1", 3, "accepted", 7), nil, false, nil)

      outcome = Admission.decide(record, ctx)

      assert outcome.status == :accepted
      assert outcome.reason == "idempotent_replay"
      assert outcome.result_revision == 7
      assert outcome.payload == nil

      assert %Admission.Outcome{status: :accepted, reason: "idempotent_replay"} =
               Admission.decide(record, ctx, nil, principal(), world())
    end

    test "the spent-id and collision arms stay rejected through decide/5 too",
         %{record: record, context: context, prior: prior} do
      # Those two reached the `:rejected` clause by luck. Pinning all three means
      # a future change to the clause shape cannot silently drop one.
      #
      # The two arms differ by WHICH FACT disagrees, not by the prior's status: a
      # collision is a prior row for a DIFFERENT request, while a spent id is the
      # same request whose row says it was rejected. Varying `result_status` alone
      # would exercise one arm twice — and did, which is why this caught itself.
      spent = record.(client_mutation_id: "spent-1", base_server_revision: 3)
      collision = record.(client_mutation_id: "spent-1", base_server_revision: 3)

      assert %Admission.Outcome{status: :rejected, reason: "rejected_mutation_id_reuse"} =
               Admission.decide(
                 spent,
                 context.(prior.("customer", "c1", 3, "rejected", 7), nil, false, nil),
                 nil,
                 principal(),
                 world()
               )

      assert %Admission.Outcome{status: :rejected, reason: "mutation_id_collision"} =
               Admission.decide(
                 collision,
                 context.(
                   prior.("customer", "a-different-entity", 3, "accepted", 7),
                   nil,
                   false,
                   nil
                 ),
                 nil,
                 principal(),
                 world()
               )
    end

    test "a step-4 accept still reaches Authority, because it carries a payload",
         %{record: record, context: context} do
      # The short-circuit keys on `payload: nil`, not on `status` — otherwise this
      # fix would break every genuine acceptance by refusing to run the authority
      # steps on it. An empty capability set must still be refused by Authority.
      outcome =
        Admission.decide(
          record.(client_mutation_id: "fresh-1"),
          context.(nil, nil, false, nil),
          nil,
          principal(),
          world()
        )

      assert outcome == {:rejected, "authorization_rejected"}
    end
  end

  defp principal do
    %VitrialSync.Canonical.Principal{organization_id: "org", capabilities: MapSet.new()}
  end

  defp world, do: %VitrialSync.Canonical.World{}
end
