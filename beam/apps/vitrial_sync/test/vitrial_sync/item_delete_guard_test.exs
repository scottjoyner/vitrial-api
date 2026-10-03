defmodule VitrialSync.ItemDeleteGuardTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Canonical, ItemDeleteGuard, Record}
  alias VitrialSync.Canonical.ItemChild

  defp record(attrs \\ []) do
    struct!(%Record{id: "r1", entity_type: "item", entity_id: "i1", updated_at: "t"}, attrs)
  end

  defp audit(attrs \\ []) do
    struct!(%ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"}, attrs)
  end

  describe "S-58: the guard's four conditions" do
    test "an item tombstone with a live audit event is refused" do
      world = %Canonical.World{item_children: [audit()]}

      assert ItemDeleteGuard.decide(record(deleted_at: "2026-10-02T00:00:00Z"), world) ==
               {:rejected, "authorization_rejected"}
    end

    test "an upsert is never refused, whatever the audit history looks like" do
      # `record.deletedAt is not None` is a conjunct, so a live audit event on an
      # upsert changes nothing. An audit event is written by a separate record.
      world = %Canonical.World{item_children: [audit()]}

      assert ItemDeleteGuard.decide(record(), world) == :ok
    end

    test "a tombstone of any other entity type is never refused" do
      world = %Canonical.World{item_children: [audit()]}

      for type <- ["customer", "project", "project_sector", "blocker", "quotation", "evidence"] do
        record = %Record{
          id: "r1",
          entity_type: type,
          entity_id: "i1",
          updated_at: "t",
          deleted_at: "t"
        }

        assert ItemDeleteGuard.decide(record, world) == :ok, type
      end
    end

    test "a tombstoned audit event does not block, because the filter is deleted_at IS NULL" do
      world = %Canonical.World{item_children: [audit(%{deleted_at: "t"})]}

      assert ItemDeleteGuard.decide(record(deleted_at: "t"), world) == :ok
    end

    test "another item's audit event does not block" do
      world = %Canonical.World{item_children: [audit(%{item_id: "i2"})]}

      assert ItemDeleteGuard.decide(record(deleted_at: "t"), world) == :ok
    end

    test "a live sidecar of another type does not block this guard" do
      # S-112: the two guards exclude opposite halves of the sidecar table, and
      # neither subsumes the other. `_ensure_item_delete_safe` in
      # `VitrialSync.Ownership` is the one that sees this row.
      world = %Canonical.World{
        item_children: [%ItemChild{entity_type: "evidence", entity_id: "e1", item_id: "i1"}]
      }

      assert ItemDeleteGuard.decide(record(deleted_at: "t"), world) == :ok
      assert Canonical.item_has_live_sidecars?(world, "i1")
    end

    test "an empty world refuses nothing" do
      assert ItemDeleteGuard.decide(record(deleted_at: "t"), Canonical.empty_world()) == :ok
    end
  end

  describe "tombstone?/1" do
    test "presence of deletedAt is the test, not its emptiness" do
      # A record that carries a full payload *and* a deletedAt is still a delete.
      # `app/sync_service.py` branches on `is not None` throughout.
      assert ItemDeleteGuard.tombstone?(%Record{entity_type: "item", deleted_at: ""})
      refute ItemDeleteGuard.tombstone?(%Record{entity_type: "item", deleted_at: nil})
      refute ItemDeleteGuard.tombstone?(%Record{entity_type: "customer", deleted_at: "t"})
    end
  end

  describe "the rejection code" do
    test "is authorization_rejected, and not lifecycle_rejected or an exception class name" do
      # S-71 / app/rejection_codes.py:69 -- `AuthorizationRejected` maps to the
      # token, never to the Python class name.
      world = %Canonical.World{item_children: [audit()]}
      {:rejected, code} = ItemDeleteGuard.decide(record(deleted_at: "t"), world)

      assert code == "authorization_rejected"
      assert code == VitrialSync.RejectionCode.for_exception("AuthorizationRejected")
      refute code == "AuthorizationRejected"
    end
  end
end
