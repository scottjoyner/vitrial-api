defmodule VitrialSync.EntityOrderTest do
  use ExUnit.Case, async: true

  alias VitrialSync.EntityOrder

  describe "order_for/1" do
    test "the canonical parent-before-child order" do
      assert EntityOrder.order_for("customer") == 0
      assert EntityOrder.order_for("project") == 1
      assert EntityOrder.order_for("project_sector") == 2
      assert EntityOrder.order_for("item") == 3
      assert EntityOrder.order_for("quotation") == 8
      assert EntityOrder.order_for("delivery_execution") == 9
    end

    test "the four order-4 types are siblings, not a chain" do
      for type <- ["item_audit_event", "evidence", "measurement", "blocker"] do
        assert EntityOrder.order_for(type) == 4
      end
    end

    test "an unknown type sorts last rather than being rejected" do
      # ENTITY_ORDER.get(entity_type, 100) -- an unrecognised type is still
      # processed, just after everything the server knows about.
      assert EntityOrder.order_for("something_new") == 100
      assert EntityOrder.order_for("") == 100
    end

    test "known_order_for/1 distinguishes known from unknown" do
      assert EntityOrder.known_order_for("item") == {:ok, 3}
      assert EntityOrder.known_order_for("something_new") == {:error, :unknown_entity_type}
    end
  end

  describe "entity_types/0" do
    test "covers exactly the EntityType literal in app/schemas.py" do
      # Thirteen types. A missing one is a type the sync engine can never order,
      # and an extra one is a type Pydantic would have rejected at the boundary.
      assert length(EntityOrder.entity_types()) == 13

      for type <- ~w(customer project project_sector item item_audit_event measurement evidence
                     customer_requirement configuration configuration_version blocker quotation
                     delivery_execution) do
        assert type in EntityOrder.entity_types(), "#{type} is missing from the order table"
      end
    end

    test "is returned in processing order" do
      assert hd(EntityOrder.entity_types()) == "customer"
      assert List.last(EntityOrder.entity_types()) == "delivery_execution"
    end
  end

  describe "sort/1" do
    test "reorders a child that arrived before its parent" do
      # S-54: a client may send the whole chain in any order.
      sorted =
        EntityOrder.sort([
          {0, %{"entityType" => "item"}},
          {1, %{"entityType" => "project_sector"}},
          {2, %{"entityType" => "project"}}
        ])

      assert Enum.map(sorted, fn {index, _} -> index end) == [2, 1, 0]
    end

    test "keeps the client's own order within one entity type" do
      # S-53. The index component of the sort key is load-bearing: dropping it
      # would reorder sibling records, and the second would be rejected for a
      # stale revision against the first.
      sorted =
        EntityOrder.sort([
          {0, %{"entityType" => "item", "entityID" => "a"}},
          {1, %{"entityType" => "item", "entityID" => "b"}},
          {2, %{"entityType" => "item", "entityID" => "c"}}
        ])

      assert Enum.map(sorted, fn {index, _} -> index end) == [0, 1, 2]
    end

    test "sibling types stay in the client's order too" do
      sorted =
        EntityOrder.sort([
          {0, %{"entityType" => "evidence"}},
          {1, %{"entityType" => "item_audit_event"}}
        ])

      assert Enum.map(sorted, fn {index, _} -> index end) == [0, 1]
    end

    test "an unknown type sorts after every known one" do
      sorted =
        EntityOrder.sort([
          {0, %{"entityType" => "mystery"}},
          {1, %{"entityType" => "customer"}}
        ])

      assert Enum.map(sorted, fn {index, _} -> index end) == [1, 0]
    end

    test "an empty batch sorts to an empty batch" do
      assert EntityOrder.sort([]) == []
    end

    test "sorting is stable across a shuffled input" do
      pairs = [
        {0, %{"entityType" => "item_audit_event"}},
        {1, %{"entityType" => "customer"}},
        {2, %{"entityType" => "evidence"}},
        {3, %{"entityType" => "quotation"}},
        {4, %{"entityType" => "project"}}
      ]

      expected = EntityOrder.sort(pairs)

      assert Enum.sort(Enum.shuffle(pairs)) |> EntityOrder.sort() == expected
    end
  end

  describe "sort_records/1" do
    test "orders structs the same way as wire maps" do
      {:ok, item} =
        VitrialSync.Record.new(%{
          id: "a",
          entity_type: "item",
          entity_id: "i1",
          updated_at: "2026-10-02T00:00:00Z",
          payload: "{}"
        })

      {:ok, project} =
        VitrialSync.Record.new(%{
          id: "b",
          entity_type: "project",
          entity_id: "p1",
          updated_at: "2026-10-02T00:00:00Z",
          payload: "{}"
        })

      assert [{1, ^project}, {0, ^item}] = EntityOrder.sort_records([{0, item}, {1, project}])
    end
  end
end
