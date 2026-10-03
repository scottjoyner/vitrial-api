defmodule VitrialSync.CanonicalTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Canonical
  alias VitrialSync.Canonical.{ItemChild, Membership, Organization, Principal, Scope}

  defp principal(attrs \\ []) do
    struct!(
      %Principal{
        user_id: "u1",
        organization_id: "org",
        membership_id: "m1",
        session_id: "s1",
        authorization_revision: 3
      },
      attrs
    )
  end

  describe "Scope, app/ownership.py:30-55" do
    test "can_access_customer? is all_customers or membership" do
      all = %Scope{all_customers: true, customer_ids: MapSet.new()}
      assert Scope.can_access_customer?(all, "c1")

      some = %Scope{customer_ids: MapSet.new(["c1"])}
      assert Scope.can_access_customer?(some, "c1")
      refute Scope.can_access_customer?(some, "c2")
      refute Scope.can_access_customer?(%Scope{}, "c1")
    end

    test "can_access_project? needs the customer AND the project" do
      scope = %Scope{customer_ids: MapSet.new(["c1"]), project_ids: MapSet.new(["p1"])}

      assert Scope.can_access_project?(scope, "p1", "c1")
      # Customer in scope, project not: refused. This is the case a
      # `can_access_customer?` check alone would wave through.
      refute Scope.can_access_project?(scope, "p2", "c1")
      # Project in scope, customer not: also refused.
      refute Scope.can_access_project?(scope, "p1", "c2")
    end

    test "all_projects does not bypass the customer check" do
      scope = %Scope{all_projects: true, customer_ids: MapSet.new(["c1"])}

      assert Scope.can_access_project?(scope, "p1", "c1")
      refute Scope.can_access_project?(scope, "p1", "c2")
    end

    test "from_principal copies the principal's own scope fields" do
      scope =
        Scope.from_principal(principal(all_customers: true, customer_ids: MapSet.new(["c1"])))

      assert scope.all_customers
      assert scope.customer_ids == MapSet.new(["c1"])
      assert scope.all_projects == false
    end
  end

  describe "payload_object/1" do
    test "an object payload is returned, and anything else reads as absent" do
      object = %{"a" => 1}
      entity = %Canonical.Entity{entity_type: "item", entity_id: "i1", payload: object}

      assert Canonical.payload_object(entity) == object
      assert Canonical.payload_object(%Canonical.Entity{payload: nil}) == nil
      assert Canonical.payload_object(%Canonical.Entity{payload: [1, 2]}) == nil
      assert Canonical.payload_object(%Canonical.Entity{payload: "text"}) == nil
      assert Canonical.payload_object(nil) == nil
    end
  end

  describe "active_item_child/3 -- _active_item_child, app/ownership.py:215-225" do
    test "present and live returns the row; tombstoned and absent both return nil" do
      live = %ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"}
      dead = %ItemChild{entity_type: "evidence", entity_id: "e2", item_id: "i1", deleted_at: "t"}

      world = %Canonical.World{item_children: [live, dead]}

      assert Canonical.active_item_child(world, "evidence", "e1") == live
      assert Canonical.active_item_child(world, "evidence", "e2") == nil
      assert Canonical.active_item_child(world, "evidence", "e3") == nil
    end
  end

  describe "the two item-delete predicates, S-112" do
    setup do
      %{
        audit: %ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"},
        sidecar: %ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"},
        other_item: %ItemChild{entity_type: "evidence", entity_id: "e2", item_id: "i2"}
      }
    end

    test "each predicate looks only at its own half of the sidecar table", %{
      audit: audit,
      sidecar: sidecar
    } do
      world = %Canonical.World{item_children: [audit]}

      assert Canonical.item_has_live_audit_history?(world, "i1")
      # The audit event is explicitly excluded from the sidecar guard: audit
      # history is allowed to outlive the item, the other half of S-112.
      refute Canonical.item_has_live_sidecars?(world, "i1")

      world = %Canonical.World{item_children: [sidecar]}
      refute Canonical.item_has_live_audit_history?(world, "i1")
      assert Canonical.item_has_live_sidecars?(world, "i1")
    end

    test "a tombstoned row blocks nothing, and another item's rows are not this item's", %{
      audit: audit,
      sidecar: sidecar,
      other_item: other
    } do
      world = %Canonical.World{
        item_children: [%{audit | deleted_at: "t"}, %{sidecar | deleted_at: "t"}, other]
      }

      refute Canonical.item_has_live_audit_history?(world, "i1")
      refute Canonical.item_has_live_sidecars?(world, "i1")
      assert Canonical.item_has_live_sidecars?(world, "i2")
    end
  end

  describe "customer_has_live_projects?/2 -- _ensure_customer_delete_safe" do
    test "only a live project blocks" do
      live = %Canonical.Project{project_id: "p1", customer_id: "c1"}
      dead = %Canonical.Project{project_id: "p2", customer_id: "c1", deleted_at: "t"}
      other = %Canonical.Project{project_id: "p3", customer_id: "c2"}

      assert Canonical.customer_has_live_projects?(%Canonical.World{projects: [live]}, "c1")
      refute Canonical.customer_has_live_projects?(%Canonical.World{projects: [dead]}, "c1")
      refute Canonical.customer_has_live_projects?(%Canonical.World{projects: [other]}, "c1")
      refute Canonical.customer_has_live_projects?(Canonical.empty_world(), "c1")
    end
  end

  describe "project_has_live_children?/2 -- _ensure_project_delete_safe" do
    test "a sector, an item or a project child of any type all block" do
      sector = %Canonical.ProjectSector{
        project_sector_id: "ps1",
        project_id: "p1",
        sector_id: "s1"
      }

      item = %Canonical.Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}
      child = %Canonical.ProjectChild{entity_type: "quotation", entity_id: "q1", project_id: "p1"}

      assert Canonical.project_has_live_children?(
               %Canonical.World{project_sectors: [sector]},
               "p1"
             )

      assert Canonical.project_has_live_children?(%Canonical.World{items: [item]}, "p1")

      assert Canonical.project_has_live_children?(
               %Canonical.World{project_children: [child]},
               "p1"
             )

      refute Canonical.project_has_live_children?(
               %Canonical.World{project_children: [%{child | deleted_at: "t"}]},
               "p1"
             )
    end
  end

  describe "project_sector_has_live_items?/2 -- _ensure_project_sector_delete_safe" do
    test "only a live item in that sector blocks" do
      item = %Canonical.Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}
      other = %Canonical.Item{item_id: "i2", project_id: "p1", project_sector_id: "ps2"}

      assert Canonical.project_sector_has_live_items?(%Canonical.World{items: [item]}, "ps1")
      refute Canonical.project_sector_has_live_items?(%Canonical.World{items: [other]}, "ps1")
    end
  end

  describe "evidence_referenced?/2 -- _ensure_evidence_delete_safe, app/ownership.py:337-358" do
    defp measurement(attrs, payload) do
      %Canonical.Entity{
        entity_type: attrs[:type] || "measurement",
        entity_id: attrs[:id] || "m1",
        payload: payload,
        deleted_at: attrs[:deleted_at]
      }
    end

    test "both reference keys are searched" do
      references = measurement(%{}, %{"evidenceReferences" => ["e1"]})

      ids =
        measurement(%{type: "customer_requirement", id: "r1"}, %{"evidenceReferenceIDs" => ["e1"]})

      assert Canonical.evidence_referenced?(%Canonical.World{entities: [references]}, "e1")
      assert Canonical.evidence_referenced?(%Canonical.World{entities: [ids]}, "e1")
      refute Canonical.evidence_referenced?(%Canonical.World{entities: [references]}, "e2")
    end

    test "only measurement and customer_requirement are searched" do
      other = measurement(%{type: "evidence", id: "e9"}, %{"evidenceReferences" => ["e1"]})

      refute Canonical.evidence_referenced?(%Canonical.World{entities: [other]}, "e1")
    end

    test "a tombstoned referrer does not block, because the filter is deleted_at IS NULL" do
      dead = measurement(%{deleted_at: "t"}, %{"evidenceReferences" => ["e1"]})

      refute Canonical.evidence_referenced?(%Canonical.World{entities: [dead]}, "e1")
    end

    test "a missing or non-list reference key is not a reference" do
      for payload <- [%{}, %{"evidenceReferences" => nil}, %{"evidenceReferences" => "e1"}] do
        refute Canonical.evidence_referenced?(
                 %Canonical.World{entities: [measurement(%{}, payload)]},
                 "e1"
               ),
               "payload=#{inspect(payload)}"
      end
    end

    test "the containment is jsonb, so a number never matches a string id" do
      numeric = measurement(%{}, %{"evidenceReferences" => [1]})

      refute Canonical.evidence_referenced?(%Canonical.World{entities: [numeric]}, "1")
    end

    test "the reference filters by evidence id alone, not by item" do
      # The reference takes `item_id` as a parameter and does not use it; the
      # filter is organization-wide. An evidence id names one document, so a
      # stricter filter would refuse a delete the server permits.
      elsewhere = measurement(%{}, %{"evidenceReferences" => ["e1"]})

      assert Canonical.evidence_referenced?(%Canonical.World{entities: [elsewhere]}, "e1")
    end
  end

  describe "capable?/2 -- _require_capability, app/ownership.py:120-122" do
    test "membership of the capability set is the whole check" do
      with_create = principal(capabilities: MapSet.new(["customer.create"]))

      assert Canonical.capable?(with_create, "customer.create")
      refute Canonical.capable?(with_create, "customer.edit")
      refute Canonical.capable?(principal(), "customer.create")
    end
  end

  describe "world lookups" do
    test "each keyed lookup returns the row or nil" do
      world = %Canonical.World{
        customers: [%Canonical.Customer{customer_id: "c1"}],
        projects: [%Canonical.Project{project_id: "p1", customer_id: "c1"}],
        project_sectors: [%Canonical.ProjectSector{project_sector_id: "ps1", project_id: "p1"}],
        items: [%Canonical.Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}],
        project_children: [%Canonical.ProjectChild{entity_type: "quotation", entity_id: "q1"}],
        evidence_blobs: [%Canonical.EvidenceBlob{document_id: "e1"}]
      }

      assert Canonical.customer(world, "c1").customer_id == "c1"
      assert Canonical.customer(world, "c2") == nil
      assert Canonical.project(world, "p1").customer_id == "c1"
      assert Canonical.project_sector(world, "ps1").project_id == "p1"
      assert Canonical.item(world, "i1").project_id == "p1"
      assert Canonical.project_child(world, "quotation", "q1").project_id == nil
      assert Canonical.project_child(world, "delivery_execution", "q1") == nil
      assert Canonical.evidence_blob(world, "e1").document_id == "e1"
    end
  end

  describe "Membership and Organization" do
    test "they exist so that _grant_created_scope has something to read" do
      membership = %Membership{active: true, organization_id: "org", user_id: "u1"}
      organization = %Organization{authorization_revision: 3}

      assert membership.active
      assert organization.authorization_revision == 3
    end
  end
end
