defmodule VitrialSync.Lifecycle do
  @moduledoc """
  S-59, admission step 6: immutable server-side lifecycle validation.

  `validate_lifecycle_mutation` (`app/lifecycle.py:324-350`), reached from
  `app/sync_service.py:337-346` for **every entity type except
  `delivery_execution`**. Only `blocker` and `quotation` have rules; every other
  type passes through untouched, and that absence is the whole of the generic
  path.

  ## Two facts that decide most of the branch matrix

  **A tombstone is a no-op here.** The first two lines of the reference are
  `if deleted_at is not None: return`, with the docstring's reason: *"Tombstone
  authorization/referential integrity belongs to canonical ownership policy. A V1
  tombstone may intentionally carry an empty payload, so lifecycle validation is
  for upserts."* So a `blocker` or `quotation` tombstone is never
  lifecycle-validated, whatever its payload holds. That is why step 5, the
  item-delete guard, is the one that has to notice tombstones.

  **Only one code leaves this module.** The reference raises exactly
  `LifecycleRejected` on every path here and `rejection_code` maps that class to
  `lifecycle_rejected` (`app/rejection_codes.py:69`). The message goes to the
  exception, not to `sync.mutation_result`. It is still carried in the return
  value, as the third element, so a test can name *which* rule fired: a test that
  asserted only the code could not detect a rule that had stopped firing, and
  this module has enough rules for that to matter.

  ## Composition

  Every rule returns `:ok | {:error, message}` and `verdict/1` is the only place
  that turns an error into a rejection code. That keeps the mapping in one spot
  rather than eighteen.
  """

  alias VitrialSync.{Canonical, Provenance, Record, RejectionCode, Value}

  @blocker_event_kinds MapSet.new(["created", "updated", "resolved", "reopened"])
  @blocker_statuses MapSet.new(["open", "resolved"])
  @quotation_event_kinds MapSet.new(["sent", "approved", "rejected", "superseded"])
  @quotation_statuses MapSet.new(["draft", "ready", "sent", "approved", "rejected", "superseded"])
  @quotation_content_locked MapSet.new(["sent", "rejected", "approved", "superseded"])
  @quotation_lifecycle_keys MapSet.new(["status", "events", "updatedAt"])

  @blocker_protected_keys ~w(category kind description status critical resolvedAt responsiblePerson vendorSupplier)
  @blocker_snapshot_normalized MapSet.new([
                                 "blockerKind",
                                 "description",
                                 "responsiblePerson",
                                 "vendorSupplier"
                               ])

  @blocker_provenance_messages {"blocker event provenance is incomplete",
                                "blocker event organization is invalid",
                                "blocker latest event does not match authenticated provenance"}
  @quotation_provenance_messages {"quotation event provenance is incomplete",
                                  "quotation event organization is invalid",
                                  "quotation latest event does not match authenticated provenance"}

  @typedoc "The reference's message for the lifecycle rule that refused the record."
  @type rejection :: String.t()

  @doc """
  Decide step 6 for one record.

  `current` is the record's `SyncEntity` row or `nil`; `payload` is the object
  decoded in step 4. Returns `:ok` to continue to the authority step, or
  `{:rejected, code, message}`.

      iex> alias VitrialSync.{Canonical, Lifecycle, Record}
      iex> record = %Record{entity_type: "blocker", entity_id: "b1"}
      iex> Lifecycle.decide(record, %{"status" => "open", "description" => "  "}, nil, nil, Canonical.empty_world())
      {:rejected, "lifecycle_rejected", "blocker description is required"}

      iex> alias VitrialSync.{Canonical, Lifecycle, Record}
      iex> record = %Record{entity_type: "item", entity_id: "i1", deleted_at: "2026-10-02T00:00:00Z"}
      iex> Lifecycle.decide(record, %{}, nil, nil, Canonical.empty_world())
      :ok
  """
  @spec decide(
          Record.t(),
          map(),
          Canonical.Principal.t() | nil,
          Canonical.Entity.t() | nil,
          Canonical.World.t()
        ) :: :ok | {:rejected, RejectionCode.t(), rejection()}
  def decide(%Record{} = record, payload, principal, current, %Canonical.World{} = world) do
    cond do
      # `if deleted_at is not None: return`
      not is_nil(record.deleted_at) ->
        :ok

      record.entity_type == "blocker" ->
        verdict(blocker(payload, principal, Canonical.payload_object(current), record.entity_id))

      record.entity_type == "quotation" ->
        verdict(
          quotation(
            payload,
            principal,
            Canonical.payload_object(current),
            record.entity_id,
            world
          )
        )

      true ->
        :ok
    end
  end

  defp verdict(:ok), do: :ok
  defp verdict({:error, message}), do: {:rejected, RejectionCode.lifecycle_rejected(), message}

  # ---------------------------------------------------------------------------
  # blocker -- app/lifecycle.py:129-199
  # ---------------------------------------------------------------------------

  defp blocker(payload, principal, current_payload, entity_id) do
    with :ok <- require_blocker_description(payload),
         :ok <- require_blocker_status(payload),
         :ok <- require_resolved_at_consistent(payload),
         {:ok, item_id} <- require_blocker_item(payload),
         {:ok, events} <- events(payload, "blocker", true),
         {:ok, _seen} <- unique_event_ids(events, "blocker"),
         {:ok, appended} <- immutable_prefix(current_payload, events, "blocker"),
         {:ok, state, last_event} <- walk_blocker_events(events, principal, entity_id, item_id),
         :ok <- require_blocker_status_matches_history(payload, state),
         :ok <- match_blocker_snapshot(payload, last_event),
         :ok <- blocker_change_requires_event(payload, current_payload, appended) do
      tail_provenance(appended, principal, @blocker_provenance_messages)
    end
  end

  defp require_blocker_description(payload) do
    if is_nil(Value.normalized(Map.get(payload, "description"))) do
      {:error, "blocker description is required"}
    else
      :ok
    end
  end

  defp require_blocker_status(payload) do
    if member?(Map.get(payload, "status"), @blocker_statuses) do
      :ok
    else
      {:error, "blocker status is invalid"}
    end
  end

  defp require_resolved_at_consistent(%{"status" => "open"} = payload) do
    if is_nil(Map.get(payload, "resolvedAt")) do
      :ok
    else
      {:error, "open blocker cannot have resolvedAt"}
    end
  end

  defp require_resolved_at_consistent(%{"status" => "resolved"} = payload) do
    if is_nil(Map.get(payload, "resolvedAt")) do
      {:error, "resolved blocker requires resolvedAt"}
    else
      :ok
    end
  end

  defp require_resolved_at_consistent(_payload), do: :ok

  defp require_blocker_item(payload) do
    case Value.normalized(Map.get(payload, "itemID")) do
      nil -> {:error, "blocker itemID is required"}
      item_id -> {:ok, item_id}
    end
  end

  # Returns the state machine's final state and the last event. The last event
  # is needed because `_validate_optional_blocker_snapshot(payload, events[-1])`
  # compares the payload's snapshot fields against it, and `events` is guaranteed
  # non-empty by `events(payload, "blocker", true)`.
  defp walk_blocker_events(events, principal, entity_id, item_id) do
    walk_blocker_events(events, principal, entity_id, item_id, 0, nil, nil, nil)
  end

  defp walk_blocker_events([], _principal, _entity_id, _item_id, _index, state, _previous, last),
    do: {:ok, state, last}

  defp walk_blocker_events(
         [event | rest],
         principal,
         entity_id,
         item_id,
         index,
         state,
         previous,
         _last
       ) do
    with :ok <- require_blocker_event_kind(event),
         :ok <- require_blocker_event_identity(event, entity_id, item_id),
         :ok <- Provenance.check(event, principal, false, @blocker_provenance_messages) do
      case step_blocker_state(event, index, state, previous) do
        {:error, message} ->
          {:error, message}

        {:ok, next_state} ->
          walk_blocker_events(
            rest,
            principal,
            entity_id,
            item_id,
            index + 1,
            next_state,
            blocker_snapshot(event),
            event
          )
      end
    end
  end

  defp require_blocker_event_kind(event) do
    if member?(Map.get(event, "kind"), @blocker_event_kinds) do
      :ok
    else
      {:error, "blocker event kind is invalid"}
    end
  end

  defp require_blocker_event_identity(event, entity_id, item_id) do
    if Map.get(event, "blockerID") == entity_id and Map.get(event, "itemID") == item_id do
      :ok
    else
      {:error, "blocker event identity is invalid"}
    end
  end

  # `_blocker_snapshot`, `app/lifecycle.py:93-101`. A list rather than a tuple so
  # it can go through `Value.py_eq?/2`, which is the Python
  # `snapshot == previous_snapshot` comparison on the `updated` arm.
  defp blocker_snapshot(event) do
    [
      Map.get(event, "blockerCategory"),
      Value.normalized(Map.get(event, "blockerKind")) || "",
      Value.normalized(Map.get(event, "description")) || "",
      Map.get(event, "critical"),
      Value.normalized(Map.get(event, "responsiblePerson")) || "",
      Value.normalized(Map.get(event, "vendorSupplier")) || ""
    ]
  end

  defp step_blocker_state(event, index, state, previous_snapshot) do
    case Map.get(event, "kind") do
      "created" ->
        if index != 0 or not is_nil(state) do
          {:error, "blocker created event must be first"}
        else
          {:ok, "open"}
        end

      "updated" ->
        cond do
          is_nil(state) ->
            {:error, "blocker update cannot precede creation"}

          is_nil(previous_snapshot) ->
            # The reference's `previous_snapshot is not None` guard. Only the
            # `created` arm runs before any snapshot exists, so this is the
            # "update is the first event" case, which `state is None` already
            # refused above; kept so the second condition reads as it does there.
            {:ok, state}

          Value.py_eq?(blocker_snapshot(event), previous_snapshot) ->
            {:error, "blocker updated event must record a material field change"}

          true ->
            {:ok, state}
        end

      "resolved" ->
        if state != "open" do
          {:error, "blocker can only resolve from open"}
        else
          {:ok, "resolved"}
        end

      "reopened" ->
        if state != "resolved" do
          {:error, "blocker can only reopen from resolved"}
        else
          {:ok, "open"}
        end
    end
  end

  defp require_blocker_status_matches_history(payload, state) do
    if state == Map.get(payload, "status") do
      :ok
    else
      {:error, "blocker status does not match lifecycle history"}
    end
  end

  # `_validate_optional_blocker_snapshot`, `app/lifecycle.py:104-127`.
  defp match_blocker_snapshot(payload, event) do
    Enum.reduce_while(
      [
        {"blockerCategory", Map.get(payload, "category")},
        {"blockerKind", Value.normalized(Map.get(payload, "kind")) || ""},
        {"description", Value.normalized(Map.get(payload, "description")) || ""},
        {"critical", Map.get(payload, "critical")},
        {"responsiblePerson", Value.normalized(Map.get(payload, "responsiblePerson")) || ""},
        {"vendorSupplier", Value.normalized(Map.get(payload, "vendorSupplier")) || ""}
      ],
      :ok,
      fn {key, expected}, :ok ->
        if not Map.has_key?(event, key) do
          {:cont, :ok}
        else
          actual =
            if MapSet.member?(@blocker_snapshot_normalized, key) do
              Value.normalized(Map.get(event, key)) || ""
            else
              Map.get(event, key)
            end

          if Value.py_eq?(actual, expected) do
            {:cont, :ok}
          else
            {:halt, {:error, "blocker latest event #{key} does not match snapshot"}}
          end
        end
      end
    )
  end

  defp blocker_change_requires_event(_payload, nil, _appended), do: :ok

  defp blocker_change_requires_event(payload, current_payload, appended) do
    cond do
      appended != [] ->
        :ok

      Enum.any?(@blocker_protected_keys, fn key ->
        Value.py_ne?(Map.get(current_payload, key), Map.get(payload, key))
      end) ->
        {:error, "blocker changes must append a lifecycle event"}

      true ->
        :ok
    end
  end

  # `if appended: _require_connected_provenance(appended[-1], principal, name=..., current_actor=True)`
  defp tail_provenance([], _principal, _messages), do: :ok

  defp tail_provenance(appended, principal, messages) do
    Provenance.check(List.last(appended), principal, true, messages)
  end

  # ---------------------------------------------------------------------------
  # quotation -- app/lifecycle.py:226-322
  # ---------------------------------------------------------------------------

  defp quotation(payload, principal, current_payload, entity_id, world) do
    with :ok <- require_quotation_status(Map.get(payload, "status")),
         {:ok, events} <- events(payload, "quotation", false),
         {:ok, _seen} <- unique_event_ids(events, "quotation"),
         :ok <- quotation_event_kinds(events, principal),
         {:ok, appended} <- immutable_prefix(current_payload, events, "quotation"),
         :ok <- walk_quotation_events(events, Map.get(payload, "status")) do
      case appended do
        [_ | _] = tail ->
          Provenance.check(List.last(tail), principal, true, @quotation_provenance_messages)

        _none ->
          check_quotation_revision(payload, current_payload, entity_id, world)
      end
    end
  end

  defp require_quotation_status(status) do
    if member?(status, @quotation_statuses) do
      :ok
    else
      {:error, "quotation status is invalid"}
    end
  end

  defp quotation_event_kinds(events, principal) do
    Enum.reduce_while(events, :ok, fn event, :ok ->
      if member?(Map.get(event, "kind"), @quotation_event_kinds) do
        case Provenance.check(event, principal, false, @quotation_provenance_messages) do
          :ok -> {:cont, :ok}
          {:error, message} -> {:halt, {:error, message}}
        end
      else
        {:halt, {:error, "quotation event kind is invalid"}}
      end
    end)
  end

  # `_apply_quotation_event` composed with the reference's `if not states:` check.
  defp walk_quotation_events(events, status) do
    case fold_quotation_events(events, MapSet.new(["draft"])) do
      nil ->
        {:error, "quotation lifecycle event sequence is invalid"}

      states ->
        if member?(status, no_event_closure(states)) do
          :ok
        else
          {:error, "quotation status is not reachable from lifecycle history"}
        end
    end
  end

  # `nil` stands for the reference's empty set, because `if not states:` is its
  # dead-end test and an empty `MapSet` is truthy in Elixir.
  defp fold_quotation_events([], states), do: states

  defp fold_quotation_events([event | rest], states) do
    case apply_quotation_event(states, Map.get(event, "kind")) do
      nil -> nil
      next -> fold_quotation_events(rest, next)
    end
  end

  defp apply_quotation_event(states, kind) do
    reachable = no_event_closure(states)

    case kind do
      "sent" -> single("ready", reachable, "sent")
      "approved" -> single("sent", reachable, "approved")
      "rejected" -> single("sent", reachable, "rejected")
      "superseded" -> if supersedable?(reachable), do: MapSet.new(["superseded"]), else: nil
      _other -> nil
    end
  end

  defp single(required, reachable, kind) do
    if MapSet.member?(reachable, required), do: MapSet.new([kind]), else: nil
  end

  defp supersedable?(reachable) do
    Enum.any?(["draft", "ready", "sent", "rejected"], &MapSet.member?(reachable, &1))
  end

  # `_quotation_no_event_closure`, `app/lifecycle.py:200-224`: the fixpoint over
  # draft/ready/rejected, which is the only part of the state graph reachable
  # without an event. `draft <-> ready` and `rejected -> draft`, so one pass is
  # enough in practice, but the reference computes a fixpoint and so does this.
  defp no_event_closure(states) do
    widened = widen_quotation_closure(states)

    if MapSet.equal?(widened, states) do
      states
    else
      no_event_closure(widened)
    end
  end

  # The reference's loop is `if state in states: for nxt in transitions[state]`.
  # Widening unconditionally would pull `ready` and `rejected` into a set that
  # only holds `sent`, and a second `sent` event would then look legal.
  defp widen_quotation_closure(states) do
    Enum.reduce(["draft", "ready", "rejected"], states, fn state, acc ->
      if MapSet.member?(acc, state) do
        Enum.reduce(quotation_destinations(state), acc, &MapSet.put(&2, &1))
      else
        acc
      end
    end)
  end

  defp quotation_destinations("draft"), do: ["ready"]
  defp quotation_destinations("ready"), do: ["draft"]
  defp quotation_destinations("rejected"), do: ["draft"]
  defp quotation_destinations(_state), do: []

  defp check_quotation_revision(payload, current_payload, entity_id, world) do
    with {:ok, revision} <- quotation_revision(payload) do
      supersedes = Value.normalized(Map.get(payload, "supersedesQuotationID"))

      if is_nil(current_payload) do
        check_quotation_predecessor(payload, revision, supersedes, entity_id, world)
      else
        check_quotation_update(payload, current_payload, revision, supersedes)
      end
    end
  end

  defp check_quotation_update(payload, current_payload, revision, supersedes) do
    with {:ok, current_revision} <- quotation_revision(current_payload) do
      cond do
        current_revision != revision ->
          {:error, "quotation revision is immutable"}

        Value.py_ne?(
          Value.normalized(Map.get(current_payload, "supersedesQuotationID")),
          supersedes
        ) ->
          {:error, "quotation supersession identity is immutable"}

        not member?(Map.get(current_payload, "status"), @quotation_statuses) ->
          {:error, "canonical quotation status is invalid"}

        member?(Map.get(current_payload, "status"), @quotation_content_locked) and
            not Value.py_eq?(commercial_content(current_payload), commercial_content(payload)) ->
          {:error, "customer-facing quotation content is locked; create a replacement revision"}

        true ->
          :ok
      end
    end
  end

  # `if revision == 1: ... else: ...` with the reference's two guards inside the
  # else. Split into four clauses so the four cases are visible rather than
  # nested inside one `if`.
  defp check_quotation_predecessor(_payload, 1, nil, _entity_id, _world), do: :ok

  defp check_quotation_predecessor(_payload, 1, _supersedes, _entity_id, _world) do
    {:error, "quotation revision 1 cannot supersede another quotation"}
  end

  defp check_quotation_predecessor(_payload, revision, nil, _entity_id, _world)
       when revision != 1 do
    {:error, "replacement quotation requires a predecessor"}
  end

  defp check_quotation_predecessor(_payload, _revision, supersedes, entity_id, _world)
       when supersedes == entity_id do
    {:error, "replacement quotation requires a predecessor"}
  end

  defp check_quotation_predecessor(payload, revision, supersedes, _entity_id, world) do
    case Canonical.entity(world, "quotation", supersedes) do
      %Canonical.Entity{deleted_at: nil} = row -> check_predecessor_row(payload, revision, row)
      _absent_or_tombstoned -> {:error, "quotation predecessor is not canonical and active"}
    end
  end

  defp check_predecessor_row(payload, revision, row) do
    case Canonical.payload_object(row) do
      nil ->
        {:error, "quotation predecessor is not canonical and active"}

      predecessor ->
        with :ok <- predecessor_field(predecessor, payload, "projectID", "Project"),
             :ok <- predecessor_field(predecessor, payload, "customerID", "Customer"),
             {:ok, predecessor_revision} <- quotation_revision(predecessor) do
          if predecessor_revision == revision - 1 do
            :ok
          else
            {:error, "quotation predecessor revision is not contiguous"}
          end
        end
    end
  end

  defp predecessor_field(predecessor, payload, key, label) do
    if Value.py_eq?(Map.get(predecessor, key), Map.get(payload, key)) do
      :ok
    else
      {:error, "quotation predecessor belongs to another #{label}"}
    end
  end

  # `_quotation_revision`, `app/lifecycle.py:237-243`. Also called on the
  # canonical rows, so a corrupt stored payload refuses here exactly as it does
  # in the reference rather than being read as revision 1.
  defp quotation_revision(payload) do
    case Map.get(payload, "revision") do
      nil ->
        {:ok, 1}

      value ->
        if Value.int?(value) and value >= 1 do
          {:ok, value}
        else
          {:error, "quotation revision must be at least 1"}
        end
    end
  end

  defp commercial_content(payload) do
    Map.reject(payload, fn {key, _value} -> MapSet.member?(@quotation_lifecycle_keys, key) end)
  end

  # ---------------------------------------------------------------------------
  # Shared history helpers
  # ---------------------------------------------------------------------------

  # `_events`, `app/lifecycle.py:27-34`.
  defp events(payload, name, required) do
    case Value.event_list(Map.get(payload, "events"), required) do
      {:ok, events} -> {:ok, events}
      {:error, :required} -> {:error, "#{name} history is required"}
      {:error, :malformed} -> {:error, "#{name} history is malformed"}
    end
  end

  # `_require_unique_event_ids`, `app/lifecycle.py:37-46`.
  defp unique_event_ids(events, name) do
    Enum.reduce_while(events, {:ok, MapSet.new()}, fn event, {:ok, seen} ->
      case Value.normalized(Map.get(event, "id")) do
        nil ->
          {:halt, {:error, "#{name} event id is required"}}

        id ->
          if MapSet.member?(seen, id) do
            {:halt, {:error, "#{name} event ids must be unique"}}
          else
            {:cont, {:ok, MapSet.put(seen, id)}}
          end
      end
    end)
  end

  # `_require_immutable_prefix`, `app/lifecycle.py:81-93`.
  defp immutable_prefix(nil, proposed, _name), do: {:ok, proposed}

  defp immutable_prefix(current_payload, proposed, name) do
    # `current_payload.get("events") or []`. Python's `or` treats every falsy
    # value as absent, including `{}` and `0`, so an empty object or zero here
    # is an empty history rather than a malformed one.
    current = Map.get(current_payload, "events")

    if falsy_json?(current) do
      {:ok, proposed}
    else
      cond do
        not is_list(current) or not Enum.all?(current, &Value.object?/1) ->
          {:error, "canonical #{name} history is malformed"}

        length(proposed) < length(current) ->
          {:error, "#{name} history cannot be truncated"}

        not Value.py_eq?(Enum.take(proposed, length(current)), current) ->
          {:error, "#{name} history is immutable"}

        true ->
          {:ok, Enum.drop(proposed, length(current))}
      end
    end
  end

  @doc false
  # Python truthiness, for the one place the reference leans on `x or default`.
  # `False` and `0` are falsy in Python; in Elixir only `nil` and `false` are, and
  # `[]` and `%{}` are truthy.
  def falsy_json?(nil), do: true
  def falsy_json?(false), do: true
  # `0` and `0.0` are the same falsy number in Python, and Elixir's `==` says so
  # without two clauses that OTP 27 would warn about as the same pattern.
  def falsy_json?(value) when is_number(value), do: value == 0
  def falsy_json?(""), do: true
  def falsy_json?(_value), do: false

  @doc false
  # `value in {...}` with Python equality, delegated so this module's rules read
  # the same way as the delivery ones. See `VitrialSync.Value.member?/2`.
  def member?(value, set), do: Value.member?(value, set)
end
