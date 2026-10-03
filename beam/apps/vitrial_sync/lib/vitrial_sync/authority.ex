defmodule VitrialSync.Authority do
  @moduledoc """
  Admission steps 5 through 8, in the order the server makes them.

  `VitrialSync.Admission` owns steps 1-4 -- the idempotency key, the prior
  mutation, optimistic concurrency and the payload decode -- and stops at the
  decode because those four need nothing but the record and three facts the
  storage layer can supply. Steps 5-8 need the canonical world, and this module
  is where the boundary sits.

      | # | step | module | code on refusal |
      |---|---|---|---|
      | 5 | item-delete guard | `VitrialSync.ItemDeleteGuard` | `authorization_rejected` |
      | 6 | lifecycle | `VitrialSync.Lifecycle` | `lifecycle_rejected` |
      | 7 | authority | `VitrialSync.Delivery` / `VitrialSync.Ownership` | `delivery_rejected` / `authorization_rejected` |
      | 8 | evidence GC | `VitrialSync.EvidenceGC` | -- |

  ## The order is the contract

  Three of these steps can refuse, and they run strictly in sequence, so a
  record with several problems is reported with the *first* one. That is the
  reference's behaviour (`app/sync_service.py:318-383` is one `try` block with
  the four calls in order), and it is the reason this is a pipeline function
  rather than four functions the caller has to remember to order:

    * an `item` tombstone with live audit history is refused at step 5 even if
      its payload would also have failed lifecycle validation, because a
      tombstone skips step 6 entirely;
    * a `delivery_execution` skips step 6 and goes to the delivery gate, so its
      refusal code is `delivery_rejected` and never `lifecycle_rejected`;
    * the evidence GC enqueue at step 8 runs only after authority has passed,
      which is what keeps a rejected authorization from queueing a delete of an
      object whose metadata was not tombstoned.

  ## Only one code per path, and it is the reference's

  `rejection_code(exc)` (`app/rejection_codes.py:50-60`) maps the four exception
  classes this block catches to four tokens. Each step below returns one of them
  and none of them can return another, because the reference's `except` clause
  (`app/sync_service.py:384-389`) catches exactly four classes and the five steps
  between them raise exactly those.

  ## What `decide/5` returns on success

  The effects the caller has to apply inside the same transaction:

      %{
        authority: :generic | :delivery,
        plan: Ownership.Plan.t() | nil,
        ownership: %{upserts: [...], scope_grant: %{...}, authorization_revision_bump?: bool} | nil,
        evidence_gc: EvidenceGC.request() | nil
      }

  The scope grant is what lets a batch create a parent and then a child that
  depends on it; the GC request is the row to insert in the same transaction as
  the tombstone. Nothing here writes, because a pure function cannot.
  """

  alias VitrialSync.{
    Canonical,
    Delivery,
    EvidenceGC,
    ItemDeleteGuard,
    Lifecycle,
    Ownership,
    Record,
    RejectionCode
  }

  @type effects :: %{
          authority: :generic | :delivery,
          plan: Ownership.Plan.t() | nil,
          ownership: map() | nil,
          evidence_gc: EvidenceGC.request() | nil
        }

  @doc """
  Run steps 5-8 for a record that has cleared the payload decode.

  `current` is the record's `SyncEntity` row or `nil`, which is the same fact
  `Admission.context()` reports as `entity_exists`. The two must agree: a caller
  that says a record exists and then supplies no row gets the "not canonical and
  active" reading, which is the same one the reference produces for a row whose
  `payload_json` is not an object.

  Returns `{:ok, effects}` or `{:rejected, code}`.

      iex> alias VitrialSync.{Authority, Canonical, Record}
      iex> record = %Record{entity_type: "item", entity_id: "i1", deleted_at: "2026-10-02T00:00:00Z"}
      iex> principal = %Canonical.Principal{organization_id: "org", capabilities: MapSet.new()}
      iex> world = %Canonical.World{item_children: [
      ...>   %Canonical.ItemChild{entity_type: "item_audit_event", entity_id: "a1", item_id: "i1"}
      ...> ]}
      iex> Authority.decide(record, %{}, nil, principal, world)
      {:rejected, "authorization_rejected"}
  """
  @spec decide(Record.t(), map(), Canonical.Entity.t() | nil, Canonical.Principal.t(), Canonical.World.t()) ::
          {:ok, effects()} | {:rejected, RejectionCode.t()}
  def decide(%Record{} = record, payload, current, principal, %Canonical.World{} = world) do
    with :ok <- step_item_delete_guard(record, world) do
      case step_authority(record, payload, current, principal, world) do
        {:ok, effects} ->
          case EvidenceGC.decide(record, world) do
            {:ok, request} -> {:ok, Map.put(effects, :evidence_gc, request)}
          end

        {:rejected, _code} = rejected ->
          rejected
      end
    end
  end

  # Step 5. `app/sync_service.py:324-333`.
  defp step_item_delete_guard(record, world) do
    case ItemDeleteGuard.decide(record, world) do
      :ok -> :ok
      {:rejected, code} -> {:rejected, code}
    end
  end

  # Steps 6 and 7. `app/sync_service.py:334-374`.
  defp step_authority(record, payload, current, principal, world) do
    with :ok <- step_lifecycle(record, payload, current, principal, world) do
      if Delivery.delivery_entity?(record.entity_type) do
        delivery_authority(record, payload, current, principal, world)
      else
        generic_authority(record, payload, current, principal, world)
      end
    end
  end

  # Step 6 runs for every type except `delivery_execution`
  # (`app/sync_service.py:337`). Its return type is a three-tuple, so the message
  # is dropped here: the code is what reaches the outcome stream.
  defp step_lifecycle(record, payload, current, principal, world) do
    case Lifecycle.decide(record, payload, principal, current, world) do
      :ok -> :ok
      {:rejected, code, _message} -> {:rejected, code}
    end
  end

  defp delivery_authority(record, payload, current, principal, world) do
    scope = Canonical.Scope.from_principal(principal)

    with {:ok, project_id} <-
           Delivery.authorize(
             record.entity_id,
             payload,
             record.deleted_at,
             current,
             principal,
             scope,
             world
           ),
         {:ok, ownership} <- Delivery.apply_ownership(record.entity_id, project_id, world) do
      {:ok,
       %{
         authority: :delivery,
         plan: nil,
         ownership: ownership,
         evidence_gc: nil
       }}
    else
      {:error, _message} -> {:rejected, RejectionCode.delivery_rejected()}
    end
  end

  defp generic_authority(record, payload, current, principal, world) do
    scope = Canonical.Scope.from_principal(principal)
    generic_entity_exists = not is_nil(current)

    with {:ok, plan} <-
           Ownership.authorize(
             record.entity_type,
             record.entity_id,
             payload,
             record.deleted_at,
             principal,
             scope,
             generic_entity_exists,
             world
           ),
         {:ok, ownership} <- Ownership.apply_plan(plan, world, principal) do
      {:ok, %{authority: :generic, plan: plan, ownership: ownership, evidence_gc: nil}}
    else
      {:error, _message} -> {:rejected, RejectionCode.authorization_rejected()}
    end
  end
end
