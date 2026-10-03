defmodule VitrialSync.RejectionCode do
  @moduledoc """
  The stable rejection vocabulary of the sync outcome stream.

  A transcription of `app/rejection_codes.py`. The reason this module exists at
  all, in both estates, is `VITR-V006`: the `sync.mutation_result` event used to
  carry `reason=type(exc).__name__`, so every authorization, lifecycle and
  delivery rejection collapsed into one of four Python class names. Those are
  neither aggregatable nor stable across a refactor that renames a class.

  ## Why these decisions return a code rather than a class

  The reference raises `AuthorizationRejected`, `LifecycleRejected` or
  `DeliveryExecutionRejected` and lets `_apply_push_once`'s single `except` clause
  (`app/sync_service.py:384-402`) call `rejection_code(exc)` once. A pure
  function cannot raise across a boundary it does not own, and returning a class
  name would reintroduce exactly the problem the vocabulary was created to fix.

  So the pure decisions return the code directly. `for_exception/1` is the
  classifier the Python uses, kept here so that the mapping from a raising site to
  a code is still one table rather than an assumption spread across five modules.
  Nothing in this app raises: `for_exception/1` is what a future effectful caller
  uses to convert an exception it caught into the same token these functions
  return, which is what makes a BEAM-side and a Python-side `sync.mutation_result`
  comparable token-for-token during dual-run.

  ## The table is a safety net, not the mapping

  `app/rejection_codes.py:63-64` says so: *"Per-site precision is opt-in via an
  explicit `rejection_code` on the raised exception; the table is only the safety
  net."* A `grep` over `app/` finds no exception in this codebase that sets a
  `rejection_code` attribute, so today the table *is* the mapping -- and that is a
  property of the current tree, not a guarantee. `for_exception/1` prefers an
  explicit code for the same reason the Python does, and falls back to
  `internal_error` so that an unmapped exception is visibly unmapped rather than
  silently borrowing a real code.
  """

  @typedoc "One token from the closed vocabulary."
  @type t :: String.t()

  @codes ~w(
    invalid_mutation
    missing_client_mutation_id
    mutation_id_collision
    stale_revision
    idempotent_replay
    rejected_mutation_id_reuse
    authorization_rejected
    capability_missing
    scope_violation
    ownership_immutable
    ownership_missing
    delete_blocked
    lifecycle_rejected
    delivery_rejected
    history_tampering
    committed
    internal_error
  )

  @doc "The closed vocabulary, in the order `app/rejection_codes.py:19-47` declares it."
  @spec codes() :: [t()]
  def codes, do: @codes

  @doc "Whether a string is a token from the vocabulary."
  @spec code?(term()) :: boolean()
  def code?(value), do: is_binary(value) and value in @codes

  @doc "The code the item-delete guard and the generic ownership path return."
  @spec authorization_rejected() :: t()
  def authorization_rejected, do: "authorization_rejected"

  @doc "The code `validate_lifecycle_mutation` raises into."
  @spec lifecycle_rejected() :: t()
  def lifecycle_rejected, do: "lifecycle_rejected"

  @doc """
  The code the delivery authority path returns.

  `DeliveryExecutionRejected`, `DeliveryProductionRejected` and
  `DeliveryInstallationRejected` all map here: the two inner classes are caught
  and re-raised as `DeliveryExecutionRejected` at `app/delivery_execution.py:605-608`
  and `769-772` before they ever reach the outcome stream, so one token is what a
  client actually observes.
  """
  @spec delivery_rejected() :: t()
  def delivery_rejected, do: "delivery_rejected"

  @doc "The code the accepted path reports."
  @spec committed() :: t()
  def committed, do: "committed"

  @default_by_class %{
    "InvalidMutation" => "invalid_mutation",
    "AuthorizationRejected" => "authorization_rejected",
    "LifecycleRejected" => "lifecycle_rejected",
    "DeliveryExecutionRejected" => "delivery_rejected",
    "DeliveryInstallationRejected" => "delivery_rejected",
    "DeliveryProductionRejected" => "delivery_rejected"
  }

  @doc """
  `rejection_code(exc)`, `app/rejection_codes.py:50-60`.

  Takes the raising site's *class name* plus an optional explicit code, in that
  order of preference, and falls back to `internal_error`. The class name is a
  string rather than a module because the input is an exception caught on the
  Python side and carried across as a label; nothing in this app constructs one.

      iex> VitrialSync.RejectionCode.for_exception("LifecycleRejected")
      "lifecycle_rejected"

      iex> VitrialSync.RejectionCode.for_exception("SomethingNew")
      "internal_error"

      iex> VitrialSync.RejectionCode.for_exception("SomethingNew", "delete_blocked")
      "delete_blocked"
  """
  @spec for_exception(String.t(), t() | nil) :: t()
  def for_exception(class_name, explicit \\ nil)

  def for_exception(_class_name, explicit) when is_binary(explicit) and explicit != "",
    do: explicit

  def for_exception(class_name, _explicit) when is_binary(class_name) do
    Map.get(@default_by_class, class_name, "internal_error")
  end

  def for_exception(_class_name, _explicit), do: "internal_error"
end
