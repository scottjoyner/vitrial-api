defmodule VitrialSync.AuthorityTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Authority, Canonical, Ownership, Record}

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
    ProjectSector
  }

  @t0 "2026-01-01T00:00:00Z"

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

  defp record(attrs) do
    struct!(%Record{updated_at: @t0, payload: "{}"}, attrs)
  end

  defp world(attrs) do
    struct!(%Canonical.World{}, attrs)
  end

  # A world in which an item and its evidence child are fully canonical, so a
  # tombstone of that evidence would be authorized were it not referenced.
  defp item_world(attrs \\ []) do
    struct!(
      world(
        customers: [%Customer{customer_id: "c1"}],
        projects: [%Project{project_id: "p1", customer_id: "c1"}],
        project_sectors: [
          %ProjectSector{project_sector_id: "ps1", project_id: "p1", sector_id: "s1"}
        ],
        items: [%Item{item_id: "i1", project_id: "p1", project_sector_id: "ps1"}],
        item_children: [%ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"}],
        membership: %Membership{active: true, organization_id: "org", user_id: "u1"},
        organization: %Organization{authorization_revision: 4}
      ),
      attrs
    )
  end

  defp quotation_world(attrs \\ []) do
    struct!(
      world(
        customers: [%Customer{customer_id: "c1"}],
        projects: [%Project{project_id: "p1", customer_id: "c1"}],
        membership: %Membership{active: true, organization_id: "org", user_id: "u1"},
        organization: %Organization{authorization_revision: 4},
        entities: [
          %Entity{
            entity_type: "quotation",
            entity_id: "q1",
            payload: %{
              "id" => "q1",
              "status" => "approved",
              "projectID" => "p1",
              "customerID" => "c1",
              "number" => "Q-1",
              "currencyCode" => "EUR",
              "pricingVersion" => 3,
              "events" => [%{"kind" => "approved"}],
              "lines" => [%{"totalPrice" => "100.00"}]
            }
          }
        ]
      ),
      attrs
    )
  end

  defp delivery_payload do
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
      "events" => [
        %{
          "id" => "e1",
          "toStatus" => "engineeringReview",
          "occurredAt" => @t0,
          "actorID" => "u1",
          "organizationID" => "org",
          "membershipID" => "m1",
          "sessionID" => "s1",
          "authorizationRevision" => 4
        }
      ]
    }
  end

  # ---------------------------------------------------------------------------
  # step 5
  # ---------------------------------------------------------------------------

  describe "step 5: the immutable-audit-history item-delete guard, S-58" do
    test "an item tombstone with live audit history is refused" do
      guarded =
        item_world(
          item_children: [
            %ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"},
            %ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"}
          ]
        )

      assert {:rejected, "authorization_rejected"} =
               Authority.decide(
                 record(%{entity_type: "item", entity_id: "i1", deleted_at: @t0}),
                 %{},
                 nil,
                 principal(["item.edit"]),
                 guarded
               )
    end

    test "the guard, not the ownership path, is what refuses: the same tombstone passes without it" do
      assert {:ok, effects} =
               Authority.decide(
                 record(%{entity_type: "item", entity_id: "i1", deleted_at: @t0}),
                 %{},
                 nil,
                 principal(["item.edit"]),
                 item_world(item_children: [])
               )

      assert effects.authority == :generic
      assert effects.plan.entity_type == "item"
      assert effects.plan.deleted_at == @t0
    end

    test "a tombstoned audit event does not block the delete" do
      cleared =
        item_world(
          item_children: [
            %ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"},
            %ItemChild{
              entity_type: "item_audit_event",
              entity_id: "a1",
              item_id: "i1",
              deleted_at: @t0
            }
          ]
        )

      # The audit event is no longer live, but the evidence sidecar is, and that
      # is the ownership guard's half of S-112.
      assert {:rejected, "authorization_rejected"} =
               Authority.decide(
                 record(%{entity_type: "item", entity_id: "i1", deleted_at: @t0}),
                 %{},
                 nil,
                 principal(["item.edit"]),
                 cleared
               )

      only_audit =
        item_world(
          item_children: [
            %ItemChild{
              entity_type: "item_audit_event",
              entity_id: "a1",
              item_id: "i1",
              deleted_at: @t0
            }
          ]
        )

      assert {:ok, _effects} =
               Authority.decide(
                 record(%{entity_type: "item", entity_id: "i1", deleted_at: @t0}),
                 %{},
                 nil,
                 principal(["item.edit"]),
                 only_audit
               )
    end

    test "the guard only ever looks at item tombstones" do
      guarded =
        item_world(
          item_children: [
            %ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"}
          ]
        )

      assert {:rejected, "authorization_rejected"} =
               Authority.decide(
                 record(%{entity_type: "measurement", entity_id: "m1", deleted_at: @t0}),
                 %{"itemID" => "i1"},
                 nil,
                 principal([]),
                 guarded
               )
    end
  end

  # ---------------------------------------------------------------------------
  # step 6
  # ---------------------------------------------------------------------------

  describe "step 6: lifecycle, S-59" do
    test "a blocker payload that fails the state machine is lifecycle_rejected" do
      # No blocker capability, so step 7 would also refuse -- but it does not get
      # there, and the code says which step spoke.
      assert {:rejected, "lifecycle_rejected"} =
               Authority.decide(
                 record(%{entity_type: "blocker", entity_id: "b1"}),
                 %{"status" => "open", "description" => "   "},
                 nil,
                 principal([]),
                 item_world()
               )
    end

    test "delivery_execution skips step 6 entirely, S-59" do
      # The same payload shape that would fail a blocker is simply not examined
      # for a delivery record; the delivery gate speaks instead, in its own code.
      assert {:rejected, "delivery_rejected"} =
               Authority.decide(
                 record(%{entity_type: "delivery_execution", entity_id: "d1"}),
                 %{"status" => "not-a-status"},
                 nil,
                 principal([]),
                 quotation_world()
               )
    end

    test "a tombstone skips step 6 for every type" do
      # A blocker with a body that would fail the state machine commits on to the
      # ownership check when it is a tombstone -- and the code it gets back is
      # the authorization one, because step 6 never ran.
      assert {:rejected, "authorization_rejected"} =
               Authority.decide(
                 record(%{entity_type: "blocker", entity_id: "b1", deleted_at: @t0}),
                 %{},
                 nil,
                 principal([]),
                 item_world()
               )
    end
  end

  # ---------------------------------------------------------------------------
  # step 7
  # ---------------------------------------------------------------------------

  describe "step 7a: delivery authority, S-60" do
    test "a delivery record is refused with delivery_rejected, never authorization_rejected" do
      assert {:rejected, "delivery_rejected"} =
               Authority.decide(
                 record(%{entity_type: "delivery_execution", entity_id: "d1"}),
                 delivery_payload(),
                 nil,
                 principal([]),
                 quotation_world()
               )
    end

    test "a delivery tombstone is refused at the gate, as S-137 says" do
      assert {:rejected, "delivery_rejected"} =
               Authority.decide(
                 record(%{entity_type: "delivery_execution", entity_id: "d1", deleted_at: @t0}),
                 delivery_payload(),
                 nil,
                 principal(["delivery.manage"]),
                 quotation_world()
               )
    end

    test "an authorized delivery yields delivery effects and no ownership plan" do
      assert {:ok, effects} =
               Authority.decide(
                 record(%{entity_type: "delivery_execution", entity_id: "d1"}),
                 delivery_payload(),
                 nil,
                 principal(["delivery.manage"]),
                 quotation_world()
               )

      assert effects.authority == :delivery
      assert effects.plan == nil
      assert effects.ownership.project_child.project_id == "p1"
      assert effects.evidence_gc == nil
    end
  end

  describe "step 7b: generic record authorization and its ownership plan" do
    test "a create with the capability yields a plan and the rows it would write" do
      assert {:ok, effects} =
               Authority.decide(
                 record(%{entity_type: "customer", entity_id: "c2"}),
                 %{"id" => "c2"},
                 nil,
                 principal(["customer.create"]),
                 item_world()
               )

      assert effects.authority == :generic
      assert effects.plan.entity_type == "customer"
      assert effects.plan.is_create
      assert effects.ownership.upserts == [{:customer, "c2", nil}]
      assert effects.ownership.scope_grant == %{customer_ids: ["c2"], project_ids: []}
      assert effects.evidence_gc == nil
    end

    test "a create without the capability is authorization_rejected" do
      assert {:rejected, "authorization_rejected"} =
               Authority.decide(
                 record(%{entity_type: "customer", entity_id: "c2"}),
                 %{"id" => "c2"},
                 nil,
                 principal([]),
                 item_world()
               )
    end

    test "an unknown type falls off the end of the dispatch and is refused" do
      assert {:rejected, "authorization_rejected"} =
               Authority.decide(
                 record(%{entity_type: "widget", entity_id: "w1"}),
                 %{},
                 nil,
                 principal([]),
                 item_world()
               )
    end
  end

  # ---------------------------------------------------------------------------
  # step 8
  # ---------------------------------------------------------------------------

  describe "step 8: evidence GC, S-62" do
    defp evidence_tombstone,
      do: record(%{entity_type: "evidence", entity_id: "e1", deleted_at: @t0})

    test "an evidence tombstone with a blob row enqueues the GC row" do
      with_blob =
        item_world(
          evidence_blobs: [
            %EvidenceBlob{document_id: "e1", storage_provider: "s3", object_key: "org/e1.pdf"}
          ]
        )

      assert {:ok, effects} =
               Authority.decide(
                 evidence_tombstone(),
                 %{},
                 nil,
                 principal(["item.evidence.manage"]),
                 with_blob
               )

      assert effects.evidence_gc == %{
               document_id: "e1",
               storage_provider: "s3",
               object_key: "org/e1.pdf",
               reason: "metadata_tombstone",
               grace_seconds: 86_400
             }
    end

    test "an evidence tombstone with no blob row commits with no GC row" do
      assert {:ok, effects} =
               Authority.decide(
                 evidence_tombstone(),
                 %{},
                 nil,
                 principal(["item.evidence.manage"]),
                 item_world()
               )

      assert effects.evidence_gc == nil
      assert effects.ownership.upserts == [{:item_child, "evidence", "e1", "i1", @t0}]
    end

    test "an evidence upsert enqueues nothing" do
      with_blob =
        item_world(
          evidence_blobs: [
            %EvidenceBlob{document_id: "e1", storage_provider: "s3", object_key: "org/e1.pdf"}
          ]
        )

      assert {:ok, effects} =
               Authority.decide(
                 record(%{entity_type: "evidence", entity_id: "e1"}),
                 %{"id" => "e1", "itemID" => "i1"},
                 nil,
                 principal(["item.evidence.manage"]),
                 with_blob
               )

      assert effects.evidence_gc == nil
    end

    test "a refused authorization never reaches the GC step" do
      # The reference enqueues in the same transaction as the tombstone, and only
      # after `authorize_record` has returned. A refused delete of a referenced
      # document must not queue physical deletion of an object whose metadata was
      # not tombstoned.
      referenced =
        item_world(
          evidence_blobs: [
            %EvidenceBlob{document_id: "e1", storage_provider: "s3", object_key: "org/e1.pdf"}
          ],
          entities: [
            %Entity{
              entity_type: "measurement",
              entity_id: "m1",
              payload: %{"evidenceReferences" => ["e1"]}
            }
          ]
        )

      assert {:rejected, "authorization_rejected"} =
               Authority.decide(
                 evidence_tombstone(),
                 %{},
                 nil,
                 principal(["item.evidence.manage"]),
                 referenced
               )
    end

    test "the GC step cannot reject anything on its own" do
      # Every path into step 8 has already been authorized, so there is no input
      # that produces a rejection here -- which is why `decide/3` has no error arm.
      assert {:ok, tombstone} =
               Authority.decide(
                 evidence_tombstone(),
                 %{},
                 nil,
                 principal(["item.evidence.manage"]),
                 item_world()
               )

      assert {:ok, upsert} =
               Authority.decide(
                 record(%{entity_type: "evidence", entity_id: "e1"}),
                 %{"id" => "e1", "itemID" => "i1"},
                 nil,
                 principal(["item.evidence.manage"]),
                 item_world()
               )

      assert is_nil(tombstone.evidence_gc)
      assert is_nil(upsert.evidence_gc)
    end
  end

  describe "the returned effects are the whole transaction's work" do
    test "a batch can create a parent and then a child that depends on it" do
      # The scope grant is what makes the second record authorizable against the
      # scope the first one widened; that is why it is in the effects rather than
      # folded into the plan.
      assert {:ok, first} =
               Authority.decide(
                 record(%{entity_type: "customer", entity_id: "c2"}),
                 %{"id" => "c2"},
                 nil,
                 principal(["customer.create"]),
                 item_world()
               )

      assert first.ownership.scope_grant.customer_ids == ["c2"]
      assert first.ownership.authorization_revision_bump?
    end

    test "the authority kind is the only difference between the two step-7 paths" do
      generic =
        Authority.decide(
          record(%{entity_type: "customer", entity_id: "c2"}),
          %{"id" => "c2"},
          nil,
          principal(["customer.create"]),
          item_world()
        )

      delivery =
        Authority.decide(
          record(%{entity_type: "delivery_execution", entity_id: "d1"}),
          delivery_payload(),
          nil,
          principal(["delivery.manage"]),
          quotation_world()
        )

      assert {:ok, %{authority: :generic, plan: %Ownership.Plan{}}} = generic
      assert {:ok, %{authority: :delivery, plan: nil}} = delivery
    end
  end
end
