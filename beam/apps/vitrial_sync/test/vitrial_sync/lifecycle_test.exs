defmodule VitrialSync.LifecycleTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Canonical, Lifecycle, Record}
  alias VitrialSync.Canonical.{Entity, Principal}

  @now "2026-10-02T12:00:00Z"

  defp principal do
    %Principal{
      user_id: "u1",
      organization_id: "org",
      membership_id: "m1",
      session_id: "s1",
      authorization_revision: 4
    }
  end

  defp provenance(overrides \\ %{}) do
    Map.merge(
      %{
        "actorID" => "u1",
        "organizationID" => "org",
        "membershipID" => "m1",
        "sessionID" => "s1",
        "authorizationRevision" => 4
      },
      overrides
    )
  end

  defp blocker_record(attrs \\ []) do
    struct!(%Record{id: "r1", entity_type: "blocker", entity_id: "b1", updated_at: @now}, attrs)
  end

  defp quotation_record(attrs \\ []) do
    struct!(%Record{id: "r1", entity_type: "quotation", entity_id: "q1", updated_at: @now}, attrs)
  end

  defp blocker_row(payload) do
    %Entity{entity_type: "blocker", entity_id: "b1", payload: payload, deleted_at: nil}
  end

  defp quotation_row(payload) do
    %Entity{entity_type: "quotation", entity_id: "q1", payload: payload, deleted_at: nil}
  end

  defp decide(record, payload, current_row \\ nil, principal_arg \\ nil, world \\ nil) do
    Lifecycle.decide(
      record,
      payload,
      principal_arg,
      current_row,
      world || Canonical.empty_world()
    )
  end

  # --- blocker fixtures -------------------------------------------------------

  # The description is present because the payload rule requires one
  # (`_require_blocker_description`), and `blocker_payload/2` derives the payload
  # description from the last event so that the snapshot rule is not what fails.
  defp blocker_created(overrides \\ %{}) do
    %{
      "id" => "e1",
      "kind" => "created",
      "blockerID" => "b1",
      "itemID" => "i1",
      "description" => "waiting on steel"
    }
    |> Map.merge(provenance())
    |> Map.merge(overrides)
  end

  defp blocker_updated(overrides \\ %{}) do
    %{
      "id" => "e2",
      "kind" => "updated",
      "blockerID" => "b1",
      "itemID" => "i1",
      "description" => "changed"
    }
    |> Map.merge(provenance())
    |> Map.merge(overrides)
  end

  # The description defaults to whatever the LAST event carries, because
  # `_validate_optional_blocker_snapshot` compares exactly those two. A fixture
  # that ignored it would test the snapshot rule instead of the rule it names.
  defp blocker_payload(events, overrides \\ %{}) do
    defaults = %{
      "status" => "open",
      "itemID" => "i1",
      "events" => events,
      "description" => snapshot_text(List.last(events))
    }

    Map.merge(defaults, overrides)
  end

  defp snapshot_text(nil), do: ""

  defp snapshot_text(event) when is_map(event) do
    case Map.get(event, "description") do
      value when is_binary(value) -> value
      _other -> ""
    end
  end

  # A history element that is not an object at all snapshots to nothing; the
  # malformed-history rule is what speaks, and it runs before this.
  defp snapshot_text(_other), do: ""

  # --- quotation fixtures -----------------------------------------------------

  defp quote_event(id, kind, overrides \\ %{}) do
    %{"id" => id, "kind" => kind}
    |> Map.merge(provenance())
    |> Map.merge(overrides)
  end

  defp quote_payload(status, events, overrides \\ %{}) do
    Map.merge(%{"status" => status, "events" => events}, overrides)
  end

  describe "S-59: which records reach this module at all" do
    test "a tombstone is a no-op, whatever its payload holds" do
      # `if deleted_at is not None: return` -- app/lifecycle.py:335-337. The
      # docstring gives the reason: a V1 tombstone may carry an empty payload, so
      # lifecycle validation is for upserts. This is why admission step 5, the
      # item-delete guard, is the one that has to notice tombstones.
      assert decide(blocker_record(deleted_at: "t"), %{"status" => "nonsense"}) == :ok
      assert decide(quotation_record(deleted_at: "t"), %{"status" => "nonsense"}) == :ok
    end

    test "an entity type with no lifecycle rules always passes" do
      for type <- [
            "customer",
            "project",
            "project_sector",
            "item",
            "measurement",
            "evidence",
            "customer_requirement",
            "configuration",
            "configuration_version",
            "item_audit_event",
            "delivery_execution"
          ] do
        record = %Record{id: "r1", entity_type: type, entity_id: "x1", updated_at: @now}

        assert decide(record, %{}) == :ok, type
      end
    end
  end

  describe "blocker: the field rules, app/lifecycle.py:129-147" do
    test "a well-formed open blocker passes" do
      assert decide(blocker_record(), blocker_payload([blocker_created()])) == :ok
    end

    test "a blank description is refused, because _normalized makes it absent" do
      payload = blocker_payload([blocker_created()], %{"description" => "   "})

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected", "blocker description is required"}
    end

    test "an unknown status is refused" do
      payload = blocker_payload([blocker_created()], %{"status" => "closed"})

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected", "blocker status is invalid"}
    end

    test "a boolean is not a status, even though Python's bool is an int" do
      payload = blocker_payload([blocker_created()], %{"status" => true})

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected", "blocker status is invalid"}
    end

    test "an open blocker with resolvedAt is refused" do
      payload = blocker_payload([blocker_created()], %{"resolvedAt" => @now})

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected", "open blocker cannot have resolvedAt"}
    end

    test "a resolved blocker without resolvedAt is refused" do
      resolved = blocker_created(%{"id" => "e2", "kind" => "resolved"})

      payload = blocker_payload([blocker_created(), resolved], %{"status" => "resolved"})

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected", "resolved blocker requires resolvedAt"}
    end

    test "a blank itemID is refused" do
      payload = blocker_payload([blocker_created()], %{"itemID" => " "})

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected", "blocker itemID is required"}
    end
  end

  describe "blocker: the history rules, app/lifecycle.py:148-186" do
    test "history is required for a blocker" do
      bare = %{"status" => "open", "description" => "d", "itemID" => "i1"}

      assert decide(blocker_record(), bare) ==
               {:rejected, "lifecycle_rejected", "blocker history is required"}

      assert decide(blocker_record(), Map.put(bare, "events", [])) ==
               {:rejected, "lifecycle_rejected", "blocker history is required"}
    end

    test "a non-object in the history is malformed, not missing" do
      payload =
        blocker_payload([blocker_created(), "nope"], %{"description" => "waiting on steel"})

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected", "blocker history is malformed"}
    end

    test "event ids must be present and unique" do
      assert decide(blocker_record(), blocker_payload([blocker_created(%{"id" => " "})])) ==
               {:rejected, "lifecycle_rejected", "blocker event id is required"}

      assert decide(blocker_record(), blocker_payload([blocker_created(), blocker_created()])) ==
               {:rejected, "lifecycle_rejected", "blocker event ids must be unique"}
    end

    test "an unknown event kind is refused" do
      assert decide(
               blocker_record(),
               blocker_payload([blocker_created(%{"kind" => "escalated"})])
             ) ==
               {:rejected, "lifecycle_rejected", "blocker event kind is invalid"}
    end

    test "every event must carry the blocker's own identity" do
      assert decide(
               blocker_record(),
               blocker_payload([blocker_created(%{"blockerID" => "other"})])
             ) ==
               {:rejected, "lifecycle_rejected", "blocker event identity is invalid"}

      assert decide(blocker_record(), blocker_payload([blocker_created(%{"itemID" => "other"})])) ==
               {:rejected, "lifecycle_rejected", "blocker event identity is invalid"}
    end

    test "provenance must be complete on every event" do
      incomplete = blocker_created(%{"actorID" => nil})

      assert decide(blocker_record(), blocker_payload([incomplete])) ==
               {:rejected, "lifecycle_rejected", "blocker event provenance is incomplete"}
    end

    test "an event from another organization is refused" do
      foreign = blocker_created(%{"organizationID" => "other"})

      assert decide(blocker_record(), blocker_payload([foreign]), nil, principal()) ==
               {:rejected, "lifecycle_rejected", "blocker event organization is invalid"}
    end
  end

  describe "blocker: the state machine, app/lifecycle.py:155-186" do
    test "an update cannot precede creation" do
      assert decide(blocker_record(), blocker_payload([blocker_updated()])) ==
               {:rejected, "lifecycle_rejected", "blocker update cannot precede creation"}
    end

    test "a second created event is refused" do
      again = blocker_created(%{"id" => "e2"})

      assert decide(blocker_record(), blocker_payload([blocker_created(), again])) ==
               {:rejected, "lifecycle_rejected", "blocker created event must be first"}
    end

    test "an update must record a material change against the previous event" do
      # `previous_snapshot` is the *immediately preceding* event, whatever its
      # kind, and the six snapshot fields are the material ones. An update that
      # restates the previous event's description is a no-op and is refused.
      assert decide(blocker_record(), blocker_payload([blocker_created(), blocker_updated()])) ==
               :ok

      no_change = blocker_updated(%{"description" => "waiting on steel"})

      assert decide(blocker_record(), blocker_payload([blocker_created(), no_change])) ==
               {:rejected, "lifecycle_rejected",
                "blocker updated event must record a material field change"}

      unchanged = blocker_created(%{"id" => "e0", "description" => "changed"})

      assert decide(blocker_record(), blocker_payload([unchanged, blocker_updated()])) ==
               {:rejected, "lifecycle_rejected",
                "blocker updated event must record a material field change"}
    end

    test "resolving before creation is refused" do
      # `if state != "open"` with `state is None` on the first event.
      assert decide(blocker_record(), blocker_payload([blocker_created(%{"kind" => "resolved"})])) ==
               {:rejected, "lifecycle_rejected", "blocker can only resolve from open"}
    end

    test "reopening before resolving is refused" do
      reopened = blocker_created(%{"id" => "e2", "kind" => "reopened"})

      assert decide(blocker_record(), blocker_payload([blocker_created(), reopened])) ==
               {:rejected, "lifecycle_rejected", "blocker can only reopen from resolved"}
    end

    test "open -> resolved -> reopened is a legal walk" do
      walk = [
        blocker_created(),
        blocker_created(%{"id" => "e2", "kind" => "resolved"}),
        blocker_created(%{"id" => "e3", "kind" => "reopened"})
      ]

      assert decide(blocker_record(), blocker_payload(walk)) == :ok
    end

    test "resolving once and leaving the status open is a status mismatch" do
      resolved = blocker_created(%{"id" => "e2", "kind" => "resolved"})

      assert decide(blocker_record(), blocker_payload([blocker_created(), resolved])) ==
               {:rejected, "lifecycle_rejected",
                "blocker status does not match lifecycle history"}
    end

    test "the payload status must equal the state the history reaches" do
      payload =
        blocker_payload([blocker_created()], %{"status" => "resolved", "resolvedAt" => @now})

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected",
                "blocker status does not match lifecycle history"}
    end
  end

  describe "blocker: the latest-event snapshot, app/lifecycle.py:104-127" do
    test "a field present on the last event must agree with the payload" do
      event = blocker_created(%{"description" => "event description"})

      assert decide(blocker_record(), blocker_payload([event], %{"description" => "payload"})) ==
               {:rejected, "lifecycle_rejected",
                "blocker latest event description does not match snapshot"}
    end

    test "a field the last event omits is not compared at all" do
      # The reference opens each check with `if event_key not in event: continue`,
      # so a payload may carry snapshot fields the appended event never set.
      payload =
        blocker_payload([blocker_created()], %{"critical" => true, "vendorSupplier" => "acme"})

      assert decide(blocker_record(), payload) == :ok
    end

    test "the six snapshot keys are the ones checked" do
      event =
        blocker_created(%{
          "responsiblePerson" => "ada",
          "vendorSupplier" => "acme",
          "critical" => true
        })

      payload =
        blocker_payload([event], %{
          "responsiblePerson" => "grace",
          "vendorSupplier" => "acme",
          "critical" => true
        })

      assert decide(blocker_record(), payload) ==
               {:rejected, "lifecycle_rejected",
                "blocker latest event responsiblePerson does not match snapshot"}
    end

    test "critical is compared as a value, so 1 satisfies true" do
      event = blocker_created(%{"critical" => true})
      payload = blocker_payload([event], %{"critical" => 1})

      assert decide(blocker_record(), payload) == :ok
    end
  end

  describe "blocker: the immutable prefix, app/lifecycle.py:81-93 and 187-193" do
    test "an existing history cannot be truncated" do
      stored = blocker_row(blocker_payload([blocker_created(), blocker_updated()]))

      assert decide(blocker_record(), blocker_payload([blocker_created()]), stored) ==
               {:rejected, "lifecycle_rejected", "blocker history cannot be truncated"}
    end

    test "an existing history cannot be rewritten" do
      stored = blocker_row(blocker_payload([blocker_created(), blocker_updated()]))

      rewritten =
        blocker_payload([blocker_created(), blocker_updated(%{"description" => "quietly"})])

      assert decide(blocker_record(), rewritten, stored) ==
               {:rejected, "lifecycle_rejected", "blocker history is immutable"}
    end

    test "a canonical history that is not a list of objects is malformed" do
      stored = blocker_row(%{"events" => "nope"})

      assert decide(blocker_record(), blocker_payload([blocker_created()]), stored) ==
               {:rejected, "lifecycle_rejected", "canonical blocker history is malformed"}
    end

    test "an empty canonical history is not malformed, because `or []` applies" do
      stored =
        blocker_row(%{
          "events" => nil,
          "status" => "open",
          "description" => "d",
          "itemID" => "i1"
        })

      assert decide(blocker_record(), blocker_payload([blocker_created()]), stored) == :ok
    end

    test "appending is allowed" do
      stored = blocker_row(blocker_payload([blocker_created()]))

      assert decide(
               blocker_record(),
               blocker_payload([blocker_created(), blocker_updated()]),
               stored
             ) ==
               :ok
    end

    test "the appended tail, and only the tail, must carry current provenance (S-120)" do
      stored = blocker_row(blocker_payload([blocker_created()]))

      stale =
        blocker_payload([blocker_created(), blocker_updated(%{"authorizationRevision" => 3})])

      assert decide(blocker_record(), stale, stored, principal()) ==
               {:rejected, "lifecycle_rejected",
                "blocker latest event does not match authenticated provenance"}

      incomplete =
        blocker_payload([blocker_created(), blocker_updated(%{"authorizationRevision" => nil})])

      assert decide(blocker_record(), incomplete, stored) ==
               {:rejected, "lifecycle_rejected", "blocker event provenance is incomplete"}
    end

    test "a protected-field change with no appended event is refused" do
      stored =
        blocker_row(
          blocker_payload([blocker_created(), blocker_updated()], %{"critical" => true})
        )

      unchanged_history = blocker_payload([blocker_created(), blocker_updated()])

      assert decide(blocker_record(), unchanged_history, stored) ==
               {:rejected, "lifecycle_rejected", "blocker changes must append a lifecycle event"}
    end

    test "restating the same protected fields with no append is allowed" do
      stored =
        blocker_row(
          blocker_payload([blocker_created(), blocker_updated()], %{"critical" => true})
        )

      same = blocker_payload([blocker_created(), blocker_updated()], %{"critical" => true})

      assert decide(blocker_record(), same, stored) == :ok
    end
  end

  describe "quotation: status and event vocabulary, app/lifecycle.py:226-241" do
    test "an unknown status is refused" do
      assert decide(quotation_record(), %{"status" => "cancelled"}) ==
               {:rejected, "lifecycle_rejected", "quotation status is invalid"}
    end

    test "history is optional for a quotation" do
      assert decide(quotation_record(), %{"status" => "draft"}, nil, principal()) == :ok
    end

    test "a present history that is not a list is 'required', not 'malformed'" do
      assert decide(quotation_record(), %{"status" => "draft", "events" => %{}}, nil, principal()) ==
               {:rejected, "lifecycle_rejected", "quotation history is required"}
    end

    test "an unknown event kind is refused" do
      events = [quote_event("e1", "drafted")]

      assert decide(quotation_record(), quote_payload("draft", events), nil, principal()) ==
               {:rejected, "lifecycle_rejected", "quotation event kind is invalid"}
    end

    test "event ids must be unique and carry provenance" do
      duplicated = [quote_event("e1", "sent"), quote_event("e1", "approved")]

      assert decide(quotation_record(), quote_payload("approved", duplicated), nil, principal()) ==
               {:rejected, "lifecycle_rejected", "quotation event ids must be unique"}

      incomplete = [%{"id" => "e1", "kind" => "sent", "organizationID" => "org"}]

      assert decide(quotation_record(), quote_payload("sent", incomplete), nil, principal()) ==
               {:rejected, "lifecycle_rejected", "quotation event provenance is incomplete"}
    end
  end

  defp predecessor(overrides \\ %{}) do
    %Entity{
      entity_type: "quotation",
      entity_id: "q0",
      payload:
        Map.merge(
          %{"projectID" => "p1", "customerID" => "c1", "revision" => 1, "status" => "approved"},
          overrides
        ),
      deleted_at: nil
    }
  end

  defp replacement_payload do
    %{
      "status" => "draft",
      "revision" => 2,
      "supersedesQuotationID" => "q0",
      "projectID" => "p1",
      "customerID" => "c1"
    }
  end

  describe "quotation: the reachability walk, app/lifecycle.py:200-224" do
    test "sent is reachable from the draft closure, and only once" do
      # The state set starts as {draft}; its no-event closure is {draft, ready},
      # which is what makes a first `sent` legal without any earlier event.
      assert decide(
               quotation_record(),
               quote_payload("sent", [quote_event("e1", "sent")]),
               nil,
               principal()
             ) ==
               :ok

      twice = [quote_event("e1", "sent"), quote_event("e2", "sent")]

      assert decide(quotation_record(), quote_payload("sent", twice), nil, principal()) ==
               {:rejected, "lifecycle_rejected", "quotation lifecycle event sequence is invalid"}
    end

    test "approved follows sent, and the payload status must match the chain" do
      chain = [quote_event("e1", "sent"), quote_event("e2", "approved")]

      assert decide(quotation_record(), quote_payload("approved", chain), nil, principal()) == :ok

      assert decide(quotation_record(), quote_payload("sent", chain), nil, principal()) ==
               {:rejected, "lifecycle_rejected",
                "quotation status is not reachable from lifecycle history"}
    end

    test "approved with no preceding sent is an invalid sequence" do
      assert decide(
               quotation_record(),
               quote_payload("approved", [quote_event("e1", "approved")]),
               nil,
               principal()
             ) ==
               {:rejected, "lifecycle_rejected", "quotation lifecycle event sequence is invalid"}
    end

    test "rejected and superseded both follow sent" do
      after_sent = fn kind, status ->
        decide(
          quotation_record(),
          quote_payload(status, [quote_event("e1", "sent"), quote_event("e2", kind)]),
          nil,
          principal()
        )
      end

      assert after_sent.("rejected", "rejected") == :ok
      assert after_sent.("superseded", "superseded") == :ok
    end

    test "the appended tail must carry current provenance" do
      stored = quotation_row(quote_payload("sent", [quote_event("e1", "sent")]))

      appended = [quote_event("e1", "sent"), quote_event("e2", "approved")]

      assert decide(quotation_record(), quote_payload("approved", appended), stored, principal()) ==
               :ok

      stale = [
        quote_event("e1", "sent"),
        quote_event("e2", "approved", %{"authorizationRevision" => 1})
      ]

      assert decide(quotation_record(), quote_payload("approved", stale), stored, principal()) ==
               {:rejected, "lifecycle_rejected",
                "quotation latest event does not match authenticated provenance"}
    end
  end

  describe "quotation: revision and supersession, app/lifecycle.py:283-322" do
    test "revision 1 cannot supersede another quotation" do
      payload = %{"status" => "draft", "revision" => 1, "supersedesQuotationID" => "q0"}

      assert decide(quotation_record(), payload, nil, principal()) ==
               {:rejected, "lifecycle_rejected",
                "quotation revision 1 cannot supersede another quotation"}
    end

    test "a revision below 1 is refused, and a boolean is not a revision" do
      assert decide(quotation_record(), %{"status" => "draft", "revision" => 0}, nil, principal()) ==
               {:rejected, "lifecycle_rejected", "quotation revision must be at least 1"}

      assert decide(
               quotation_record(),
               %{"status" => "draft", "revision" => true},
               nil,
               principal()
             ) ==
               {:rejected, "lifecycle_rejected", "quotation revision must be at least 1"}
    end

    test "a replacement without a predecessor is refused" do
      assert decide(quotation_record(), %{"status" => "draft", "revision" => 2}, nil, principal()) ==
               {:rejected, "lifecycle_rejected", "replacement quotation requires a predecessor"}
    end

    test "a replacement may not supersede itself" do
      payload = %{"status" => "draft", "revision" => 2, "supersedesQuotationID" => "q1"}

      assert decide(quotation_record(), payload, nil, principal()) ==
               {:rejected, "lifecycle_rejected", "replacement quotation requires a predecessor"}
    end

    test "a canonical, active, contiguous predecessor on the same parent passes" do
      world = %Canonical.World{entities: [predecessor()]}

      assert decide(quotation_record(), replacement_payload(), nil, principal(), world) == :ok
    end

    test "a tombstoned or missing predecessor is refused" do
      tombstoned = %Canonical.World{entities: [%{predecessor() | deleted_at: "t"}]}

      assert decide(quotation_record(), replacement_payload(), nil, principal(), tombstoned) ==
               {:rejected, "lifecycle_rejected",
                "quotation predecessor is not canonical and active"}

      assert decide(
               quotation_record(),
               replacement_payload(),
               nil,
               principal(),
               Canonical.empty_world()
             ) ==
               {:rejected, "lifecycle_rejected",
                "quotation predecessor is not canonical and active"}
    end

    test "a predecessor with a non-object payload is not canonical" do
      world = %Canonical.World{entities: [%{predecessor() | payload: nil}]}

      assert decide(quotation_record(), replacement_payload(), nil, principal(), world) ==
               {:rejected, "lifecycle_rejected",
                "quotation predecessor is not canonical and active"}
    end

    test "a predecessor on another Project or Customer is refused" do
      wrong_project = %Canonical.World{entities: [predecessor(%{"projectID" => "p2"})]}

      assert decide(quotation_record(), replacement_payload(), nil, principal(), wrong_project) ==
               {:rejected, "lifecycle_rejected",
                "quotation predecessor belongs to another Project"}

      wrong_customer = %Canonical.World{entities: [predecessor(%{"customerID" => "c2"})]}

      assert decide(quotation_record(), replacement_payload(), nil, principal(), wrong_customer) ==
               {:rejected, "lifecycle_rejected",
                "quotation predecessor belongs to another Customer"}
    end

    test "the predecessor revision must be exactly one less" do
      world = %Canonical.World{entities: [predecessor(%{"revision" => 7})]}

      assert decide(quotation_record(), replacement_payload(), nil, principal(), world) ==
               {:rejected, "lifecycle_rejected",
                "quotation predecessor revision is not contiguous"}
    end
  end

  describe "quotation: an update, app/lifecycle.py:298-311" do
    test "the revision and the supersession identity are both immutable" do
      stored =
        quotation_row(%{"status" => "draft", "revision" => 1, "supersedesQuotationID" => nil})

      assert decide(
               quotation_record(),
               %{"status" => "draft", "revision" => 2},
               stored,
               principal()
             ) ==
               {:rejected, "lifecycle_rejected", "quotation revision is immutable"}

      rebased = %{"status" => "draft", "revision" => 1, "supersedesQuotationID" => "q0"}

      assert decide(quotation_record(), rebased, stored, principal()) ==
               {:rejected, "lifecycle_rejected", "quotation supersession identity is immutable"}
    end

    test "customer-facing content is locked once the canonical status has left draft" do
      # The proposed history has to equal the canonical one, or the content lock
      # is not reached: an appended tail goes down the provenance branch instead.
      history = [quote_event("e1", "sent"), quote_event("e2", "approved")]

      stored =
        quotation_row(%{
          "status" => "sent",
          "revision" => 1,
          "events" => history,
          "lines" => [%{"itemID" => "i1", "totalPrice" => 100}]
        })

      same = %{
        "status" => "approved",
        "revision" => 1,
        "events" => history,
        "lines" => [%{"itemID" => "i1", "totalPrice" => 100}]
      }

      assert decide(quotation_record(), same, stored, principal()) == :ok

      repriced = Map.put(same, "lines", [%{"itemID" => "i1", "totalPrice" => 90}])

      assert decide(quotation_record(), repriced, stored, principal()) ==
               {:rejected, "lifecycle_rejected",
                "customer-facing quotation content is locked; create a replacement revision"}
    end

    test "status, events and updatedAt are not commercial content" do
      history = [quote_event("e1", "sent")]

      stored =
        quotation_row(%{
          "status" => "sent",
          "revision" => 1,
          "updatedAt" => "old",
          "events" => history,
          "lines" => [%{"itemID" => "i1"}]
        })

      moved = %{
        "status" => "approved",
        "revision" => 1,
        "updatedAt" => "new",
        "events" => history ++ [quote_event("e2", "approved")],
        "lines" => [%{"itemID" => "i1"}]
      }

      assert decide(quotation_record(), moved, stored, principal()) == :ok
    end

    test "a corrupt canonical status is refused rather than read as unlocked" do
      stored = quotation_row(%{"status" => "nonsense", "revision" => 1})

      assert decide(
               quotation_record(),
               %{"status" => "draft", "revision" => 1},
               stored,
               principal()
             ) ==
               {:rejected, "lifecycle_rejected", "canonical quotation status is invalid"}
    end

    test "a corrupt canonical revision is refused too, rather than read as 1" do
      stored = quotation_row(%{"status" => "draft", "revision" => 0})

      assert decide(
               quotation_record(),
               %{"status" => "draft", "revision" => 0},
               stored,
               principal()
             ) ==
               {:rejected, "lifecycle_rejected", "quotation revision must be at least 1"}
    end
  end

  describe "the rejection code" do
    test "is lifecycle_rejected, never a Python class name" do
      # S-71 / app/rejection_codes.py:69 -- `LifecycleRejected` maps to the token.
      assert decide(blocker_record(), %{}) ==
               {:rejected, "lifecycle_rejected", "blocker description is required"}
    end

    test "and the third element is the reference's own message, so a test can name the rule" do
      {:rejected, code, message} = decide(quotation_record(), %{"status" => "nonsense"})

      assert code == "lifecycle_rejected"
      assert message == "quotation status is invalid"
    end
  end
end
