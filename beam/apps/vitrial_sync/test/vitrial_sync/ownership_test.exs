defmodule VitrialSync.OwnershipTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Canonical, Ownership}

  alias VitrialSync.Canonical.{
    Customer,
    Entity,
    Item,
    ItemChild,
    Membership,
    Organization,
    Principal,
    Project,
    ProjectChild,
    ProjectSector,
    Scope
  }

  # The reference's `EffectiveScope` comes from the principal, so the fixture
  # principal is organization-wide. `restricted_principal/3` is the other half of
  # the scope rules and is used only where the test is about scope.
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

  defp restricted_principal(capabilities, customers, projects) do
    %Principal{
      user_id: "u1",
      organization_id: "org",
      membership_id: "m1",
      session_id: "s1",
      authorization_revision: 4,
      all_customers: false,
      all_projects: false,
      customer_ids: MapSet.new(customers),
      project_ids: MapSet.new(projects),
      capabilities: MapSet.new(capabilities)
    }
  end

  defp world(attrs \\ []) do
    struct!(%Canonical.World{}, attrs)
  end

  defp chain do
    world(
      customers: [%Customer{customer_id: "c1"}],
      projects: [%Project{project_id: "p1", customer_id: "c1"}],
      project_sectors: [
        %ProjectSector{project_sector_id: "ps1", project_id: "p1", sector_id: "s1"}
      ],
      items: [%Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}]
    )
  end

  defp authorize(entity_type, entity_id, payload, principal, world, opts \\ []) do
    scope = Scope.from_principal(principal)

    Ownership.authorize(
      entity_type,
      entity_id,
      payload,
      opts[:deleted_at],
      principal,
      scope,
      Keyword.get(opts, :generic_exists, false),
      world
    )
  end

  defp blocker_event(id, overrides \\ %{}) do
    Map.merge(
      %{
        "id" => id,
        "blockerID" => "b1",
        "itemID" => "i1",
        "organizationID" => "org",
        "actorID" => "u1",
        "membershipID" => "m1",
        "sessionID" => "s1",
        "authorizationRevision" => 4
      },
      overrides
    )
  end

  defp quotation_event(id, overrides \\ %{}) do
    Map.merge(
      %{
        "id" => id,
        "kind" => "sent",
        "actorID" => "u1",
        "organizationID" => "org",
        "membershipID" => "m1",
        "sessionID" => "s1",
        "authorizationRevision" => 4
      },
      overrides
    )
  end

  # ---------------------------------------------------------------------------
  # dispatch
  # ---------------------------------------------------------------------------

  describe "the dispatch, app/ownership.py:357-796" do
    test "an unknown entity type falls off the end of the chain and denies the write" do
      # The reference's comment: "Falling through denies the write, which is the
      # direction to fail: if the dispatch in sync_service is ever broken,
      # delivery records are rejected rather than waved through a stale second
      # authority." A delivery_execution arriving here is SUSPECT-6, not a path.
      assert {:error, "canonical authorization is not implemented for delivery_execution"} =
               authorize("delivery_execution", "d1", %{}, principal([]), Canonical.empty_world())

      assert {:error, "canonical authorization is not implemented for widget"} =
               authorize("widget", "w1", %{}, principal([]), Canonical.empty_world())
    end

    test "the six item-child types each name their own capability" do
      for {type, capability} <- [
            {"measurement", "item.measurements.manage"},
            {"evidence", "item.evidence.manage"},
            {"customer_requirement", "item.requirements.manage"},
            {"configuration", "item.configuration.manage"},
            {"blocker", "item.blockers.manage"}
          ] do
        payload = %{"id" => "x1", "itemID" => "i1"}

        # Without the capability the type's own message fires...
        assert {:error, message} = authorize(type, "x1", payload, principal([]), chain())
        assert message == "#{capability} capability required"

        # ...and with it the write is authorized. A blocker additionally needs a
        # connected event history, which is asserted on its own below.
        if type == "blocker" do
          assert {:error, "connected blocker history requires events"} =
                   authorize(type, "x1", payload, principal([capability]), chain())
        else
          assert {:ok, plan} = authorize(type, "x1", payload, principal([capability]), chain())
          assert plan.entity_type == type
          assert plan.item_id == "i1"
          assert plan.is_create
        end
      end
    end
  end

  # ---------------------------------------------------------------------------
  # customer
  # ---------------------------------------------------------------------------

  describe "customer, app/ownership.py:369-390" do
    test "a create needs customer.create and yields a plan naming itself as the customer" do
      assert {:ok, plan} =
               authorize(
                 "customer",
                 "c1",
                 %{"id" => "c1"},
                 principal(["customer.create"]),
                 Canonical.empty_world()
               )

      assert plan.entity_type == "customer"
      assert plan.is_create
      assert plan.customer_id == "c1"
    end

    test "a create without customer.create is refused" do
      assert {:error, "customer.create capability required"} =
               authorize(
                 "customer",
                 "c1",
                 %{"id" => "c1"},
                 principal([]),
                 Canonical.empty_world()
               )
    end

    test "the payload id must match the entityID" do
      # `_require_identity/3`, app/ownership.py:111-116.
      assert {:error, "payload identity does not match canonical entityID"} =
               authorize(
                 "customer",
                 "c1",
                 %{"id" => "c2"},
                 principal(["customer.create"]),
                 Canonical.empty_world()
               )

      assert {:error, "payload identity does not match canonical entityID"} =
               authorize(
                 "customer",
                 "c1",
                 %{},
                 principal(["customer.create"]),
                 Canonical.empty_world()
               )
    end

    test "a tombstone may omit the id, because a V1 tombstone may carry an empty payload" do
      existing = world(customers: [%Customer{customer_id: "c1"}])

      assert {:ok, plan} =
               authorize("customer", "c1", %{}, principal(["customer.edit"]), existing,
                 deleted_at: "t"
               )

      assert plan.deleted_at == "t"
      refute plan.is_create
    end

    test "a tombstone of an unknown customer is refused, and a generic row is not a substitute" do
      # S-114: a generic SyncEntity row existing is not evidence of canonical
      # ownership, and a request to delete something canonical never had is
      # refused rather than allowed through the generic path.
      assert {:error, "cannot tombstone an unknown Customer"} =
               authorize(
                 "customer",
                 "c1",
                 %{},
                 principal(["customer.edit"]),
                 Canonical.empty_world(),
                 deleted_at: "t"
               )

      assert {:error, "existing Customer lacks canonical ownership"} =
               authorize(
                 "customer",
                 "c1",
                 %{"id" => "c1"},
                 principal(["customer.create"]),
                 Canonical.empty_world(),
                 generic_exists: true
               )
    end

    test "an update needs customer.edit and a customer in scope" do
      existing = world(customers: [%Customer{customer_id: "c1"}])

      assert {:ok, _plan} =
               authorize(
                 "customer",
                 "c1",
                 %{"id" => "c1"},
                 principal(["customer.edit"]),
                 existing
               )

      assert {:error, "customer.edit capability required"} =
               authorize("customer", "c1", %{"id" => "c1"}, principal([]), existing)
    end

    test "a customer outside the scope is refused" do
      existing = world(customers: [%Customer{customer_id: "c1"}])

      assert {:error, "Customer is outside authorized scope"} =
               authorize(
                 "customer",
                 "c1",
                 %{"id" => "c1"},
                 restricted_principal(["customer.edit"], ["other"], []),
                 existing
               )

      assert {:ok, _plan} =
               authorize(
                 "customer",
                 "c1",
                 %{"id" => "c1"},
                 restricted_principal(["customer.edit"], ["c1"], []),
                 existing
               )
    end

    test "a customer with live projects cannot be deleted" do
      blocked =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1"}]
        )

      assert {:error, "Customer cannot be deleted while Projects remain active"} =
               authorize("customer", "c1", %{}, principal(["customer.edit"]), blocked,
                 deleted_at: "t"
               )

      cleared =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1", deleted_at: "t"}]
        )

      assert {:ok, _plan} =
               authorize("customer", "c1", %{}, principal(["customer.edit"]), cleared,
                 deleted_at: "t"
               )
    end
  end

  # ---------------------------------------------------------------------------
  # project
  # ---------------------------------------------------------------------------

  describe "project, app/ownership.py:392-419" do
    test "a create needs project.create, an active customer, and the customer in scope" do
      customers = world(customers: [%Customer{customer_id: "c1"}])
      capable = principal(["project.create"])

      assert {:ok, plan} =
               authorize(
                 "project",
                 "p1",
                 %{"id" => "p1", "customerID" => "c1"},
                 capable,
                 customers
               )

      assert plan.project_id == "p1"
      assert plan.customer_id == "c1"

      assert {:error, "Project parent Customer is not canonical and active"} =
               authorize(
                 "project",
                 "p1",
                 %{"id" => "p1", "customerID" => "nope"},
                 capable,
                 customers
               )

      tombstoned = world(customers: [%Customer{customer_id: "c1", deleted_at: "t"}])

      assert {:error, "Project parent Customer is not canonical and active"} =
               authorize(
                 "project",
                 "p1",
                 %{"id" => "p1", "customerID" => "c1"},
                 capable,
                 tombstoned
               )

      assert {:error, "Project parent Customer is outside authorized scope"} =
               authorize(
                 "project",
                 "p1",
                 %{"id" => "p1", "customerID" => "c1"},
                 restricted_principal(["project.create"], [], []),
                 customers
               )
    end

    test "the customer link is immutable on update, and omitting it is allowed (S-106)" do
      projects =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1"}]
        )

      capable = principal(["project.edit"])

      # `supplied = payload.get("customerID"); if supplied is not None and
      # supplied != canonical`. Omitting the parent key is not a way to *change*
      # the parent; it is a way not to restate it.
      assert {:ok, _plan} = authorize("project", "p1", %{"id" => "p1"}, capable, projects)

      assert {:error, "Project Customer ownership is immutable"} =
               authorize(
                 "project",
                 "p1",
                 %{"id" => "p1", "customerID" => "c2"},
                 capable,
                 projects
               )
    end

    test "a project with a live sector, item or project child cannot be deleted" do
      capable = principal(["project.edit"])

      for extra <- [
            [project_sectors: [%ProjectSector{project_sector_id: "ps1", project_id: "p1"}]],
            [items: [%Item{item_id: "i1", project_id: "p1"}]],
            [
              project_children: [
                %ProjectChild{entity_type: "quotation", entity_id: "q1", project_id: "p1"}
              ]
            ],
            [
              project_children: [
                %ProjectChild{
                  entity_type: "delivery_execution",
                  entity_id: "d1",
                  project_id: "p1"
                }
              ]
            ]
          ] do
        blocked =
          struct!(
            world(
              customers: [%Customer{customer_id: "c1"}],
              projects: [%Project{project_id: "p1", customer_id: "c1"}]
            ),
            extra
          )

        assert {:error, "Project cannot be deleted while canonical children remain active"} =
                 authorize("project", "p1", %{}, capable, blocked, deleted_at: "t")
      end
    end

    test "tombstoned children no longer block the project delete" do
      cleared =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1"}],
          project_sectors: [
            %ProjectSector{project_sector_id: "ps1", project_id: "p1", deleted_at: "t"}
          ]
        )

      assert {:ok, _plan} =
               authorize("project", "p1", %{}, principal(["project.edit"]), cleared,
                 deleted_at: "t"
               )
    end
  end

  # ---------------------------------------------------------------------------
  # project_sector
  # ---------------------------------------------------------------------------

  describe "project_sector, app/ownership.py:421-455" do
    test "a create needs project.edit, a live project, and a sector id" do
      projects =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1"}]
        )

      capable = principal(["project.edit"])

      assert {:ok, plan} =
               authorize(
                 "project_sector",
                 "ps1",
                 %{"id" => "ps1", "projectID" => "p1", "sectorID" => "s1"},
                 capable,
                 projects
               )

      assert plan.project_id == "p1"
      assert plan.sector_id == "s1"
      assert plan.customer_id == "c1"

      assert {:error, "sectorID is required for canonical ownership"} =
               authorize(
                 "project_sector",
                 "ps1",
                 %{"id" => "ps1", "projectID" => "p1"},
                 capable,
                 projects
               )
    end

    test "both parents are immutable when supplied" do
      existing = chain()
      capable = principal(["project.edit"])

      assert {:error, "ProjectSector Project ownership is immutable"} =
               authorize(
                 "project_sector",
                 "ps1",
                 %{"id" => "ps1", "projectID" => "p9", "sectorID" => "s1"},
                 capable,
                 existing
               )

      assert {:error, "ProjectSector Sector identity is immutable"} =
               authorize(
                 "project_sector",
                 "ps1",
                 %{"id" => "ps1", "sectorID" => "s9"},
                 capable,
                 existing
               )
    end

    test "a sector with live items cannot be deleted" do
      assert {:error, "ProjectSector cannot be deleted while Items remain active"} =
               authorize(
                 "project_sector",
                 "ps1",
                 %{"id" => "ps1"},
                 principal(["project.edit"]),
                 chain(),
                 deleted_at: "t"
               )

      cleared = %{
        chain()
        | items: [
            %Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1", deleted_at: "t"}
          ]
      }

      assert {:ok, _plan} =
               authorize(
                 "project_sector",
                 "ps1",
                 %{"id" => "ps1"},
                 principal(["project.edit"]),
                 cleared,
                 deleted_at: "t"
               )
    end
  end

  # ---------------------------------------------------------------------------
  # item
  # ---------------------------------------------------------------------------

  describe "item, app/ownership.py:457-515" do
    test "a create needs item.create and a sector that belongs to the named project" do
      capable = principal(["item.create"])

      # "i2" is not canonical, so this is the create branch; "i1" already is.
      assert {:ok, plan} =
               authorize(
                 "item",
                 "i2",
                 %{"id" => "i2", "projectID" => "p1", "projectSectorID" => "ps1"},
                 capable,
                 chain()
               )

      assert plan.project_sector_id == "ps1"
      assert plan.project_id == "p1"
      assert plan.customer_id == "c1"

      # A sector that is canonical but hangs off another project.
      elsewhere =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1"}],
          project_sectors: [
            %ProjectSector{project_sector_id: "ps1", project_id: "p2", sector_id: "s1"}
          ]
        )

      assert {:error, "Item ProjectSector is not canonical for its Project"} =
               authorize(
                 "item",
                 "i1",
                 %{"id" => "i1", "projectID" => "p1", "projectSectorID" => "ps1"},
                 capable,
                 elsewhere
               )
    end

    test "an existing item needs a live sector, a live project, and a live item" do
      capable = principal(["item.edit"])

      no_sector =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1"}],
          items: [%Item{item_id: "i1", project_id: "p1", project_sector_id: nil}]
        )

      assert {:error, "existing Item lacks canonical ProjectSector ownership"} =
               authorize("item", "i1", %{"id" => "i1"}, capable, no_sector)

      # The sector row is gone, so the item's own link is dangling.
      no_sector_row =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [%Project{project_id: "p1", customer_id: "c1"}],
          items: [%Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}]
        )

      assert {:error, "Item canonical ProjectSector is missing or inactive"} =
               authorize("item", "i1", %{"id" => "i1"}, capable, no_sector_row)

      # The item row is gone, so the caller is asking about something that does
      # not exist: that is a create, and a create needs item.create.
      assert {:error, "cannot tombstone an unknown Item"} =
               authorize("item", "i1", %{}, capable, world(), deleted_at: "t")
    end

    test "both parents are immutable when supplied" do
      capable = principal(["item.edit"])

      assert {:error, "Item Project ownership is immutable"} =
               authorize("item", "i1", %{"id" => "i1", "projectID" => "p9"}, capable, chain())

      assert {:error, "Item ProjectSector ownership is immutable"} =
               authorize(
                 "item",
                 "i1",
                 %{"id" => "i1", "projectSectorID" => "ps9"},
                 capable,
                 chain()
               )
    end

    test "an item with a live sidecar cannot be deleted" do
      blocked = %{
        chain()
        | item_children: [%ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"}]
      }

      assert {:error, "Item cannot be deleted while sidecar/history records remain active"} =
               authorize("item", "i1", %{}, principal(["item.edit"]), blocked, deleted_at: "t")
    end

    test "S-112: the two delete guards are non-interchangeable" do
      # `_ensure_item_delete_safe` excludes `item_audit_event` on purpose, and
      # `_item_has_immutable_audit_history` covers only that type. Together they
      # cover the whole sidecar table; neither does alone.
      audit_world = %{
        chain()
        | item_children: [
            %ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"}
          ]
      }

      sidecar_world = %{
        chain()
        | item_children: [%ItemChild{entity_type: "measurement", entity_id: "m1", item_id: "i1"}]
      }

      assert Canonical.item_has_live_audit_history?(audit_world, "i1")
      refute Canonical.item_has_live_sidecars?(audit_world, "i1")

      refute Canonical.item_has_live_audit_history?(sidecar_world, "i1")
      assert Canonical.item_has_live_sidecars?(sidecar_world, "i1")
    end

    test "a tombstoned sidecar no longer blocks the item delete" do
      cleared = %{
        chain()
        | item_children: [
            %ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1", deleted_at: "t"}
          ]
      }

      assert {:ok, _plan} =
               authorize("item", "i1", %{}, principal(["item.edit"]), cleared, deleted_at: "t")
    end
  end

  # ---------------------------------------------------------------------------
  # append-only types
  # ---------------------------------------------------------------------------

  describe "append-only types, S-110" do
    test "item_audit_event can be created once and never again" do
      capable = principal(["item.create"])
      payload = %{"id" => "a1", "itemID" => "i1", "kind" => "created"}

      assert {:error, "item_audit_event history is append-only"} =
               authorize("item_audit_event", "a1", payload, capable, chain(),
                 generic_exists: true
               )

      existing = %{
        chain()
        | item_children: [
            %ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"}
          ]
      }

      assert {:error, "item_audit_event history is append-only"} =
               authorize("item_audit_event", "a1", payload, capable, existing)

      # S-110 says the history is written once, so there is no second write of
      # any kind -- including a tombstone.
      assert {:error, "item_audit_event history is append-only"} =
               authorize("item_audit_event", "a1", %{}, capable, existing, deleted_at: "t")
    end

    test "a configuration_version must be named {configurationID}#v{version} and sit on its base" do
      capable = principal(["item.configuration.manage"])

      nested = %{
        "configuration" => %{"itemID" => "i1", "id" => "c1", "version" => 1},
        "id" => "c1#v1"
      }

      # The base configuration must already be a canonical child of the same item.
      assert {:error, "configuration_version base configuration is not canonical"} =
               authorize("configuration_version", "c1#v1", nested, capable, chain())

      with_base = %{
        chain()
        | item_children: [
            %ItemChild{entity_type: "configuration", entity_id: "c1", item_id: "i1"}
          ]
      }

      assert {:ok, plan} =
               authorize("configuration_version", "c1#v1", nested, capable, with_base)

      assert plan.item_id == "i1"

      # The payload id matches, so the naming rule is what speaks: the version
      # inside the nested configuration is 1, so the canonical id would be c1#v1.
      renumbered = Map.put(nested, "id", "c1#v2")

      assert {:error, "configuration_version identity is invalid"} =
               authorize("configuration_version", "c1#v2", renumbered, capable, with_base)

      zero = %{
        "configuration" => %{"itemID" => "i1", "id" => "c1", "version" => 0},
        "id" => "c1#v0"
      }

      assert {:error, "configuration_version version is invalid"} =
               authorize("configuration_version", "c1#v0", zero, capable, with_base)

      # `version` is read as a Python int, so a string version is invalid.
      text = %{
        "configuration" => %{"itemID" => "i1", "id" => "c1", "version" => "1"},
        "id" => "c1#v1"
      }

      assert {:error, "configuration_version version is invalid"} =
               authorize("configuration_version", "c1#v1", text, capable, with_base)
    end
  end

  # ---------------------------------------------------------------------------
  # item audit events
  # ---------------------------------------------------------------------------

  describe "item_audit_event, S-109" do
    defp audit_payload(kind, overrides \\ %{}) do
      Map.merge(
        %{
          "id" => "a1",
          "itemID" => "i1",
          "kind" => kind,
          "actorID" => "u1",
          "organizationID" => "org",
          "membershipID" => "m1",
          "sessionID" => "s1",
          "authorizationRevision" => 4,
          "after" => %{"id" => "i1", "projectID" => "p1", "projectSectorID" => "ps1"}
        },
        overrides
      )
    end

    test "the capability is derived from the event kind" do
      both = principal(["item.create", "item.edit"])

      assert {:ok, _plan} =
               authorize("item_audit_event", "a1", audit_payload("created"), both, chain())

      assert {:ok, _plan} =
               authorize("item_audit_event", "a1", audit_payload("updated"), both, chain())

      assert {:error, "item.create capability required"} =
               authorize(
                 "item_audit_event",
                 "a1",
                 audit_payload("created"),
                 principal([]),
                 chain()
               )

      assert {:error, "item.edit capability required"} =
               authorize(
                 "item_audit_event",
                 "a1",
                 audit_payload("updated"),
                 principal(["item.create"]),
                 chain()
               )

      assert {:error, "item audit kind is invalid"} =
               authorize("item_audit_event", "a1", audit_payload("escalated"), both, chain())
    end

    test "a readiness override needs the second capability" do
      without = principal(["item.readiness.change"])
      with_both = principal(["item.readiness.change", "item.readiness.override"])
      payload = audit_payload("commercial_readiness_changed")

      assert {:ok, _plan} = authorize("item_audit_event", "a1", payload, without, chain())

      used = Map.put(payload, "readinessOverrideUsed", true)

      assert {:error, "item.readiness.override capability required"} =
               authorize("item_audit_event", "a1", used, without, chain())

      assert {:ok, _plan} = authorize("item_audit_event", "a1", used, with_both, chain())
    end

    test "the resulting and prior snapshots must be the audited item, in its project" do
      capable = principal(["item.create"])

      wrong_identity =
        audit_payload("created", %{
          "after" => %{"id" => "i2", "projectID" => "p1", "projectSectorID" => "ps1"}
        })

      assert {:error, "item audit resulting Item identity is invalid"} =
               authorize("item_audit_event", "a1", wrong_identity, capable, chain())

      wrong_parent =
        audit_payload("created", %{
          "after" => %{"id" => "i1", "projectID" => "p2", "projectSectorID" => "ps1"}
        })

      assert {:error, "item audit parent relationship is not canonical"} =
               authorize("item_audit_event", "a1", wrong_parent, capable, chain())

      with_before =
        audit_payload("created", %{
          "before" => %{"id" => "i1", "projectID" => "p2", "projectSectorID" => "ps1"}
        })

      assert {:error, "item audit prior parent relationship is not canonical"} =
               authorize("item_audit_event", "a1", with_before, capable, chain())
    end

    test "the whole payload must carry current provenance" do
      capable = principal(["item.create"])

      stale = audit_payload("created", %{"actorID" => "someone-else"})

      assert {:error, "actorID does not match authenticated provenance"} =
               authorize("item_audit_event", "a1", stale, capable, chain())

      bumped = audit_payload("created", %{"authorizationRevision" => 5})

      assert {:error, "authorizationRevision does not match authenticated provenance"} =
               authorize("item_audit_event", "a1", bumped, capable, chain())
    end
  end

  # ---------------------------------------------------------------------------
  # evidence references
  # ---------------------------------------------------------------------------

  describe "evidence references, S-107 / S-108" do
    defp with_evidence do
      %{
        chain()
        | item_children: [%ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"}]
      }
    end

    test "every referenced document must be a live evidence child of the same item" do
      capable = principal(["item.measurements.manage"])
      base = %{"id" => "m1", "itemID" => "i1"}

      assert {:ok, _plan} =
               authorize(
                 "measurement",
                 "m1",
                 Map.put(base, "evidenceReferences", ["e1"]),
                 capable,
                 with_evidence()
               )

      assert {:error, "evidence reference is not canonical for the same Item"} =
               authorize(
                 "measurement",
                 "m1",
                 Map.put(base, "evidenceReferences", ["nope"]),
                 capable,
                 with_evidence()
               )

      assert {:error, "invalid evidence reference"} =
               authorize(
                 "measurement",
                 "m1",
                 Map.put(base, "evidenceReferences", [""]),
                 capable,
                 with_evidence()
               )

      assert {:error, "invalid evidence reference"} =
               authorize(
                 "measurement",
                 "m1",
                 Map.put(base, "evidenceReferences", "e1"),
                 capable,
                 with_evidence()
               )

      # A live evidence child of a *different* item is not a reference for this one.
      elsewhere = %{
        chain()
        | item_children: [%ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i2"}]
      }

      assert {:error, "evidence reference is not canonical for the same Item"} =
               authorize(
                 "measurement",
                 "m1",
                 Map.put(base, "evidenceReferences", ["e1"]),
                 capable,
                 elsewhere
               )
    end

    test "a tombstoned evidence child is not a live one" do
      tombstoned = %{
        chain()
        | item_children: [
            %ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1", deleted_at: "t"}
          ]
      }

      assert {:error, "evidence reference is not canonical for the same Item"} =
               authorize(
                 "measurement",
                 "m1",
                 %{"id" => "m1", "itemID" => "i1", "evidenceReferences" => ["e1"]},
                 principal(["item.measurements.manage"]),
                 tombstoned
               )
    end

    test "customer_requirement uses evidenceReferenceIDs, with the same rule" do
      capable = principal(["item.requirements.manage"])
      base = %{"id" => "r1", "itemID" => "i1"}

      assert {:ok, _plan} =
               authorize(
                 "customer_requirement",
                 "r1",
                 Map.put(base, "evidenceReferenceIDs", ["e1"]),
                 capable,
                 with_evidence()
               )

      assert {:error, "evidence reference is not canonical for the same Item"} =
               authorize(
                 "customer_requirement",
                 "r1",
                 Map.put(base, "evidenceReferenceIDs", ["e2"]),
                 capable,
                 with_evidence()
               )
    end

    test "an evidence delete is refused while a live record still references it" do
      referencing =
        Map.put(with_evidence(), :entities, [
          %Entity{
            entity_type: "measurement",
            entity_id: "m1",
            payload: %{"evidenceReferences" => ["e1"]}
          }
        ])

      capable = principal(["item.evidence.manage"])

      assert {:error, "Evidence cannot be deleted while referenced"} =
               authorize("evidence", "e1", %{}, capable, referencing, deleted_at: "t")

      # The guard reads both keys, whichever table the reference sits in.
      by_requirement =
        Map.put(with_evidence(), :entities, [
          %Entity{
            entity_type: "customer_requirement",
            entity_id: "r1",
            payload: %{"evidenceReferenceIDs" => ["e1"]}
          }
        ])

      assert {:error, "Evidence cannot be deleted while referenced"} =
               authorize("evidence", "e1", %{}, capable, by_requirement, deleted_at: "t")

      # An upsert is not a tombstone, so it still has to carry its own id.
      assert {:ok, _plan} = authorize("evidence", "e1", %{"id" => "e1"}, capable, with_evidence())
    end

    test "a tombstoned referencing record does not hold the evidence open" do
      referencing =
        Map.put(with_evidence(), :entities, [
          %Entity{
            entity_type: "measurement",
            entity_id: "m1",
            payload: %{"evidenceReferences" => ["e1"]},
            deleted_at: "t"
          }
        ])

      assert {:ok, _plan} =
               authorize("evidence", "e1", %{}, principal(["item.evidence.manage"]), referencing,
                 deleted_at: "t"
               )
    end
  end

  # ---------------------------------------------------------------------------
  # configuration finalize/reopen
  # ---------------------------------------------------------------------------

  describe "configuration finalize/reopen" do
    defp configuration_world(status) do
      %{
        chain()
        | item_children: [
            %ItemChild{entity_type: "configuration", entity_id: "c1", item_id: "i1"}
          ],
          entities: [
            %Entity{
              entity_type: "configuration",
              entity_id: "c1",
              payload: %{"id" => "c1", "itemID" => "i1", "status" => status}
            }
          ]
      }
    end

    test "closing a configuration needs the finalize capability" do
      payload = %{"id" => "c1", "itemID" => "i1", "status" => "complete"}
      draft = configuration_world("draft")

      assert {:error, "item.configuration.finalize capability required"} =
               authorize(
                 "configuration",
                 "c1",
                 payload,
                 principal(["item.configuration.manage"]),
                 draft
               )

      assert {:ok, _plan} =
               authorize(
                 "configuration",
                 "c1",
                 payload,
                 principal(["item.configuration.manage", "item.configuration.finalize"]),
                 draft
               )
    end

    test "reopening a complete configuration needs the reopen capability" do
      payload = %{"id" => "c1", "itemID" => "i1", "status" => "draft"}
      complete = configuration_world("complete")

      assert {:error, "item.configuration.reopen capability required"} =
               authorize(
                 "configuration",
                 "c1",
                 payload,
                 principal(["item.configuration.manage"]),
                 complete
               )

      assert {:ok, _plan} =
               authorize(
                 "configuration",
                 "c1",
                 payload,
                 principal(["item.configuration.manage", "item.configuration.reopen"]),
                 complete
               )
    end

    test "a status that is neither complete nor transitioning to it asks for nothing" do
      payload = %{"id" => "c1", "itemID" => "i1", "status" => "draft"}

      assert {:ok, _plan} =
               authorize(
                 "configuration",
                 "c1",
                 payload,
                 principal(["item.configuration.manage"]),
                 configuration_world("blocked")
               )
    end
  end

  # ---------------------------------------------------------------------------
  # blocker connected history
  # ---------------------------------------------------------------------------

  describe "blocker connected history, app/ownership.py:632-651" do
    defp blocker_payload(events) do
      %{"id" => "b1", "itemID" => "i1", "events" => events}
    end

    test "a blocker needs a non-empty history and every event must name this blocker" do
      capable = principal(["item.blockers.manage"])

      assert {:error, "connected blocker history requires events"} =
               authorize("blocker", "b1", blocker_payload([]), capable, chain())

      assert {:ok, _plan} =
               authorize(
                 "blocker",
                 "b1",
                 blocker_payload([blocker_event("e1")]),
                 capable,
                 chain()
               )

      assert {:error, "blocker event identity is invalid"} =
               authorize(
                 "blocker",
                 "b1",
                 blocker_payload([blocker_event("e1", %{"blockerID" => "b2"})]),
                 capable,
                 chain()
               )

      assert {:error, "blocker event organization is invalid"} =
               authorize(
                 "blocker",
                 "b1",
                 blocker_payload([blocker_event("e1", %{"organizationID" => "other"})]),
                 capable,
                 chain()
               )

      assert {:error, "blocker event provenance is incomplete"} =
               authorize(
                 "blocker",
                 "b1",
                 blocker_payload([blocker_event("e1", %{"sessionID" => nil})]),
                 capable,
                 chain()
               )
    end

    test "only the appended tail is checked against the current principal (S-120)" do
      capable = principal(["item.blockers.manage"])

      # A historical event authored by someone else is history, not a forgery.
      history = [
        blocker_event("e1", %{"actorID" => "u0", "authorizationRevision" => 1}),
        blocker_event("e2")
      ]

      assert {:ok, _plan} = authorize("blocker", "b1", blocker_payload(history), capable, chain())

      stale_tail = [blocker_event("e1"), blocker_event("e2", %{"actorID" => "u0"})]

      assert {:error, "actorID does not match authenticated provenance"} =
               authorize("blocker", "b1", blocker_payload(stale_tail), capable, chain())
    end

    test "a tombstone skips the history check entirely" do
      existing = %{
        chain()
        | item_children: [%ItemChild{entity_type: "blocker", entity_id: "b1", item_id: "i1"}]
      }

      assert {:ok, _plan} =
               authorize("blocker", "b1", %{}, principal(["item.blockers.manage"]), existing,
                 deleted_at: "t"
               )
    end
  end

  # ---------------------------------------------------------------------------
  # quotation
  # ---------------------------------------------------------------------------

  describe "quotation, S-115 / S-116" do
    defp quotation_world do
      world(
        customers: [%Customer{customer_id: "c1"}],
        projects: [%Project{project_id: "p1", customer_id: "c1"}],
        project_sectors: [
          %ProjectSector{project_sector_id: "ps1", project_id: "p1", sector_id: "s1"}
        ],
        items: [%Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}]
      )
    end

    defp quote_payload(status, overrides \\ %{}) do
      Map.merge(
        %{
          "id" => "q1",
          "projectID" => "p1",
          "customerID" => "c1",
          "status" => status,
          "lines" => [%{"itemID" => "i1", "totalPrice" => 100}]
        },
        overrides
      )
    end

    test "the capability is chosen by status" do
      draft = principal(["quotation.create"])

      assert {:ok, _plan} =
               authorize("quotation", "q1", quote_payload("draft"), draft, quotation_world())

      assert {:ok, _plan} =
               authorize("quotation", "q1", quote_payload("ready"), draft, quotation_world())

      assert {:error, "quotation.send capability required"} =
               authorize("quotation", "q1", quote_payload("sent"), draft, quotation_world())

      approved =
        quote_payload("approved", %{"events" => [quotation_event("e1", %{"kind" => "approved"})]})

      assert {:ok, _plan} =
               authorize(
                 "quotation",
                 "q1",
                 approved,
                 principal(["quotation.approve"]),
                 quotation_world()
               )

      assert {:error, "Quotation status is invalid"} =
               authorize("quotation", "q1", quote_payload("cancelled"), draft, quotation_world())
    end

    test "a tombstone needs only quotation.create and skips the structural checks" do
      # S-115: a tombstone is not blocked by the content lock, and none of the
      # customer, line or provenance checks run for it.
      existing = %{
        quotation_world()
        | project_children: [
            %ProjectChild{entity_type: "quotation", entity_id: "q1", project_id: "p1"}
          ]
      }

      assert {:ok, plan} =
               authorize("quotation", "q1", %{}, principal(["quotation.create"]), existing,
                 deleted_at: "t"
               )

      assert plan.project_id == "p1"
      refute plan.is_create
    end

    test "the quotation's customer must equal the canonical project's" do
      assert {:error, "Quotation Customer does not match canonical Project"} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("draft", %{"customerID" => "c2"}),
                 principal(["quotation.create"]),
                 quotation_world()
               )
    end

    test "the project is immutable once the quotation is canonical" do
      existing = %{
        quotation_world()
        | project_children: [
            %ProjectChild{entity_type: "quotation", entity_id: "q1", project_id: "p1"}
          ]
      }

      assert {:error, "Quotation Project ownership is immutable"} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("draft", %{"projectID" => "p2"}),
                 principal(["quotation.create"]),
                 existing
               )
    end

    test "lines must be present and every line's item must be in the same project" do
      capable = principal(["quotation.create"])

      assert {:error, "Quotation must contain canonical Item lines"} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("draft", %{"lines" => []}),
                 capable,
                 quotation_world()
               )

      assert {:error, "Quotation line is malformed"} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("draft", %{"lines" => ["i1"]}),
                 capable,
                 quotation_world()
               )

      assert {:error, "itemID is required for canonical ownership"} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("draft", %{"lines" => [%{"totalPrice" => 1}]}),
                 capable,
                 quotation_world()
               )

      # A live Item, in a live Sector, but under a different Project.
      elsewhere =
        world(
          customers: [%Customer{customer_id: "c1"}],
          projects: [
            %Project{project_id: "p1", customer_id: "c1"},
            %Project{project_id: "p2", customer_id: "c1"}
          ],
          project_sectors: [
            %ProjectSector{project_sector_id: "ps1", project_id: "p2", sector_id: "s1"}
          ],
          items: [%Item{item_id: "i1", project_id: "p2", project_sector_id: "ps1"}]
        )

      assert {:error, "Quotation line Item belongs to another Project"} =
               authorize("quotation", "q1", quote_payload("draft"), capable, elsewhere)
    end

    test "a quotation whose project is not canonical is refused" do
      assert {:error, "Project is not canonical and active"} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("draft"),
                 principal(["quotation.create"]),
                 Canonical.empty_world()
               )
    end

    test "a status that has left draft needs an event history with current provenance" do
      capable = principal(["quotation.send"])

      assert {:error, "Quotation lifecycle transition requires provenance"} =
               authorize("quotation", "q1", quote_payload("sent"), capable, quotation_world())

      assert {:ok, _plan} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("sent", %{"events" => [quotation_event("e1")]}),
                 capable,
                 quotation_world()
               )

      assert {:error, "actorID does not match authenticated provenance"} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("sent", %{
                   "events" => [quotation_event("e1", %{"actorID" => "u0"})]
                 }),
                 capable,
                 quotation_world()
               )

      assert {:error, "Quotation event organization is invalid"} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("sent", %{
                   "events" => [quotation_event("e1", %{"organizationID" => "other"})]
                 }),
                 capable,
                 quotation_world()
               )

      # A draft needs none of it.
      assert {:ok, _plan} =
               authorize(
                 "quotation",
                 "q1",
                 quote_payload("draft"),
                 principal(["quotation.create"]),
                 quotation_world()
               )
    end
  end

  # ---------------------------------------------------------------------------
  # apply_plan
  # ---------------------------------------------------------------------------

  describe "apply_plan/3, app/ownership.py:765-901" do
    defp plan(attrs) do
      struct!(%Ownership.Plan{deleted_at: nil}, attrs)
    end

    defp grantable(attrs \\ []) do
      struct!(
        world(
          membership: %Membership{active: true, organization_id: "org", user_id: "u1"},
          organization: %Organization{authorization_revision: 4}
        ),
        attrs
      )
    end

    test "a created customer upserts the row and widens the creator's customer scope" do
      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "customer",
                   entity_id: "c1",
                   is_create: true,
                   customer_id: "c1"
                 }),
                 grantable(),
                 principal(["customer.create"])
               )

      assert effect.upserts == [{:customer, "c1", nil}]
      assert effect.scope_grant == %{customer_ids: ["c1"], project_ids: []}
      # The grant changed the membership, so the organization revision has to
      # move: that is what makes every in-flight mutation fail (S-121).
      assert effect.authorization_revision_bump?
    end

    test "a created project widens project scope only" do
      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "project",
                   entity_id: "p1",
                   is_create: true,
                   customer_id: "c1",
                   project_id: "p1"
                 }),
                 grantable(),
                 principal(["project.create"])
               )

      assert effect.scope_grant == %{customer_ids: [], project_ids: ["p1"]}
      assert effect.authorization_revision_bump?
    end

    test "a membership that is no longer canonical refuses the create" do
      revoked = world(organization: %Organization{authorization_revision: 4})

      creator =
        plan(%{entity_type: "customer", entity_id: "c1", is_create: true, customer_id: "c1"})

      assert {:error, "creator membership is not canonical and active"} =
               Ownership.apply_plan(creator, revoked, principal(["customer.create"]))

      inactive =
        grantable(membership: %Membership{active: false, organization_id: "org", user_id: "u1"})

      assert {:error, "creator membership is not canonical and active"} =
               Ownership.apply_plan(creator, inactive, principal(["customer.create"]))

      foreign =
        grantable(membership: %Membership{active: true, organization_id: "other", user_id: "u1"})

      assert {:error, "creator membership is not canonical and active"} =
               Ownership.apply_plan(creator, foreign, principal(["customer.create"]))
    end

    test "an all_customers membership needs no grant and no revision bump" do
      wide =
        grantable(
          membership: %Membership{
            active: true,
            organization_id: "org",
            user_id: "u1",
            all_customers: true
          }
        )

      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "customer",
                   entity_id: "c1",
                   is_create: true,
                   customer_id: "c1"
                 }),
                 wide,
                 principal(["customer.create"])
               )

      assert effect.scope_grant == %{customer_ids: [], project_ids: []}
      refute effect.authorization_revision_bump?
    end

    test "a customer the membership does not yet hold is granted" do
      held =
        grantable(
          membership: %Membership{
            active: true,
            organization_id: "org",
            user_id: "u1",
            customer_ids: ["c9"]
          }
        )

      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "customer",
                   entity_id: "c1",
                   is_create: true,
                   customer_id: "c1"
                 }),
                 held,
                 principal(["customer.create"])
               )

      assert effect.scope_grant == %{customer_ids: ["c1"], project_ids: []}
    end

    test "an existing customer takes only its deleted_at, and grants nothing" do
      existing = grantable(customers: [%Customer{customer_id: "c1"}])

      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "customer",
                   entity_id: "c1",
                   deleted_at: "t",
                   customer_id: "c1"
                 }),
                 existing,
                 principal(["customer.edit"])
               )

      assert effect.upserts == [{:customer, "c1", "t"}]
      assert effect.scope_grant == %{customer_ids: [], project_ids: []}
      refute effect.authorization_revision_bump?
    end

    test "a project whose canonical customer disagrees with the plan is refused" do
      existing = grantable(projects: [%Project{project_id: "p1", customer_id: "c1"}])

      assert {:error, "Project Customer ownership is immutable"} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "project",
                   entity_id: "p1",
                   customer_id: "c2",
                   project_id: "p1"
                 }),
                 existing,
                 principal(["project.edit"])
               )
    end

    test "parent links are re-checked at apply time, with their own wording (S-118)" do
      # The apply-time wording differs from the authorize-time wording on
      # purpose: the authorize-time check reads the client's payload, this one
      # reads the row as it is now.
      principal = principal(["item.edit"])

      moved =
        grantable(
          items: [%Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}],
          project_sectors: [
            %ProjectSector{project_sector_id: "ps1", project_id: "p1", sector_id: "s1"}
          ]
        )

      assert {:error, "Item canonical ownership is immutable"} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "item",
                   entity_id: "i1",
                   project_id: "p9",
                   project_sector_id: "ps1"
                 }),
                 moved,
                 principal
               )

      assert {:error, "ProjectSector ownership is immutable"} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "project_sector",
                   entity_id: "ps1",
                   project_id: "p1",
                   sector_id: "s9"
                 }),
                 moved,
                 principal
               )

      assert {:error, "Quotation Project ownership is immutable"} =
               Ownership.apply_plan(
                 plan(%{entity_type: "quotation", entity_id: "q1", project_id: "p9"}),
                 grantable(
                   project_children: [
                     %ProjectChild{entity_type: "quotation", entity_id: "q1", project_id: "p1"}
                   ]
                 ),
                 principal
               )

      assert {:error, "evidence Item ownership is immutable"} =
               Ownership.apply_plan(
                 plan(%{entity_type: "evidence", entity_id: "e1", item_id: "i2"}),
                 grantable(
                   item_children: [
                     %ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"}
                   ]
                 ),
                 principal
               )
    end

    test "a plan missing its parent is refused rather than written half-formed" do
      principal = principal(["item.edit"])

      assert {:error, "Item ownership plan is missing project_id or project_sector_id"} =
               Ownership.apply_plan(
                 plan(%{entity_type: "item", entity_id: "i1"}),
                 grantable(),
                 principal
               )

      assert {:error, "Item ownership plan is missing project_id or project_sector_id"} =
               Ownership.apply_plan(
                 plan(%{entity_type: "item", entity_id: "i1", project_id: "p1"}),
                 grantable(),
                 principal
               )

      assert {:error, "ProjectSector ownership plan is missing project_id or sector_id"} =
               Ownership.apply_plan(
                 plan(%{entity_type: "project_sector", entity_id: "ps1"}),
                 grantable(),
                 principal
               )

      assert {:error, "Project-child ownership plan is missing project_id"} =
               Ownership.apply_plan(
                 plan(%{entity_type: "quotation", entity_id: "q1"}),
                 grantable(),
                 principal
               )

      assert {:error, "Item-child ownership plan is missing item_id"} =
               Ownership.apply_plan(
                 plan(%{entity_type: "evidence", entity_id: "e1"}),
                 grantable(),
                 principal
               )
    end

    test "an unsupported plan type is refused" do
      assert {:error, "unsupported ownership plan \"widget\""} =
               Ownership.apply_plan(
                 plan(%{entity_type: "widget", entity_id: "w1"}),
                 grantable(),
                 principal([])
               )
    end

    test "each row keeps the tuple the writer needs, and the two tables stay apart" do
      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{entity_type: "quotation", entity_id: "q1", project_id: "p1"}),
                 grantable(),
                 principal([])
               )

      assert effect.upserts == [{:project_child, "quotation", "q1", "p1", nil}]

      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "evidence",
                   entity_id: "e1",
                   item_id: "i1",
                   deleted_at: "t"
                 }),
                 grantable(),
                 principal([])
               )

      assert effect.upserts == [{:item_child, "evidence", "e1", "i1", "t"}]

      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "project_sector",
                   entity_id: "ps1",
                   project_id: "p1",
                   sector_id: "s1"
                 }),
                 grantable(),
                 principal([])
               )

      assert effect.upserts == [{:project_sector, "ps1", "p1", "s1", nil}]

      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "project",
                   entity_id: "p1",
                   customer_id: "c1",
                   project_id: "p1"
                 }),
                 grantable(),
                 principal([])
               )

      assert effect.upserts == [{:project, "p1", "c1", nil}]
    end

    test "a project child that keeps its project is written again" do
      existing =
        grantable(
          project_children: [
            %ProjectChild{entity_type: "quotation", entity_id: "q1", project_id: "p1"}
          ]
        )

      assert {:ok, effect} =
               Ownership.apply_plan(
                 plan(%{
                   entity_type: "quotation",
                   entity_id: "q1",
                   project_id: "p1",
                   deleted_at: "t"
                 }),
                 existing,
                 principal([])
               )

      assert effect.upserts == [{:project_child, "quotation", "q1", "p1", "t"}]
    end
  end
end
