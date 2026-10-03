defmodule VitrialSync.ItemDeleteGuard do
  @moduledoc """
  S-58, admission step 5: the immutable-audit-history item-delete guard.

  The check as written at `app/sync_service.py:324-333`:

      if (
          record.entityType == "item"
          and record.deletedAt is not None
          and await _item_has_immutable_audit_history(
              db, principal.organization_id, record.entityID
          )
      ):
          raise AuthorizationRejected(
              "Item cannot be deleted while immutable audit history remains"
          )

  It runs **before** the lifecycle and authority steps, which matters: a blocker
  whose canonical history is non-empty is refused with `authorization_rejected`
  even though `validate_lifecycle_mutation` would have been a no-op for it anyway
  (S-59 skips tombstones), and even if the caller holds every capability that the
  ownership path would ask for. An audit event is server-owned and immutable, and
  this is the only place that says so.

  ## The guard and the ownership guard are not interchangeable

  `_ensure_item_delete_safe` (`app/ownership.py:320-334`) is the *other* half of
  S-112 and it excludes `item_audit_event`; this one excludes everything else.
  Both run, this one first. An item can therefore be deleted only when it has no
  live sidecars of any type **and** no live audit events -- and neither guard
  subsumes the other, which is precisely why the spec pins them separately
  (S-112, S-113).

  ## A tombstone with a payload

  The guard branches on `record.deletedAt is not None`, not on the payload being
  empty. S-59's docstring says a V1 tombstone "may intentionally carry an empty
  payload", which is why the lifecycle step is skipped for tombstones; this guard
  is unaffected either way, because it never reads the payload. A record that
  carries a full payload *and* a `deletedAt` is still a delete as far as this
  guard is concerned, and treating it as an upsert would let a client retire an
  item with audit history in one call.
  """

  alias VitrialSync.{Canonical, RejectionCode, Record}

  @doc """
  Decide step 5 for one record.

  Returns `:ok` when the record may continue to the lifecycle and authority
  steps, and `{:rejected, code}` when it may not. The only code it can return is
  `authorization_rejected`, because the only exception the reference raises on
  this path is `AuthorizationRejected` (`app/sync_service.py:331`).

      iex> alias VitrialSync.{Canonical, ItemDeleteGuard, Record}
      iex> record = %Record{entity_type: "item", entity_id: "i1", deleted_at: "2026-10-02T00:00:00Z"}
      iex> world = %Canonical.World{item_children: [
      ...>   %Canonical.ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"}
      ...> ]}
      iex> ItemDeleteGuard.decide(record, world)
      {:rejected, "authorization_rejected"}
  """
  @spec decide(Record.t(), Canonical.World.t()) :: :ok | {:rejected, RejectionCode.t()}
  def decide(%Record{} = record, %Canonical.World{} = world) do
    if record.entity_type == "item" and tombstone?(record) and
         Canonical.item_has_live_audit_history?(world, record.entity_id) do
      {:rejected, RejectionCode.authorization_rejected()}
    else
      :ok
    end
  end

  @doc """
  Whether this record is a tombstone.

  `Record.tombstone?/1`, spelled out here so that reading the guard top to
  bottom shows the whole predicate: the type must be `item`, `deletedAt` must be
  **present**, and a live audit event must exist.
  """
  @spec tombstone?(Record.t()) :: boolean()
  def tombstone?(%Record{entity_type: "item", deleted_at: nil}), do: false
  def tombstone?(%Record{entity_type: "item"}), do: true
  def tombstone?(%Record{}), do: false
end
