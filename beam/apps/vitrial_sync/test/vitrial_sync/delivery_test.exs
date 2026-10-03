defmodule VitrialSync.DeliveryTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Canonical, Delivery}

  alias VitrialSync.Canonical.{
    Customer,
    Entity,
    Item,
    ItemChild,
    Principal,
    Project,
    ProjectChild,
    Scope
  }

  @t0 "2026-01-01T00:00:00Z"
  @t1 "2026-01-02T00:00:00Z"
  @t2 "2026-01-03T00:00:00Z"
  @t3 "2026-01-04T00:00:00Z"
  @t4 "2026-01-05T00:00:00Z"
  @t5 "2026-01-06T00:00:00Z"
  @t6 "2026-01-07T00:00:00Z"
  @t1b "2026-01-02T12:00:00Z"

  defp principal(capabilities) do
    %Principal{
      user_id: "u1",
      organization_id: "org",
      membership_id: "m1",
      session_id: "s1",
      authorization_revision: 4,
      all_customers: true,
      all_projects: true,
      capabilities: MapSet.new(capabilities)
    }
  end

  defp capable, do: principal(["delivery.manage"])

  defp world(attrs) do
    struct!(%Canonical.World{}, attrs)
  end

  defp chain(attrs \\ []) do
    struct!(
      world(
        customers: [%Customer{customer_id: "c1"}],
        projects: [%Project{project_id: "p1", customer_id: "c1"}],
        items: [%Item{item_id: "i1", project_id: "p1"}]
      ),
      attrs
    )
  end

  defp quotation_payload(overrides \\ %{}) do
    Map.merge(
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
      },
      overrides
    )
  end

  defp quotation_world(overrides \\ []) do
    chain(
      Keyword.merge(
        [
          entities: [
            %Entity{entity_type: "quotation", entity_id: "q1", payload: quotation_payload()}
          ]
        ],
        overrides
      )
    )
  end

  defp with_evidence do
    quotation_world(
      item_children: [%ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"}]
    )
  end

  # An execution row that already exists: the world must carry both halves, or
  # the continuity check refuses before the history rules are reached.
  defp existing_world(payload) do
    %{
      with_evidence()
      | project_children: [
          %ProjectChild{entity_type: "delivery_execution", entity_id: "d1", project_id: "p1"}
        ],
        entities:
          with_evidence().entities ++
            [
              %Entity{
                entity_type: "delivery_execution",
                entity_id: "d1",
                payload: payload
              }
            ]
    }
  end

  defp stored(payload),
    do: %Entity{entity_type: "delivery_execution", entity_id: "d1", payload: payload}

  defp authorize(payload, opts \\ []) do
    principal = Keyword.get(opts, :principal, capable())
    world = Keyword.get(opts, :world, quotation_world())
    scope = Scope.from_principal(principal)

    Delivery.authorize(
      "d1",
      payload,
      Keyword.get(opts, :deleted_at),
      Keyword.get(opts, :current),
      principal,
      scope,
      world
    )
  end

  # ---------------------------------------------------------------------------
  # fixtures
  # ---------------------------------------------------------------------------

  defp event(id, from, to, occurred_at, overrides \\ %{}) do
    Map.merge(
      %{
        "id" => id,
        "fromStatus" => from,
        "toStatus" => to,
        "occurredAt" => occurred_at,
        "actorID" => "u1",
        "organizationID" => "org",
        "membershipID" => "m1",
        "sessionID" => "s1",
        "authorizationRevision" => 4
      },
      overrides
    )
  end

  # The opening event has no `fromStatus`: the reference tests
  # `first.get("fromStatus") is not None or first.get("toStatus") != "engineeringReview"`.
  defp opening_event, do: event("e1", nil, "engineeringReview", @t0)

  defp delivery_payload(overrides \\ %{}) do
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
        "startedAt" => @t0,
        "updatedAt" => @t0,
        "events" => [opening_event()]
      },
      overrides
    )
  end

  defp materials_plan(revision, updated_at) do
    %{
      "revision" => revision,
      "updatedAt" => updated_at,
      "requirements" => [
        %{"id" => "m1", "description" => "steel plate", "unit" => "kg", "quantity" => "10"}
      ],
      "actorID" => "u1",
      "organizationID" => "org",
      "membershipID" => "m1",
      "sessionID" => "s1",
      "authorizationRevision" => 4
    }
  end

  defp procurement_plan do
    %{
      "revision" => 1,
      "materialsPlanRevision" => 1,
      "updatedAt" => @t2,
      "requirements" => [%{"requirementID" => "m1", "status" => "available"}],
      "actorID" => "u1",
      "organizationID" => "org",
      "membershipID" => "m1",
      "sessionID" => "s1",
      "authorizationRevision" => 4
    }
  end

  defp production_plan do
    %{
      "revision" => 1,
      "procurementPlanRevision" => 1,
      "updatedAt" => @t3,
      "steps" => [%{"id" => "s1", "description" => "fabricate", "status" => "complete"}],
      "actorID" => "u1",
      "organizationID" => "org",
      "membershipID" => "m1",
      "sessionID" => "s1",
      "authorizationRevision" => 4
    }
  end

  defp installation_schedule do
    %{
      "revision" => 1,
      "productionPlanRevision" => 1,
      "scheduledFor" => @t5,
      "updatedAt" => @t4,
      "installerAssignment" => %{"id" => "ins1", "displayName" => "Ada"},
      "actorID" => "u1",
      "organizationID" => "org",
      "membershipID" => "m1",
      "sessionID" => "s1",
      "authorizationRevision" => 4
    }
  end

  defp installation_completion(overrides \\ %{}) do
    Map.merge(
      %{
        "revision" => 1,
        "installationScheduleRevision" => 1,
        "completedAt" => @t6,
        "updatedAt" => @t6,
        "evidenceReferences" => [%{"itemID" => "i1", "documentID" => "e1"}],
        "actorID" => "u1",
        "organizationID" => "org",
        "membershipID" => "m1",
        "sessionID" => "s1",
        "authorizationRevision" => 4
      },
      overrides
    )
  end

  # An execution that has reached Scheduled, with every handoff plan frozen. The
  # event walk is a real one, because the chain is validated.
  defp scheduled_payload(overrides \\ %{}) do
    events = [
      opening_event(),
      event("e2", "engineeringReview", "materialsRequired", @t1),
      event("e3", "materialsRequired", "procurement", @t2),
      event("e4", "procurement", "production", @t3),
      event("e5", "production", "readyForInstallation", @t4),
      event("e6", "readyForInstallation", "scheduled", @t5)
    ]

    Map.merge(
      %{
        "id" => "d1",
        "status" => "scheduled",
        "projectID" => "p1",
        "customerID" => "c1",
        "quotationID" => "q1",
        "quotationNumber" => "Q-1",
        "quotationRevision" => 1,
        "currencyCode" => "EUR",
        "pricingVersion" => 3,
        "quotedTotal" => "100.00",
        "startedAt" => @t0,
        "updatedAt" => @t5,
        "events" => events,
        "materialsPlan" => materials_plan(1, @t1),
        "procurementPlan" => procurement_plan(),
        "productionPlan" => production_plan(),
        "installationSchedule" => installation_schedule()
      },
      overrides
    )
  end

  # An execution parked at Materials Required with one saved plan revision: the
  # stage where a self-transition is legal, and the only stage where a plan can
  # still move.
  defp materials_payload(events, plan, updated_at) do
    %{
      "id" => "d1",
      "status" => "materialsRequired",
      "projectID" => "p1",
      "customerID" => "c1",
      "quotationID" => "q1",
      "quotationNumber" => "Q-1",
      "quotationRevision" => 1,
      "currencyCode" => "EUR",
      "pricingVersion" => 3,
      "quotedTotal" => "100.00",
      "startedAt" => @t0,
      "updatedAt" => updated_at,
      "events" => events,
      "materialsPlan" => plan
    }
  end

  defp saved_materials do
    materials_payload(
      [opening_event(), event("e2", "engineeringReview", "materialsRequired", @t1)],
      materials_plan(1, @t1),
      @t1
    )
  end

  # ---------------------------------------------------------------------------
  # the gate
  # ---------------------------------------------------------------------------

  describe "the gate owns exactly one type, S-60" do
    test "the dispatch names delivery_execution and nothing else" do
      assert Delivery.entity_type() == "delivery_execution"
      assert Delivery.delivery_entity?("delivery_execution")
      refute Delivery.delivery_entity?("customer")
      refute Delivery.delivery_entity?("item")
      refute Delivery.delivery_entity?("quotation")
    end
  end

  describe "capability, app/delivery_execution.py:703-717" do
    test "delivery.manage is required before anything else is read" do
      assert {:error, "delivery.manage capability required"} =
               authorize(delivery_payload(),
                 principal: principal([]),
                 world: Canonical.empty_world()
               )
    end

    test "an authorized creation yields the project it hangs from" do
      assert {:ok, "p1"} = authorize(delivery_payload())
    end
  end

  describe "S-137: a delivery tombstone is refused outright" do
    test "the refusal is the first thing after the capability" do
      assert {:error, "delivery execution history cannot be tombstoned"} =
               authorize(delivery_payload(), deleted_at: @t0, world: Canonical.empty_world())
    end
  end

  describe "continuity, app/delivery_execution.py:718-746" do
    test "an ownership row with no payload is a partial migration, not a create" do
      world =
        quotation_world(
          project_children: [
            %ProjectChild{entity_type: "delivery_execution", entity_id: "d1", project_id: "p1"}
          ]
        )

      assert {:error, "delivery execution canonical ownership exists without canonical payload"} =
               authorize(delivery_payload(), world: world)
    end

    test "a tombstoned stored payload must stay inert rather than be resurrected" do
      current = %Entity{
        entity_type: "delivery_execution",
        entity_id: "d1",
        payload: delivery_payload(),
        deleted_at: @t0
      }

      assert {:error, "existing delivery execution payload is not canonical and active"} =
               authorize(delivery_payload(),
                 current: current,
                 world: existing_world(delivery_payload())
               )
    end

    test "a stored payload that is not an object is not a payload" do
      current = %Entity{
        entity_type: "delivery_execution",
        entity_id: "d1",
        payload: "not-an-object"
      }

      assert {:error, "existing delivery execution payload is not canonical and active"} =
               authorize(delivery_payload(),
                 current: current,
                 world: existing_world(delivery_payload())
               )
    end

    test "a payload with no ownership row is refused" do
      current = %Entity{
        entity_type: "delivery_execution",
        entity_id: "d1",
        payload: delivery_payload()
      }

      assert {:error, "existing delivery execution lacks canonical ownership"} =
               authorize(delivery_payload(), current: current)
    end

    test "a tombstoned ownership row is refused" do
      world =
        quotation_world(
          project_children: [
            %ProjectChild{
              entity_type: "delivery_execution",
              entity_id: "d1",
              project_id: "p1",
              deleted_at: @t0
            }
          ]
        )

      current = %Entity{
        entity_type: "delivery_execution",
        entity_id: "d1",
        payload: delivery_payload()
      }

      assert {:error, "existing delivery execution ownership is tombstoned"} =
               authorize(delivery_payload(), current: current, world: world)
    end
  end

  # ---------------------------------------------------------------------------
  # the payload
  # ---------------------------------------------------------------------------

  describe "the payload's own identity and status" do
    test "the payload id must be the entityID" do
      assert {:error, "delivery execution payload identity is invalid"} =
               authorize(delivery_payload(%{"id" => "d2"}))
    end

    test "a status outside the state machine is refused" do
      assert {:error, "delivery execution status is invalid"} =
               authorize(delivery_payload(%{"status" => "cancelled"}))
    end
  end

  describe "the event history, app/delivery_execution.py:115-123 and 545-570" do
    test "history is mandatory here, unlike the optional lifecycle histories" do
      assert {:error, "delivery execution history is required"} =
               authorize(delivery_payload(%{"events" => nil}))

      assert {:error, "delivery execution history is required"} =
               authorize(delivery_payload(%{"events" => []}))

      assert {:error, "delivery execution history is required"} =
               authorize(delivery_payload(%{"events" => "e1"}))

      assert {:error, "delivery execution history is malformed"} =
               authorize(delivery_payload(%{"events" => ["e1"]}))
    end

    test "every event needs a unique id" do
      duplicated = [opening_event(), event("e1", "engineeringReview", "materialsRequired", @t1)]

      assert {:error, "delivery execution event ids must be unique"} =
               authorize(delivery_payload(%{"events" => duplicated, "updatedAt" => @t1}))

      blank = [event("e1", nil, "engineeringReview", @t0, %{"id" => "  "})]

      assert {:error, "delivery execution event id is required"} =
               authorize(delivery_payload(%{"events" => blank}))
    end

    test "every historical event needs complete provenance, not just the newest" do
      stale = [
        opening_event(),
        event("e2", "engineeringReview", "materialsRequired", @t1, %{"organizationID" => "other"})
      ]

      assert {:error, "delivery execution event organization is invalid"} =
               authorize(delivery_payload(%{"events" => stale, "updatedAt" => @t1}))

      incomplete = [
        opening_event(),
        event("e2", "engineeringReview", "materialsRequired", @t1, %{"sessionID" => nil})
      ]

      assert {:error, "delivery execution event provenance is incomplete"} =
               authorize(delivery_payload(%{"events" => incomplete, "updatedAt" => @t1}))
    end

    test "a creation must open at Engineering Review" do
      opening = [event("e1", "engineeringReview", "materialsRequired", @t0)]

      assert {:error, "delivery execution must start with Engineering Review"} =
               authorize(delivery_payload(%{"events" => opening}))
    end

    test "the chain must be continuous" do
      broken = [opening_event(), event("e2", "procurement", "production", @t1)]

      assert {:error, "delivery execution event chain is discontinuous"} =
               authorize(delivery_payload(%{"events" => broken, "updatedAt" => @t1}))
    end

    test "a stage may not be skipped" do
      skipped = [opening_event(), event("e2", "engineeringReview", "production", @t1)]

      assert {:error, "delivery execution transition is invalid"} =
               authorize(delivery_payload(%{"events" => skipped, "updatedAt" => @t1}))
    end

    test "a status outside the machine cannot appear in a walk" do
      off_machine = [opening_event(), event("e2", "engineeringReview", "shipped", @t1)]

      assert {:error, "delivery execution event status is invalid"} =
               authorize(delivery_payload(%{"events" => off_machine, "updatedAt" => @t1}))
    end

    test "a self-transition is a plan revision, not a state change" do
      # `allowed_transition?` admits five self-transitions. This one is legal to
      # walk; the plan revision it implies is the handoff's business.
      revisits = [
        opening_event(),
        event("e2", "engineeringReview", "materialsRequired", @t1),
        event("e3", "materialsRequired", "materialsRequired", @t2)
      ]

      payload =
        delivery_payload(%{
          "status" => "materialsRequired",
          "events" => revisits,
          "updatedAt" => @t2
        })

      assert {:ok, "p1"} = authorize(payload)
    end
  end

  describe "the timestamp anchors, app/delivery_execution.py:597-609" do
    test "updatedAt must be the newest event's occurredAt" do
      assert {:error, "delivery execution updatedAt must match its latest immutable event"} =
               authorize(delivery_payload(%{"updatedAt" => @t1}))
    end

    test "startedAt must be the first event's occurredAt" do
      assert {:error, "delivery execution startedAt must match its initial event"} =
               authorize(delivery_payload(%{"startedAt" => @t1}))
    end
  end

  # ---------------------------------------------------------------------------
  # the project
  # ---------------------------------------------------------------------------

  describe "the project half of the identity" do
    test "project, customer and quotation ids are all required" do
      for key <- ~w(projectID customerID quotationID) do
        assert {:error, "delivery execution project/customer/quotation identity is required"} =
                 authorize(Map.delete(delivery_payload(), key))
      end
    end

    test "the project must be canonical and live" do
      bare =
        world(
          entities: [
            %Entity{entity_type: "quotation", entity_id: "q1", payload: quotation_payload()}
          ]
        )

      assert {:error, "delivery execution Project is not canonical and active"} =
               authorize(delivery_payload(), world: bare)

      tombstoned =
        quotation_world(
          projects: [%Project{project_id: "p1", customer_id: "c1", deleted_at: @t0}]
        )

      assert {:error, "delivery execution Project is not canonical and active"} =
               authorize(delivery_payload(), world: tombstoned)
    end

    test "the customer must be the project's own" do
      assert {:error, "delivery execution Customer does not match canonical Project"} =
               authorize(delivery_payload(%{"customerID" => "c2"}))
    end

    test "the project must be in scope" do
      narrow = %Principal{
        user_id: "u1",
        organization_id: "org",
        membership_id: "m1",
        session_id: "s1",
        authorization_revision: 4,
        customer_ids: MapSet.new(["c1"]),
        project_ids: MapSet.new(),
        capabilities: MapSet.new(["delivery.manage"])
      }

      assert {:error, "delivery execution Project is outside authorized scope"} =
               authorize(delivery_payload(), principal: narrow)
    end
  end

  # ---------------------------------------------------------------------------
  # the quotation
  # ---------------------------------------------------------------------------

  describe "the quotation half of the authority, app/delivery_execution.py:784-826" do
    test "a quotation that is not canonical is refused" do
      assert {:error, "delivery execution requires a canonical approved quotation"} =
               authorize(delivery_payload(), world: chain())
    end

    test "only an approved quotation carries an execution" do
      world =
        quotation_world(
          entities: [
            %Entity{
              entity_type: "quotation",
              entity_id: "q1",
              payload: quotation_payload(%{"status" => "sent"})
            }
          ]
        )

      assert {:error, "delivery execution requires an approved quotation"} =
               authorize(delivery_payload(), world: world)
    end

    test "the approval must be in the immutable event history" do
      for events <- [[%{"kind" => "sent"}], nil, "approved"] do
        world =
          quotation_world(
            entities: [
              %Entity{
                entity_type: "quotation",
                entity_id: "q1",
                payload: quotation_payload(%{"events" => events})
              }
            ]
          )

        assert {:error, "delivery execution quotation is missing immutable approval history"} =
                 authorize(delivery_payload(), world: world)
      end
    end

    test "the quotation must belong to the same project and customer" do
      world =
        quotation_world(
          entities: [
            %Entity{
              entity_type: "quotation",
              entity_id: "q1",
              payload: quotation_payload(%{"projectID" => "p2"})
            }
          ]
        )

      assert {:error, "delivery execution quotation ownership does not match Project"} =
               authorize(delivery_payload(), world: world)
    end

    test "each bound quotation field is compared by name" do
      for {key, value} <- [
            {"quotationNumber", "Q-2"},
            {"quotationRevision", 2},
            {"currencyCode", "USD"},
            {"pricingVersion", 4}
          ] do
        assert {:error, message} = authorize(delivery_payload(%{key => value}))
        assert message == "delivery execution #{key} does not match canonical quotation"
      end
    end

    test "an explicit quotation revision must be a positive integer" do
      world =
        quotation_world(
          entities: [
            %Entity{
              entity_type: "quotation",
              entity_id: "q1",
              payload: quotation_payload(%{"revision" => 0})
            }
          ]
        )

      assert {:error, "canonical quotation revision is invalid"} =
               authorize(delivery_payload(), world: world)
    end

    test "a quotation with no revision is revision one" do
      assert {:ok, "p1"} = authorize(delivery_payload(%{"quotationRevision" => 1}))
    end
  end

  describe "quotedTotal is exact decimal arithmetic, not float arithmetic" do
    defp total_world(lines) do
      quotation_world(
        entities: [
          %Entity{
            entity_type: "quotation",
            entity_id: "q1",
            payload: quotation_payload(%{"lines" => lines})
          }
        ]
      )
    end

    test "0.1 + 0.2 is 0.3 here, because the sum is decimal, not binary" do
      world = total_world([%{"totalPrice" => "0.1"}, %{"totalPrice" => "0.2"}])

      # As binary doubles the sum would be 0.30000000000000004. It is
      # `Decimal(str(x))` that makes this true, and the reference uses Decimal.
      assert {:ok, "p1"} = authorize(delivery_payload(%{"quotedTotal" => "0.3"}), world: world)

      assert {:error, "delivery execution quotedTotal does not match canonical quotation"} =
               authorize(delivery_payload(%{"quotedTotal" => "0.30000000000000004"}),
                 world: world
               )
    end

    test "an integer and its float spelling are the same amount" do
      world = total_world([%{"totalPrice" => 1}])

      assert {:ok, "p1"} = authorize(delivery_payload(%{"quotedTotal" => 1.0}), world: world)
      assert {:ok, "p1"} = authorize(delivery_payload(%{"quotedTotal" => 1}), world: world)
      assert {:ok, "p1"} = authorize(delivery_payload(%{"quotedTotal" => "1.00"}), world: world)
    end

    test "a genuinely different amount is refused" do
      world = total_world([%{"totalPrice" => "100.00"}])

      assert {:error, "delivery execution quotedTotal does not match canonical quotation"} =
               authorize(delivery_payload(%{"quotedTotal" => "100.01"}), world: world)
    end

    test "an unparseable amount is refused rather than coerced" do
      assert {:error, "quotedTotal is invalid"} =
               authorize(delivery_payload(%{"quotedTotal" => "a lot"}))

      assert {:error, "quotedTotal is invalid"} =
               authorize(delivery_payload(%{"quotedTotal" => nil}))

      assert {:error, "quotedTotal is invalid"} =
               authorize(delivery_payload(%{"quotedTotal" => true}))

      world = total_world([%{"totalPrice" => "free"}])

      assert {:error, "canonical quotation totalPrice is invalid"} =
               authorize(delivery_payload(), world: world)
    end

    test "a quotation with no lines has no total" do
      assert {:error, "canonical quotation lines are unavailable"} =
               authorize(delivery_payload(), world: total_world([]))
    end
  end

  # ---------------------------------------------------------------------------
  # the handoffs
  # ---------------------------------------------------------------------------

  describe "the four handoffs, S-126" do
    test "a Materials Required plan may not be authored before Materials Required" do
      payload = Map.put(delivery_payload(), "materialsPlan", materials_plan(1, @t0))

      assert {:error, "delivery materials plan can only be authored during Materials Required"} =
               authorize(payload)
    end

    test "Procurement cannot be entered without a saved materials plan" do
      revisits = [
        opening_event(),
        event("e2", "engineeringReview", "materialsRequired", @t1),
        event("e3", "materialsRequired", "procurement", @t2)
      ]

      payload =
        delivery_payload(%{"status" => "procurement", "events" => revisits, "updatedAt" => @t2})

      assert {:error, "delivery materials plan is required before Procurement"} =
               authorize(payload)
    end

    test "Production cannot be entered until every frozen material is available" do
      chain_events = [
        opening_event(),
        event("e2", "engineeringReview", "materialsRequired", @t1),
        event("e3", "materialsRequired", "procurement", @t2),
        event("e4", "procurement", "production", @t3)
      ]

      ordered =
        Map.merge(delivery_payload(), %{
          "status" => "production",
          "events" => chain_events,
          "updatedAt" => @t3,
          "materialsPlan" => materials_plan(1, @t1)
        })

      short =
        Map.put(procurement_plan(), "requirements", [
          %{"requirementID" => "m1", "status" => "ordered"}
        ])

      assert {:error, "delivery Production is blocked until every frozen material is available"} =
               authorize(Map.put(ordered, "procurementPlan", short))
    end

    test "Ready for Installation is blocked until every production step is complete" do
      chain_events = [
        opening_event(),
        event("e2", "engineeringReview", "materialsRequired", @t1),
        event("e3", "materialsRequired", "procurement", @t2),
        event("e4", "procurement", "production", @t3),
        event("e5", "production", "readyForInstallation", @t4)
      ]

      payload =
        Map.merge(delivery_payload(), %{
          "status" => "readyForInstallation",
          "events" => chain_events,
          "updatedAt" => @t4,
          "materialsPlan" => materials_plan(1, @t1),
          "procurementPlan" => procurement_plan()
        })

      unfinished = %{
        "revision" => 1,
        "procurementPlanRevision" => 1,
        "updatedAt" => @t3,
        "steps" => [%{"id" => "s1", "description" => "fabricate", "status" => "inProgress"}],
        "actorID" => "u1",
        "organizationID" => "org",
        "membershipID" => "m1",
        "sessionID" => "s1",
        "authorizationRevision" => 4
      }

      assert {:error,
              "delivery Ready for Installation is blocked until every production step is complete"} =
               authorize(Map.put(payload, "productionPlan", unfinished))
    end
  end

  # ---------------------------------------------------------------------------
  # evidence authority
  # ---------------------------------------------------------------------------

  describe "installation evidence authority, app/delivery_installation.py:588-630" do
    test "a reference must name an active Item in the delivery project" do
      payload = scheduled_payload(%{"installationCompletion" => installation_completion()})

      assert {:ok, "p1"} = authorize(payload, world: with_evidence())
    end

    test "an Item in another project does not carry delivery evidence" do
      elsewhere = %{with_evidence() | items: [%Item{item_id: "i1", project_id: "p2"}]}
      payload = scheduled_payload(%{"installationCompletion" => installation_completion()})

      assert {:error,
              "delivery installation completion evidence Item is not active in the delivery Project"} =
               authorize(payload, world: elsewhere)
    end

    test "the document must be a live evidence child of that Item" do
      tombstoned = %{
        with_evidence()
        | item_children: [
            %ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1", deleted_at: @t0}
          ]
      }

      payload = scheduled_payload(%{"installationCompletion" => installation_completion()})

      assert {:error,
              "delivery installation completion evidence is not canonical for the referenced Item"} =
               authorize(payload, world: tombstoned)
    end

    test "completion evidence is required, and each reference is shaped" do
      empty =
        scheduled_payload(%{
          "installationCompletion" => installation_completion(%{"evidenceReferences" => []})
        })

      assert {:error, "delivery installation completion requires at least one evidence reference"} =
               authorize(empty, world: with_evidence())

      for reference <- [%{"itemID" => "i1"}, %{"documentID" => "e1"}] do
        payload =
          scheduled_payload(%{
            "installationCompletion" =>
              installation_completion(%{"evidenceReferences" => [reference]})
          })

        assert {:error,
                "delivery installation completion evidence reference requires itemID and documentID"} =
                 authorize(payload, world: with_evidence())
      end

      # A reference that is not an object at all is malformed rather than
      # incomplete; the shape test runs before the two-key test.
      for reference <- ["e1", 7] do
        payload =
          scheduled_payload(%{
            "installationCompletion" =>
              installation_completion(%{"evidenceReferences" => [reference]})
          })

        assert {:error, "delivery installation completion evidence reference is malformed"} =
                 authorize(payload, world: with_evidence())
      end
    end
  end

  # ---------------------------------------------------------------------------
  # updates
  # ---------------------------------------------------------------------------

  describe "history on an update, app/delivery_execution.py:611-700" do
    test "a self-transition that carries a plan revision is accepted" do
      events =
        saved_materials()["events"] ++
          [event("e3", "materialsRequired", "materialsRequired", @t2)]

      payload = materials_payload(events, materials_plan(2, @t2), @t2)

      assert {:ok, "p1"} =
               authorize(payload,
                 current: stored(saved_materials()),
                 world: existing_world(saved_materials())
               )
    end

    test "a self-transition with an unchanged plan is a no-op dressed as a transition" do
      events =
        saved_materials()["events"] ++
          [event("e3", "materialsRequired", "materialsRequired", @t2)]

      payload = materials_payload(events, materials_plan(1, @t1), @t2)

      assert {:error, "Materials Required self-transition requires a materials plan revision"} =
               authorize(payload,
                 current: stored(saved_materials()),
                 world: existing_world(saved_materials())
               )
    end

    test "a plan revision must advance by exactly one and be anchored to updatedAt" do
      events =
        saved_materials()["events"] ++
          [event("e3", "materialsRequired", "materialsRequired", @t2)]

      assert {:error, "delivery materials plan revision must advance by exactly one"} =
               authorize(
                 materials_payload(events, materials_plan(3, @t2), @t2),
                 current: stored(saved_materials()),
                 world: existing_world(saved_materials())
               )

      assert {:error, "delivery materials plan updatedAt must match delivery execution updatedAt"} =
               authorize(
                 materials_payload(events, materials_plan(2, @t1), @t2),
                 current: stored(saved_materials()),
                 world: existing_world(saved_materials())
               )
    end

    test "appending two transitions in one mutation is refused" do
      events =
        saved_materials()["events"] ++
          [
            event("e3", "materialsRequired", "materialsRequired", @t2),
            event("e4", "materialsRequired", "procurement", @t2)
          ]

      payload = materials_payload(events, materials_plan(2, @t2), @t2)

      assert {:error, "delivery execution may append only one transition per mutation"} =
               authorize(payload,
                 current: stored(saved_materials()),
                 world: existing_world(saved_materials())
               )
    end

    test "the newest appended event must be the current actor's" do
      events =
        saved_materials()["events"] ++
          [event("e3", "materialsRequired", "materialsRequired", @t2, %{"actorID" => "u0"})]

      payload = materials_payload(events, materials_plan(2, @t2), @t2)

      assert {:error, "delivery execution latest event does not match authenticated provenance"} =
               authorize(payload,
                 current: stored(saved_materials()),
                 world: existing_world(saved_materials())
               )
    end

    test "history may not be truncated" do
      shortened =
        scheduled_payload(%{
          "events" => Enum.take(scheduled_payload()["events"], 3),
          "updatedAt" => @t2
        })

      assert {:error, "delivery execution history cannot be truncated"} =
               authorize(shortened,
                 current: stored(scheduled_payload()),
                 world: existing_world(scheduled_payload())
               )
    end

    test "history may not be rewritten in place" do
      rewritten_events =
        [opening_event(), event("e2", "engineeringReview", "materialsRequired", @t1b)] ++
          Enum.drop(scheduled_payload()["events"], 2)

      rewritten = scheduled_payload(%{"events" => rewritten_events})

      assert {:error, "delivery execution history is immutable"} =
               authorize(rewritten,
                 current: stored(scheduled_payload()),
                 world: existing_world(scheduled_payload())
               )
    end

    test "a bound handoff field may not move on an update" do
      moved = scheduled_payload(%{"quotedTotal" => "120.00"})

      assert {:error, "delivery execution immutable handoff field quotedTotal changed"} =
               authorize(moved,
                 current: stored(scheduled_payload()),
                 world: existing_world(scheduled_payload())
               )
    end

    test "an operational change with no appended event is refused" do
      # Saving completion evidence while Scheduled is legal -- and it is an
      # operational change, so it has to be announced by an event.
      payload =
        scheduled_payload(%{
          "installationCompletion" => installation_completion(%{"updatedAt" => @t5})
        })

      assert {:error, "delivery execution operational changes must append a lifecycle event"} =
               authorize(payload,
                 current: stored(scheduled_payload()),
                 world: existing_world(scheduled_payload())
               )
    end
  end

  # ---------------------------------------------------------------------------
  # apply_ownership
  # ---------------------------------------------------------------------------

  describe "apply_ownership/3, app/delivery_execution.py:830-865" do
    test "a new execution writes the project child" do
      assert {:ok, effect} = Delivery.apply_ownership("d1", "p1", Canonical.empty_world())

      assert effect.project_child == %{
               entity_type: "delivery_execution",
               entity_id: "d1",
               project_id: "p1",
               deleted_at: nil
             }
    end

    test "an existing execution on the same project is written again" do
      owned =
        world(
          project_children: [
            %ProjectChild{entity_type: "delivery_execution", entity_id: "d1", project_id: "p1"}
          ]
        )

      assert {:ok, effect} = Delivery.apply_ownership("d1", "p1", owned)
      assert effect.project_child.project_id == "p1"
    end

    test "an execution may not move to another project" do
      owned =
        world(
          project_children: [
            %ProjectChild{entity_type: "delivery_execution", entity_id: "d1", project_id: "p1"}
          ]
        )

      assert {:error, "delivery execution Project ownership is immutable"} =
               Delivery.apply_ownership("d1", "p2", owned)
    end

    test "a tombstoned ownership row is not resurrected, S-139" do
      tombstoned =
        world(
          project_children: [
            %ProjectChild{
              entity_type: "delivery_execution",
              entity_id: "d1",
              project_id: "p1",
              deleted_at: @t0
            }
          ]
        )

      assert {:error, "delivery execution tombstoned ownership cannot be resurrected"} =
               Delivery.apply_ownership("d1", "p1", tombstoned)
    end
  end
end
