defmodule VitrialSync.EntityOrder do
  @moduledoc """
  Canonical parent-before-child processing order for a push batch.

  `ENTITY_ORDER` in `app/ownership.py:77-95`, and the rule behind it
  (`app/ownership.py:98-101`):

  > Canonical parents and referenced child records are evaluated first so a single
  > atomic batch can arrive in arbitrary client order without ever trusting a
  > child-provided parent implicitly.

  That is the whole justification for the table. A client may send
  `[item, project_sector, project]` in one batch and all three are accepted,
  because authorization for the `item` consults the `project` that the *same
  batch* just wrote. Reordering is what makes that safe; without it the `item`
  would be authorized against a parent the server has never seen.

  ## Two orderings, and the difference matters

  The sort is by `(order, original_index)`. Both components are load-bearing:

    * the **order** component is the parent/child hierarchy
    * the **index** component is the client's own order, so two records of the
      same entity type are never reordered relative to each other

  Dropping the index component looks harmless and is not: two `item` records
  touching the same project would then be processed in whatever order the sort
  implementation happened to produce, and the second would be rejected for a
  stale revision against the first.

  ## Reordering is for processing only

  `acceptedRecordIDs` and `rejectedRecordIDs` are emitted in the client's
  original order regardless (`app/sync_service.py:447-448`). A client that
  matches results to records positionally must not be given them in processing
  order, and nothing in this module is allowed to leak that order into the
  response.
  """

  @unknown_order 100

  # Kept as a map literal rather than derived from a list so that a duplicate
  # entity type is a compile-visible duplicate key, not a silent last-one-wins.
  @entity_order %{
    "customer" => 0,
    "project" => 1,
    "project_sector" => 2,
    "item" => 3,
    "item_audit_event" => 4,
    "evidence" => 4,
    "measurement" => 4,
    "blocker" => 4,
    "customer_requirement" => 5,
    "configuration" => 6,
    "configuration_version" => 7,
    "quotation" => 8,
    # A delivery execution is raised against a quotation, so it must be
    # authorized after it. Keeping them adjacent is what lets a client send the
    # approved quotation and the delivery execution it opens in one atomic
    # batch, in any client-side order.
    "delivery_execution" => 9
  }

  @typedoc "The entity types the sync contract admits."
  @type entity_type :: String.t()

  @doc "The order assigned to an entity type; #{@unknown_order} for anything unknown."
  @spec order_for(entity_type()) :: non_neg_integer()
  def order_for(entity_type) when is_binary(entity_type), do: Map.get(@entity_order, entity_type, @unknown_order)

  @doc "The order assigned to an entity type, as a `{:ok, _}` / `{:error, _}` pair."
  @spec known_order_for(entity_type()) :: {:ok, non_neg_integer()} | {:error, :unknown_entity_type}
  def known_order_for(entity_type) when is_binary(entity_type) do
    case Map.fetch(@entity_order, entity_type) do
      {:ok, order} -> {:ok, order}
      :error -> {:error, :unknown_entity_type}
    end
  end

  @doc "The `EntityType` literal list from `app/schemas.py`, in processing order."
  @spec entity_types() :: [entity_type()]
  def entity_types, do: @entity_order |> Map.keys() |> Enum.sort_by(&Map.fetch!(@entity_order, &1))

  @doc """
  The sort key for one record: its parent's order, then its client's index.

  Exposed rather than inlined so the tuple shape has one definition shared by
  `sort/1` and by any caller that needs to explain an ordering decision.
  """
  @spec sort_key(entity_type(), non_neg_integer()) :: {non_neg_integer(), non_neg_integer()}
  def sort_key(entity_type, original_index) when is_integer(original_index) and original_index >= 0,
    do: {order_for(entity_type), original_index}

  @doc """
  Order `{index, record}` pairs for processing, keeping the original index attached.

  The index has to travel with the record: the result lists are keyed by
  original position, so a sort that discarded it would make it impossible to
  report an outcome against the record that earned it.

      iex> VitrialSync.EntityOrder.sort([{0, %{"entityType" => "item"}}, {1, %{"entityType" => "project"}}])
      [{1, %{"entityType" => "project"}}, {0, %{"entityType" => "item"}}]
  """
  @spec sort([{non_neg_integer(), map()}]) :: [{non_neg_integer(), map()}]
  def sort(pairs) when is_list(pairs) do
    Enum.sort_by(pairs, fn {index, record} -> sort_key(Map.get(record, "entityType"), index) end)
  end

  @doc """
  Order `VitrialSync.Record` structs for processing, keeping the original index.

  Same contract as `sort/1`, over the struct rather than a wire map, so the
  push engine does not have to reach into records through string keys.
  """
  @spec sort_records([{non_neg_integer(), VitrialSync.Record.t()}]) ::
          [{non_neg_integer(), VitrialSync.Record.t()}]
  def sort_records(pairs) when is_list(pairs) do
    Enum.sort_by(pairs, fn {index, record} -> sort_key(record.entity_type, index) end)
  end
end
