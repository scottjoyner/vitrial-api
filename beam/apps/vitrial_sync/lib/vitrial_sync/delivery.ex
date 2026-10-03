defmodule VitrialSync.Delivery do
  @moduledoc """
  S-60, admission step 7a: delivery authority.

  `authorize_delivery_execution` (`app/delivery_execution.py:703-828`) followed
  by `apply_delivery_execution_ownership` (`830-865`), reached from
  `app/sync_service.py:347-362`. A `delivery_execution` record is authorized by
  this gate and by **nothing else** -- not by the generic path, not by
  `validate_lifecycle_mutation` (S-59 skips the type).

  ## Why the gate is separate from the generic path

  The reference says it at `app/sync_service.py:334-336`:

      # delivery_execution has a dedicated, stricter lifecycle/authority
      # validator in authorize_delivery_execution. Running the older generic
      # delivery validator as a second authority gate let the two copies drift.

  And `authorize_record` says the same from the other side
  (`app/ownership.py:757-771`): `delivery_execution` is absent by design and an
  unknown type falls off the end into
  `"canonical authorization is not implemented for {entity_type}"`. The spec
  records the consequence as SUSPECT-6: the generic branch is unreachable from
  the HTTP surface and drifted. So `VitrialSync.Ownership` also refuses
  `delivery_execution`, and the only path in is here.

  ## Continuity is the first thing checked

  Before the payload is read at all, the gate establishes that the stored state
  and the canonical ownership row agree about what exists:

    * a tombstone is refused outright (S-137) -- delivery history is non-deletable;
    * an existing row that is tombstoned, or whose payload is not an object, is
      refused, because a historical tombstone must stay inert rather than be
      reinterpreted as a fresh create and resurrected;
    * an existing payload with no `CanonicalProjectChild` is refused, and a
      `CanonicalProjectChild` with no payload is refused. Neither direction is
      recoverable, and both are the signature of a partial migration.

  ## Money

  `quotedTotal` is compared against the sum of the canonical quotation's line
  totals with `Decimal` equality, and the sum is exact. See
  `VitrialSync.Decimal` for why BEAM floats are not a substitute and what the
  representation is.

  ## What `authorize/7` returns

  `{:ok, project_id}` -- the project this execution belongs to, which is what
  `apply_ownership_plan` needs. Or `{:error, message}`, where the message is the
  reference's own; the caller turns it into the `delivery_rejected` code.
  """

  alias VitrialSync.{Canonical, Decimal, Delivery.Handoff, Provenance, Value}

  @entity_type "delivery_execution"
  @capability "delivery.manage"

  @delivery_provenance {"delivery execution event provenance is incomplete",
                        "delivery execution event organization is invalid",
                        "delivery execution latest event does not match authenticated provenance"}
  @delivery_provenance_current_actor {"delivery execution event provenance is incomplete",
                                      "delivery execution event organization is invalid",
                                      "delivery execution latest event does not match authenticated provenance"}

  @statuses MapSet.new([
              "engineeringReview",
              "materialsRequired",
              "procurement",
              "production",
              "readyForInstallation",
              "scheduled",
              "installed",
              "complete"
            ])

  # `DELIVERY_TRANSITIONS`, `app/delivery_execution.py:34-42`. A plain map, so a
  # status that is not a key is a status with no forward transition.
  @transitions %{
    "engineeringReview" => "materialsRequired",
    "materialsRequired" => "procurement",
    "procurement" => "production",
    "production" => "readyForInstallation",
    "readyForInstallation" => "scheduled",
    "scheduled" => "installed",
    "installed" => "complete"
  }

  # `IMMUTABLE_HANDOFF_KEYS`, `app/delivery_execution.py:64-75`: the fields bound
  # to the canonical quotation or to the execution's own identity, none of which
  # a later mutation may move.
  @immutable_handoff_keys ~w(id quotationID quotationNumber quotationRevision projectID customerID currencyCode quotedTotal pricingVersion startedAt)

  @doc "The entity type this gate owns."
  @spec entity_type() :: String.t()
  def entity_type, do: @entity_type

  @doc "Whether this record is dispatched to the delivery authority gate."
  @spec delivery_entity?(String.t()) :: boolean()
  def delivery_entity?(entity_type), do: entity_type == @entity_type

  @doc """
  `authorize_delivery_execution/7`, `app/delivery_execution.py:703-828`.

  Returns `{:ok, project_id}` or `{:error, message}`.

      iex> alias VitrialSync.{Canonical, Delivery}
      iex> world = %Canonical.World{}
      iex> principal = %Canonical.Principal{organization_id: "org", capabilities: MapSet.new()}
      iex> scope = Canonical.Scope.from_principal(principal)
      iex> Delivery.authorize("d1", %{}, nil, nil, principal, scope, world)
      {:error, "delivery.manage capability required"}
  """
  @spec authorize(
          String.t(),
          map(),
          term(),
          Canonical.Entity.t() | nil,
          Canonical.Principal.t(),
          Canonical.Scope.t(),
          Canonical.World.t()
        ) :: {:ok, String.t()} | {:error, String.t()}
  def authorize(entity_id, payload, deleted_at, current, principal, scope, world) do
    with :ok <- require_capability(principal),
         :ok <- refuse_tombstone(deleted_at),
         {:ok, current_payload} <- continuity(entity_id, current, world),
         :ok <- validate_payload(payload, current_payload, principal, entity_id),
         {:ok, project_id, customer_id} <- resolve_identity(payload),
         :ok <- require_active_project(world, project_id, customer_id, scope),
         :ok <- installation_evidence_authority(payload, current_payload, world, project_id),
         :ok <- require_approved_quotation(world, payload, project_id, customer_id) do
      case Canonical.project_child(world, @entity_type, entity_id) do
        nil -> {:ok, project_id}
        %Canonical.ProjectChild{project_id: ^project_id} -> {:ok, project_id}
        _other -> {:error, "delivery execution Project ownership is immutable"}
      end
    end
  end

  @doc """
  `apply_delivery_execution_ownership/4`, `app/delivery_execution.py:830-865`.

  Returns the project-child upsert the reference would `db.add`, or the
  resurrection refusal (S-139). It never sees a tombstoned child from
  `authorize/7` -- that path already refused -- but `apply_ownership/3` is public
  and must stand on its own, so the check is repeated rather than assumed.

      iex> alias VitrialSync.{Canonical, Delivery}
      iex> Delivery.apply_ownership("d1", "p1", Canonical.empty_world())
      {:ok, %{project_child: %{entity_type: "delivery_execution", entity_id: "d1", project_id: "p1", deleted_at: nil}}}
  """
  @spec apply_ownership(String.t(), String.t(), Canonical.World.t()) ::
          {:ok, map()} | {:error, String.t()}
  def apply_ownership(entity_id, project_id, %Canonical.World{} = world) do
    case Canonical.project_child(world, @entity_type, entity_id) do
      nil ->
        {:ok,
         %{
           project_child: %{
             entity_type: @entity_type,
             entity_id: entity_id,
             project_id: project_id,
             deleted_at: nil
           }
         }}

      %Canonical.ProjectChild{deleted_at: nil, project_id: ^project_id} ->
        {:ok,
         %{
           project_child: %{
             entity_type: @entity_type,
             entity_id: entity_id,
             project_id: project_id,
             deleted_at: nil
           }
         }}

      %Canonical.ProjectChild{deleted_at: nil} ->
        {:error, "delivery execution Project ownership is immutable"}

      %Canonical.ProjectChild{} ->
        {:error, "delivery execution tombstoned ownership cannot be resurrected"}
    end
  end

  defp require_capability(principal) do
    if Canonical.capable?(principal, @capability) do
      :ok
    else
      {:error, "#{@capability} capability required"}
    end
  end

  # S-137: `if deleted_at is not None: raise`. Delivery history is non-deletable,
  # so the tombstone branch never reaches the ownership checks below.
  defp refuse_tombstone(nil), do: :ok

  defp refuse_tombstone(_deleted_at),
    do: {:error, "delivery execution history cannot be tombstoned"}

  # The `if current is not None: ... else: ...` block, `app/delivery_execution.py:718-746`.
  defp continuity(entity_id, nil, world) do
    case Canonical.project_child(world, @entity_type, entity_id) do
      nil ->
        {:ok, nil}

      _orphan ->
        {:error, "delivery execution canonical ownership exists without canonical payload"}
    end
  end

  defp continuity(entity_id, %Canonical.Entity{} = current, world) do
    cond do
      not is_nil(current.deleted_at) or is_nil(Canonical.payload_object(current)) ->
        {:error, "existing delivery execution payload is not canonical and active"}

      is_nil(Canonical.project_child(world, @entity_type, entity_id)) ->
        {:error, "existing delivery execution lacks canonical ownership"}

      true ->
        case Canonical.project_child(world, @entity_type, entity_id) do
          %Canonical.ProjectChild{deleted_at: nil} ->
            {:ok, current.payload}

          _tombstoned ->
            {:error, "existing delivery execution ownership is tombstoned"}
        end
    end
  end

  defp resolve_identity(payload) do
    project_id = Value.normalized(Map.get(payload, "projectID"))
    customer_id = Value.normalized(Map.get(payload, "customerID"))
    quotation_id = Value.normalized(Map.get(payload, "quotationID"))

    if is_nil(project_id) or is_nil(customer_id) or is_nil(quotation_id) do
      {:error, "delivery execution project/customer/quotation identity is required"}
    else
      {:ok, project_id, customer_id}
    end
  end

  defp require_active_project(world, project_id, customer_id, scope) do
    case Canonical.project(world, project_id) do
      nil ->
        {:error, "delivery execution Project is not canonical and active"}

      %Canonical.Project{deleted_at: nil} = project ->
        cond do
          project.customer_id != customer_id ->
            {:error, "delivery execution Customer does not match canonical Project"}

          not Canonical.Scope.can_access_project?(scope, project.project_id, project.customer_id) ->
            {:error, "delivery execution Project is outside authorized scope"}

          true ->
            :ok
        end

      _tombstoned ->
        {:error, "delivery execution Project is not canonical and active"}
    end
  end

  # `validate_installation_evidence_authority`, `app/delivery_installation.py:588-630`,
  # with its two `db.get` calls resolved through the world.
  defp installation_evidence_authority(payload, current_payload, world, project_id) do
    with :ok <-
           evidence_references(
             Map.get(payload, "installationCompletion"),
             current_payload && Map.get(current_payload, "installationCompletion"),
             world,
             project_id,
             "completion"
           ) do
      evidence_references(
        Map.get(payload, "deliveryCloseout"),
        current_payload && Map.get(current_payload, "deliveryCloseout"),
        world,
        project_id,
        "closeout"
      )
    end
  end

  defp evidence_references(value, current_value, world, project_id, label) do
    if not is_nil(value) and not Value.py_eq?(value, current_value) do
      if Value.object?(value) do
        # `payload.get("evidenceReferences") or []`, parenthesised: without them
        # `||` binds looser than `|>` and the fold is applied to the `[]` arm,
        # so a non-empty list would be returned as the decision.
        references = Map.get(value, "evidenceReferences") || []

        Enum.reduce_while(references, :ok, fn reference, :ok ->
          case evidence_reference(reference, world, project_id, label) do
            :ok -> {:cont, :ok}
            error -> {:halt, error}
          end
        end)
      else
        {:error,
         if(label == "completion",
           do: "delivery installation completion is malformed",
           else: "delivery closeout is malformed"
         )}
      end
    else
      :ok
    end
  end

  defp evidence_reference(reference, world, project_id, label) do
    cond do
      not Value.object?(reference) ->
        {:error, "delivery installation #{label} evidence reference is malformed"}

      is_nil(Value.normalized(Map.get(reference, "itemID"))) or
          is_nil(Value.normalized(Map.get(reference, "documentID"))) ->
        {:error,
         "delivery installation #{label} evidence reference requires itemID and documentID"}

      true ->
        check_evidence_item(
          world,
          project_id,
          Value.normalized(Map.get(reference, "itemID")),
          Value.normalized(Map.get(reference, "documentID")),
          label
        )
    end
  end

  defp check_evidence_item(world, project_id, item_id, document_id, label) do
    case Canonical.item(world, item_id) do
      %Canonical.Item{deleted_at: nil, project_id: ^project_id} ->
        case Canonical.item_child(world, "evidence", document_id) do
          %Canonical.ItemChild{deleted_at: nil, item_id: ^item_id} ->
            :ok

          _other ->
            {:error,
             "delivery installation #{label} evidence is not canonical for the referenced Item"}
        end

      _other ->
        {:error,
         "delivery installation #{label} evidence Item is not active in the delivery Project"}
    end
  end

  # The quotation half of the authority, `app/delivery_execution.py:784-826`.
  defp require_approved_quotation(world, payload, project_id, customer_id) do
    quotation_id = Value.normalized(Map.get(payload, "quotationID"))

    with {:ok, quote} <- canonical_quotation(world, quotation_id) do
      with :ok <- require_quotation_status(quote),
           :ok <- require_quotation_ownership(quote, project_id, customer_id),
           :ok <- require_approval_history(quote) do
        with {:ok, revision} <- canonical_quotation_revision(quote),
             :ok <- match_quotation_fields(payload, quote, revision) do
          match_quoted_total(payload, quote)
        end
      end
    end
  end

  defp canonical_quotation(world, quotation_id) do
    case Canonical.entity(world, "quotation", quotation_id) do
      %Canonical.Entity{deleted_at: nil} = row ->
        case Canonical.payload_object(row) do
          nil ->
            {:error, "delivery execution requires a canonical approved quotation"}

          quote ->
            {:ok, quote}
        end

      _other ->
        {:error, "delivery execution requires a canonical approved quotation"}
    end
  end

  defp require_quotation_status(quote) do
    if Map.get(quote, "status") == "approved" do
      :ok
    else
      {:error, "delivery execution requires an approved quotation"}
    end
  end

  defp require_quotation_ownership(quote, project_id, customer_id) do
    if Map.get(quote, "projectID") != project_id or Map.get(quote, "customerID") != customer_id do
      {:error, "delivery execution quotation ownership does not match Project"}
    else
      :ok
    end
  end

  # `quote.get("events") or []` then the `any(... kind == "approved")` test.
  defp require_approval_history(quote) do
    case Map.get(quote, "events") do
      events when is_list(events) ->
        if Enum.any?(events, fn event ->
             Value.object?(event) and Map.get(event, "kind") == "approved"
           end) do
          :ok
        else
          {:error, "delivery execution quotation is missing immutable approval history"}
        end

      _falsy_or_other ->
        {:error, "delivery execution quotation is missing immutable approval history"}
    end
  end

  # `_quotation_revision(quote)`, `app/delivery_execution.py:84-90`.
  defp canonical_quotation_revision(quote) do
    case Map.get(quote, "revision") do
      nil ->
        {:ok, 1}

      value ->
        if Value.int?(value) and value >= 1 do
          {:ok, value}
        else
          {:error, "canonical quotation revision is invalid"}
        end
    end
  end

  defp match_quotation_fields(payload, quote, revision) do
    expected = [
      {"quotationNumber", Map.get(quote, "number")},
      {"quotationRevision", revision},
      {"currencyCode", Map.get(quote, "currencyCode")},
      {"pricingVersion", Map.get(quote, "pricingVersion")}
    ]

    Enum.reduce_while(expected, :ok, fn {key, value}, :ok ->
      if Value.py_eq?(Map.get(payload, key), value) do
        {:cont, :ok}
      else
        {:halt, {:error, "delivery execution #{key} does not match canonical quotation"}}
      end
    end)
  end

  # `_decimal(payload.get("quotedTotal")) != _quotation_total(quote)`,
  # `app/delivery_execution.py:824-825` and `103-112`.
  defp match_quoted_total(payload, quote) do
    with {:ok, quoted} <- quoted_total(Map.get(payload, "quotedTotal")),
         {:ok, total} <- quotation_total(quote) do
      if Decimal.not_eq?(quoted, total) do
        {:error, "delivery execution quotedTotal does not match canonical quotation"}
      else
        :ok
      end
    end
  end

  defp quoted_total(value) do
    case Decimal.parse(value) do
      {:ok, decimal} -> {:ok, decimal}
      :error -> {:error, "quotedTotal is invalid"}
    end
  end

  defp quotation_total(quote) do
    case Map.get(quote, "lines") do
      lines when is_list(lines) and lines != [] ->
        Enum.reduce_while(lines, {:ok, Decimal.zero()}, fn line, {:ok, total} ->
          if Value.object?(line) do
            case Decimal.parse(Map.get(line, "totalPrice")) do
              {:ok, decimal} ->
                {:cont, {:ok, Decimal.add(total, decimal)}}

              :error ->
                {:halt, {:error, "canonical quotation totalPrice is invalid"}}
            end
          else
            {:halt, {:error, "canonical quotation line is malformed"}}
          end
        end)

      _other ->
        {:error, "canonical quotation lines are unavailable"}
    end
  end

  # ---------------------------------------------------------------------------
  # validate_delivery_execution_payload -- app/delivery_execution.py:545-700
  # ---------------------------------------------------------------------------

  defp validate_payload(payload, current_payload, principal, entity_id) do
    with :ok <- require_payload_identity(payload, entity_id),
         {:ok, status} <- require_status(Map.get(payload, "status")),
         {:ok, events} <- delivery_events(payload),
         {:ok, _seen} <- unique_event_ids(events),
         :ok <- event_provenance(events, principal),
         :ok <- start_in_engineering_review(events),
         :ok <- walk_transitions(events) do
      with :ok <- anchor_timestamps(payload, events),
           :ok <- handoffs(payload, current_payload, principal, status) do
        enforce_history(payload, current_payload, events, principal)
      end
    end
  end

  defp require_payload_identity(payload, entity_id) do
    if Map.get(payload, "id") == entity_id do
      :ok
    else
      {:error, "delivery execution payload identity is invalid"}
    end
  end

  defp require_status(status) do
    if Value.member?(status, @statuses) do
      {:ok, status}
    else
      {:error, "delivery execution status is invalid"}
    end
  end

  # `_events(payload)`, `app/delivery_execution.py:115-123`: always required here,
  # so the `required=False` arm of the lifecycle reader does not apply.
  defp delivery_events(payload) do
    case Value.event_list(Map.get(payload, "events"), true) do
      {:ok, events} -> {:ok, events}
      {:error, :required} -> {:error, "delivery execution history is required"}
      {:error, :malformed} -> {:error, "delivery execution history is malformed"}
    end
  end

  defp unique_event_ids(events) do
    Enum.reduce_while(events, {:ok, MapSet.new()}, fn event, {:ok, seen} ->
      case Value.normalized(Map.get(event, "id")) do
        nil ->
          {:halt, {:error, "delivery execution event id is required"}}

        id ->
          if MapSet.member?(seen, id) do
            {:halt, {:error, "delivery execution event ids must be unique"}}
          else
            {:cont, {:ok, MapSet.put(seen, id)}}
          end
      end
    end)
  end

  defp event_provenance(events, principal) do
    Enum.reduce_while(events, :ok, fn event, :ok ->
      case Provenance.check(event, principal, false, delivery_provenance_messages(false)) do
        :ok -> {:cont, :ok}
        error -> {:halt, error}
      end
    end)
  end

  defp delivery_provenance_messages(true) do
    @delivery_provenance_current_actor
  end

  defp delivery_provenance_messages(false) do
    @delivery_provenance
  end

  # `first.get("fromStatus") is not None or first.get("toStatus") != "engineeringReview"`
  defp start_in_engineering_review([first | _rest]) do
    if not is_nil(Map.get(first, "fromStatus")) or
         Map.get(first, "toStatus") != "engineeringReview" do
      {:error, "delivery execution must start with Engineering Review"}
    else
      :ok
    end
  end

  # The first event has no predecessor to check against, which is why the walk
  # starts at index 0 with no state: the `index > 0` guards below are the whole
  # of the continuity rule.
  defp walk_transitions(events), do: walk_transitions(events, nil, 0)

  defp walk_transitions([], _state, _index), do: :ok

  defp walk_transitions([event | rest], state, index) do
    to_status = Map.get(event, "toStatus")

    cond do
      not Value.member?(to_status, @statuses) ->
        {:error, "delivery execution event status is invalid"}

      index > 0 and Map.get(event, "fromStatus") != state ->
        {:error, "delivery execution event chain is discontinuous"}

      index > 0 and not allowed_transition?(state, to_status) ->
        {:error, "delivery execution transition is invalid"}

      true ->
        walk_transitions(rest, to_status, index + 1)
    end
  end

  # `DELIVERY_TRANSITIONS.get(state)` plus the four documented self-transitions,
  # `app/delivery_execution.py:581-585`. A self-transition is a plan revision, not
  # a state change, and is what lets a client edit a plan without advancing.
  defp allowed_transition?(state, to_status) do
    Map.get(@transitions, state) == to_status or
      {state, to_status} in [
        {"materialsRequired", "materialsRequired"},
        {"procurement", "procurement"},
        {"production", "production"},
        {"readyForInstallation", "readyForInstallation"},
        {"scheduled", "scheduled"}
      ]
  end

  defp anchor_timestamps(payload, events) do
    cond do
      Map.get(payload, "updatedAt") != Map.get(List.last(events), "occurredAt") ->
        {:error, "delivery execution updatedAt must match its latest immutable event"}

      Map.get(payload, "startedAt") != Map.get(hd(events), "occurredAt") ->
        {:error, "delivery execution startedAt must match its initial event"}

      true ->
        :ok
    end
  end

  # S-126: the four handoffs run in this order, and the reference wraps the
  # production and installation pair in one `try` because their rejections are
  # re-raised as `DeliveryExecutionRejected`. All four raise the same class, so
  # the wrapping changes nothing observable here -- but the *order* is fixed and
  # a test that only checks "some delivery rejection" would not notice a swap.
  defp handoffs(payload, current_payload, principal, status) do
    with :ok <- Handoff.materials(payload, current_payload, principal, status),
         :ok <- Handoff.procurement(payload, current_payload, principal, status),
         :ok <- Handoff.production(payload, current_payload, principal, status) do
      Handoff.installation(payload, current_payload, principal, status)
    end
  end

  # `if current_payload is None: ... return`, then the immutability and history
  # rules, `app/delivery_execution.py:611-700`.
  defp enforce_history(_payload, nil, events, principal) do
    Provenance.check(List.last(events), principal, true, delivery_provenance_messages(true))
  end

  defp enforce_history(payload, current_payload, events, principal) do
    with :ok <- immutable_handoff_fields(current_payload, payload) do
      case current_events(current_payload) do
        {:ok, current_events} ->
          with :ok <- history_length(events, current_events),
               :ok <- history_prefix(events, current_events) do
            appended_rules(payload, current_payload, events, principal)
          end

        {:error, _message} = error ->
          error
      end
    end
  end

  defp immutable_handoff_fields(current_payload, payload) do
    Enum.reduce_while(@immutable_handoff_keys, :ok, fn key, :ok ->
      if Value.py_eq?(Map.get(current_payload, key), Map.get(payload, key)) do
        {:cont, :ok}
      else
        {:halt, {:error, "delivery execution immutable handoff field #{key} changed"}}
      end
    end)
  end

  defp current_events(current_payload) do
    case Map.get(current_payload, "events") do
      events when is_list(events) ->
        if Enum.all?(events, &Value.object?/1) do
          {:ok, events}
        else
          {:error, "canonical delivery execution history is malformed"}
        end

      _other ->
        {:error, "canonical delivery execution history is malformed"}
    end
  end

  defp history_length(events, current_events) do
    if length(events) < length(current_events) do
      {:error, "delivery execution history cannot be truncated"}
    else
      :ok
    end
  end

  defp history_prefix(events, current_events) do
    if Value.py_eq?(Enum.take(events, length(current_events)), current_events) do
      :ok
    else
      {:error, "delivery execution history is immutable"}
    end
  end

  defp appended_rules(payload, current_payload, events, principal) do
    appended = Enum.drop(events, length(Map.get(current_payload, "events")))

    cond do
      appended == [] ->
        operational_change_requires_event(payload, current_payload)

      length(appended) != 1 ->
        {:error, "delivery execution may append only one transition per mutation"}

      true ->
        self_transition_revision(payload, current_payload, List.last(appended))
        |> case do
          {:error, _message} = error ->
            error

          :ok ->
            Provenance.check(
              List.last(appended),
              principal,
              true,
              delivery_provenance_messages(true)
            )
        end
    end
  end

  defp operational_change_requires_event(payload, current_payload) do
    protected =
      ~w(status updatedAt materialsPlan procurementPlan productionPlan installationSchedule installationCompletion)

    if Enum.any?(protected, fn key ->
         not Value.py_eq?(Map.get(current_payload, key), Map.get(payload, key))
       end) do
      {:error, "delivery execution operational changes must append a lifecycle event"}
    else
      :ok
    end
  end

  # The four self-transitions that must carry an actual plan revision,
  # `app/delivery_execution.py:658-699`. A self-transition with an unchanged
  # plan is a no-op dressed as a transition.
  defp self_transition_revision(payload, current_payload, newest) do
    from = Map.get(newest, "fromStatus")
    to = Map.get(newest, "toStatus")

    cond do
      from == "materialsRequired" and to == "materialsRequired" and
          Value.py_eq?(
            Map.get(current_payload, "materialsPlan"),
            Map.get(payload, "materialsPlan")
          ) ->
        {:error, "Materials Required self-transition requires a materials plan revision"}

      from == "procurement" and to == "procurement" and
          Value.py_eq?(
            Map.get(current_payload, "procurementPlan"),
            Map.get(payload, "procurementPlan")
          ) ->
        {:error, "Procurement self-transition requires a procurement plan revision"}

      from == "production" and to == "production" and
          Value.py_eq?(
            Map.get(current_payload, "productionPlan"),
            Map.get(payload, "productionPlan")
          ) ->
        {:error, "Production self-transition requires a production plan revision"}

      from == "readyForInstallation" and to == "readyForInstallation" and
          Value.py_eq?(
            Map.get(current_payload, "installationSchedule"),
            Map.get(payload, "installationSchedule")
          ) ->
        {:error,
         "Installation schedule self-transition requires an installation schedule revision"}

      from == "scheduled" and to == "scheduled" and
        Value.py_eq?(
          Map.get(current_payload, "installationSchedule"),
          Map.get(payload, "installationSchedule")
        ) and
          Value.py_eq?(
            Map.get(current_payload, "installationCompletion"),
            Map.get(payload, "installationCompletion")
          ) ->
        {:error,
         "Scheduled self-transition requires an installation schedule or completion revision"}

      true ->
        :ok
    end
  end
end
