defmodule VitrialSync.Delivery.Handoff do
  @moduledoc """
  The four delivery handoff validators, kept apart from the authority gate.

  `validate_delivery_execution_payload` (`app/delivery_execution.py:545-700`)
  calls four sub-validators in a fixed order (S-126), and three of them live in
  their own Python modules:

    * `_validate_materials_handoff` -- `app/delivery_execution.py:379-438`
    * `_validate_procurement_handoff` -- `app/delivery_execution.py:452-544`
    * `validate_production_handoff` -- `app/delivery_production.py:135-205`
    * `validate_installation_handoff` -- `app/delivery_installation.py:297-546`

  They are here rather than inside `VitrialSync.Delivery` because they share
  almost nothing: each validates one frozen plan object, each has its own status
  vocabulary, and each is about a hundred lines. Splitting them keeps the
  authority module readable and lets a test name which handoff refused.

  ## The shape they all share

  Every one of the four is the same three-part function:

    1. validate the *proposed* plan object, and where it depends on another,
       validate the frozen plan it must reference by revision;
    2. refuse a plan authored in a status that cannot carry one, and refuse a
       status that requires one and has none;
    3. if there is a current payload, validate its plan the same way, then apply
       the freeze / editable / one-step rules for the transition.

  The freeze rules are where the value is. Once a plan's stage has been left
  behind, the plan is immutable; while the stage is current a revision must
  advance by exactly one and its `updatedAt` must equal the execution's
  `updatedAt`; and at a transition boundary the plan must be saved first, so a
  client cannot advance the state machine and edit the plan in the same
  mutation.

  ## Comparison is Python's

  `plan != current_plan` is a Python dict comparison and `plan["revision"] !=
  expected` is a Python scalar comparison. Both go through
  `VitrialSync.Value.py_eq?/2`, so a plan that differs only in `1` versus `1.0`
  is *unchanged* here exactly as it is there.
  """

  alias VitrialSync.{Decimal, Provenance, Value}

  # `isinstance(x, dict)` appears in guard position throughout the Python, and
  # the guards below mirror it, so `object?/1` has to be usable in a guard here. A
  # plain function cannot be, and neither can a remote call, so the one-line body
  # of `VitrialSync.Value.object?/1` is written out. `is_struct/1` is a macro and
  # is guard-legal; that is what keeps a struct from counting as a JSON object.
  defmacro object?(value) do
    quote(do: is_map(unquote(value)) and not is_struct(unquote(value)))
  end

  # A plan validator hands back the object it validated, because the caller
  # reasons about that object (its revision, its status, whether it changed). A
  # `with` yields its last expression, which here is the provenance verdict, so
  # the object has to be re-attached to the `:ok` rather than left behind.
  defp validated(value, :ok), do: {:ok, value}
  defp validated(_value, {:error, _message} = error), do: error

  @materials_frozen MapSet.new([
                      "procurement",
                      "production",
                      "readyForInstallation",
                      "scheduled",
                      "installed",
                      "complete"
                    ])
  @procurement_frozen MapSet.new([
                        "production",
                        "readyForInstallation",
                        "scheduled",
                        "installed",
                        "complete"
                      ])
  @procurement_requirement_statuses MapSet.new([
                                      "unconfirmed",
                                      "ordered",
                                      "shortage",
                                      "available"
                                    ])
  @production_frozen MapSet.new(["readyForInstallation", "scheduled", "installed", "complete"])
  @production_step_statuses MapSet.new(["pending", "inProgress", "complete"])
  @installation_editable MapSet.new(["readyForInstallation", "scheduled"])
  @installation_frozen MapSet.new(["complete"])

  @pre_production MapSet.new(["engineeringReview", "materialsRequired", "procurement"])

  @materials_messages {"delivery materials plan provenance is incomplete",
                       "delivery materials plan organization is invalid",
                       "delivery materials plan does not match authenticated provenance"}
  @procurement_messages {"delivery procurement plan provenance is incomplete",
                         "delivery procurement plan organization is invalid",
                         "delivery procurement plan does not match authenticated provenance"}
  @production_messages {"delivery production plan provenance is incomplete",
                        "delivery production plan organization is invalid",
                        "delivery production plan does not match authenticated provenance"}

  @doc "`MATERIALS_FROZEN_STATUSES`, `app/delivery_execution.py:44-51`."
  @spec materials_frozen_statuses() :: MapSet.t(String.t())
  def materials_frozen_statuses, do: @materials_frozen

  @doc "`_validate_materials_handoff`, `app/delivery_execution.py:379-438`."
  @spec materials(map(), map() | nil, map(), String.t()) :: :ok | {:error, String.t()}
  def materials(payload, current_payload, principal, status) do
    with {:ok, plan} <- validate_materials_plan(Map.get(payload, "materialsPlan"), principal),
         :ok <- materials_stage(plan, status),
         {:ok, current_plan} <- current_materials_plan(current_payload, principal) do
      materials_transition(payload, plan, current_payload, current_plan, status, principal)
    end
  end

  defp materials_stage(plan, status) do
    cond do
      status == "engineeringReview" and not is_nil(plan) ->
        {:error, "delivery materials plan can only be authored during Materials Required"}

      MapSet.member?(@materials_frozen, status) and is_nil(plan) ->
        {:error, "delivery materials plan is required before Procurement"}

      true ->
        :ok
    end
  end

  defp current_materials_plan(nil, _principal), do: {:ok, nil}

  defp current_materials_plan(current_payload, principal) do
    validate_materials_plan(Map.get(current_payload, "materialsPlan"), principal)
  end

  defp materials_transition(_payload, _plan, nil, _current_plan, _status, _principal), do: :ok

  defp materials_transition(payload, plan, current_payload, current_plan, status, principal) do
    current_status = Map.get(current_payload, "status")

    cond do
      MapSet.member?(@materials_frozen, current_status) ->
        freeze(
          plan,
          current_plan,
          "delivery materials plan is immutable after Procurement starts"
        )

      current_status == "engineeringReview" ->
        reject_unless_nil(
          plan,
          "enter Materials Required before authoring the delivery materials plan"
        )

      current_status != "materialsRequired" ->
        :ok

      status == "procurement" ->
        cond do
          is_nil(current_plan) ->
            {:error, "save a delivery materials plan before starting Procurement"}

          not Value.py_eq?(plan, current_plan) ->
            {:error, "delivery materials plan must be saved before the Procurement transition"}

          true ->
            :ok
        end

      status != "materialsRequired" or Value.py_eq?(plan, current_plan) ->
        :ok

      is_nil(plan) ->
        {:error, "delivery materials plan cannot be cleared"}

      true ->
        revision_step(
          payload,
          plan,
          current_plan,
          "delivery materials plan",
          principal,
          @materials_messages
        )
    end
  end

  # `_validate_materials_plan`, `app/delivery_execution.py:218-278`.
  defp validate_materials_plan(nil, _principal), do: {:ok, nil}

  defp validate_materials_plan(plan, _principal) when not object?(plan),
    do: {:error, "delivery materials plan is malformed"}

  defp validate_materials_plan(plan, principal) do
    with :ok <- positive_revision(plan, "delivery materials plan"),
         :ok <-
           required_string(plan, "updatedAt", "delivery materials plan updatedAt is required"),
         {:ok, requirements} <-
           non_empty_list(
             plan,
             "requirements",
             "delivery materials plan requires at least one material"
           ),
         {:ok, _seen} <- unique_ids(requirements, "id", "delivery material requirement"),
         :ok <- materials_requirements(requirements) do
      validated(plan, Provenance.check(plan, principal, false, @materials_messages))
    end
  end

  defp materials_requirements(requirements) do
    Enum.reduce_while(requirements, :ok, fn requirement, :ok ->
      with :ok <-
             required_string(
               requirement,
               "description",
               "delivery material description is required"
             ),
           :ok <- required_string(requirement, "unit", "delivery material unit is required"),
           {:ok, quantity} <-
             decimal(Map.get(requirement, "quantity"), "delivery material quantity"),
           :ok <- optional_string(requirement, "note", "delivery material note is invalid") do
        if Decimal.positive?(quantity) do
          {:cont, :ok}
        else
          {:halt, {:error, "delivery material quantity must be greater than zero"}}
        end
      else
        {:error, _message} = error -> {:halt, error}
      end
    end)
  end

  # ---------------------------------------------------------------------------

  @doc "`_validate_procurement_handoff`, `app/delivery_execution.py:452-544`."
  @spec procurement(map(), map() | nil, map(), String.t()) :: :ok | {:error, String.t()}
  def procurement(payload, current_payload, principal, status) do
    with {:ok, materials_plan} <-
           validate_materials_plan(Map.get(payload, "materialsPlan"), principal),
         {:ok, plan} <-
           validate_procurement_plan(
             Map.get(payload, "procurementPlan"),
             materials_plan,
             principal
           ),
         :ok <- procurement_stage(plan, status),
         {:ok, current_plan} <- current_procurement_plan(current_payload, principal) do
      procurement_transition(payload, plan, current_payload, current_plan, status, principal)
    end
  end

  defp procurement_stage(plan, status) do
    cond do
      status in ["engineeringReview", "materialsRequired"] and not is_nil(plan) ->
        {:error, "delivery procurement plan can only be authored during Procurement"}

      MapSet.member?(@procurement_frozen, status) ->
        require_production_ready(plan)

      true ->
        :ok
    end
  end

  # `_require_production_ready`, `app/delivery_execution.py:441-451`.
  defp require_production_ready(nil) do
    {:error, "save delivery procurement readiness before starting Production"}
  end

  defp require_production_ready(plan) do
    blocked =
      Enum.any?(Map.get(plan, "requirements") || [], fn requirement ->
        not object?(requirement) or Map.get(requirement, "status") != "available"
      end)

    if blocked do
      {:error, "delivery Production is blocked until every frozen material is available"}
    else
      :ok
    end
  end

  defp current_procurement_plan(nil, _principal), do: {:ok, nil}

  defp current_procurement_plan(current_payload, principal) do
    with {:ok, current_materials} <-
           validate_materials_plan(Map.get(current_payload, "materialsPlan"), principal) do
      validate_procurement_plan(
        Map.get(current_payload, "procurementPlan"),
        current_materials,
        principal
      )
    end
  end

  defp procurement_transition(_payload, _plan, nil, _current_plan, _status, _principal), do: :ok

  defp procurement_transition(payload, plan, current_payload, current_plan, status, principal) do
    current_status = Map.get(current_payload, "status")

    cond do
      MapSet.member?(@procurement_frozen, current_status) ->
        freeze(
          plan,
          current_plan,
          "delivery procurement plan is immutable after Production starts"
        )

      MapSet.member?(@pre_production, current_status) ->
        reject_unless_nil(
          plan,
          "enter Procurement before authoring delivery procurement readiness"
        )

      current_status != "procurement" ->
        :ok

      status == "production" ->
        cond do
          is_nil(current_plan) ->
            {:error, "save delivery procurement readiness before starting Production"}

          not Value.py_eq?(plan, current_plan) ->
            {:error, "delivery procurement plan must be saved before the Production transition"}

          true ->
            require_production_ready(plan)
        end

      status != "procurement" or Value.py_eq?(plan, current_plan) ->
        :ok

      is_nil(plan) ->
        {:error, "delivery procurement plan cannot be cleared"}

      true ->
        revision_step(
          payload,
          plan,
          current_plan,
          "delivery procurement plan",
          principal,
          @procurement_messages
        )
    end
  end

  # `_validate_procurement_plan`, `app/delivery_execution.py:280-377`.
  defp validate_procurement_plan(nil, _materials_plan, _principal), do: {:ok, nil}

  defp validate_procurement_plan(plan, _materials_plan, _principal) when not object?(plan),
    do: {:error, "delivery procurement plan is malformed"}

  defp validate_procurement_plan(plan, materials_plan, principal) do
    with :ok <- procurement_requires_materials(materials_plan),
         :ok <- positive_revision(plan, "delivery procurement plan"),
         :ok <-
           frozen_reference(
             plan,
             "materialsPlanRevision",
             materials_plan,
             "delivery procurement plan materials revision does not match frozen materials"
           ),
         :ok <-
           required_string(plan, "updatedAt", "delivery procurement plan updatedAt is required"),
         {:ok, requirements} <-
           non_empty_list(
             plan,
             "requirements",
             "delivery procurement plan requires every frozen material"
           ) do
      frozen = frozen_materials(materials_plan)

      with {:ok, _seen} <- procurement_requirements(requirements, frozen),
           :ok <- covers_frozen_materials(requirements, frozen) do
        validated(plan, Provenance.check(plan, principal, false, @procurement_messages))
      end
    end
  end

  defp procurement_requires_materials(nil) do
    {:error, "delivery procurement plan requires a frozen materials plan"}
  end

  defp procurement_requires_materials(_materials_plan), do: :ok

  # No nil clause: `procurement_requires_materials(nil)` above already halts the
  # pipeline with an error, so by the time this is reached the plan is present.
  # The clause that was here could not be invoked, which is exactly what
  # `mix compile --warnings-as-errors` reported — and that is the build the BEAM
  # CI job runs, so a clause no caller can reach is a red build, not a style note.
  defp frozen_materials(materials_plan) do
    Enum.reduce(List.wrap(Map.get(materials_plan, "requirements") || []), %{}, fn requirement,
                                                                                  acc ->
      case Value.normalized(Map.get(requirement, "id")) do
        nil -> acc
        id -> Map.put(acc, id, requirement)
      end
    end)
  end

  defp procurement_requirements(requirements, frozen) do
    Enum.reduce_while(requirements, {:ok, MapSet.new()}, fn requirement, {:ok, seen} ->
      case procurement_requirement(requirement, frozen) do
        {:error, _message} = error ->
          {:halt, error}

        {:ok, id} ->
          if MapSet.member?(seen, id) do
            {:halt, {:error, "delivery procurement requirement ids must be unique"}}
          else
            {:cont, {:ok, MapSet.put(seen, id)}}
          end
      end
    end)
  end

  defp procurement_requirement(requirement, frozen) do
    with {:ok, id} <-
           required_id(
             requirement,
             "requirementID",
             "delivery procurement requirement id is required"
           ),
         :ok <- require_frozen_material(id, frozen),
         :ok <- valid_procurement_status(requirement),
         :ok <- valid_lead_time(requirement),
         :ok <- valid_expected_availability(requirement),
         :ok <-
           optional_string(
             requirement,
             "note",
             "delivery procurement requirement note is invalid"
           ) do
      shortage = valid_shortage(requirement, frozen, id)
      if shortage == :ok, do: {:ok, id}, else: shortage
    end
  end

  defp require_frozen_material(id, frozen) do
    if Map.has_key?(frozen, id) do
      :ok
    else
      {:error, "delivery procurement requirement references unknown frozen material"}
    end
  end

  defp valid_procurement_status(requirement) do
    if Value.member?(Map.get(requirement, "status"), @procurement_requirement_statuses) do
      :ok
    else
      {:error, "delivery procurement requirement status is invalid"}
    end
  end

  defp valid_lead_time(requirement) do
    case Map.get(requirement, "leadTimeDays") do
      nil ->
        :ok

      value ->
        if Value.int?(value) and value >= 0 do
          :ok
        else
          {:error, "delivery procurement requirement lead time is invalid"}
        end
    end
  end

  # `if expected_available_at is not None and _normalized(expected_available_at) is None`
  defp valid_expected_availability(requirement) do
    case Map.get(requirement, "expectedAvailableAt") do
      nil ->
        :ok

      value ->
        if is_nil(Value.normalized(value)),
          do: {:error, "delivery procurement expected availability is invalid"},
          else: :ok
    end
  end

  defp valid_shortage(requirement, frozen, id) do
    shortage_quantity = Map.get(requirement, "shortageQuantity")

    if Map.get(requirement, "status") == "shortage" do
      material = Map.get(frozen, id) || %{}

      with {:ok, shortage} <- decimal(shortage_quantity, "delivery procurement shortage quantity"),
           {:ok, required} <- decimal(Map.get(material, "quantity"), "delivery material quantity") do
        if Decimal.positive?(shortage) and Decimal.compare(shortage, required) != :gt do
          :ok
        else
          {:error, "delivery procurement shortage quantity is invalid"}
        end
      end
    else
      if is_nil(shortage_quantity) do
        :ok
      else
        {:error, "delivery procurement shortage quantity is only valid for shortages"}
      end
    end
  end

  defp covers_frozen_materials(requirements, frozen) do
    seen =
      MapSet.new(
        Enum.map(requirements, fn requirement ->
          Value.normalized(Map.get(requirement, "requirementID"))
        end)
      )

    expected = MapSet.new(Map.keys(frozen))
    missing = MapSet.difference(expected, seen)

    cond do
      MapSet.equal?(seen, expected) ->
        :ok

      MapSet.size(missing) > 0 ->
        {:error, "delivery procurement plan is missing frozen material requirements"}

      true ->
        {:error, "delivery procurement plan requirements do not match frozen materials"}
    end
  end

  # ---------------------------------------------------------------------------

  @doc "`validate_production_handoff`, `app/delivery_production.py:135-205`."
  @spec production(map(), map() | nil, map(), String.t()) :: :ok | {:error, String.t()}
  def production(payload, current_payload, principal, status) do
    with {:ok, plan} <-
           validate_production_plan(
             Map.get(payload, "productionPlan"),
             Map.get(payload, "procurementPlan"),
             principal
           ),
         :ok <- production_stage(plan, status),
         {:ok, current_plan} <- current_production_plan(current_payload, principal) do
      production_transition(plan, current_payload, current_plan, status, payload, principal)
    end
  end

  defp production_stage(plan, status) do
    cond do
      MapSet.member?(@pre_production, status) and not is_nil(plan) ->
        {:error, "delivery production plan can only be authored during Production"}

      MapSet.member?(@production_frozen, status) ->
        require_installation_ready(plan)

      true ->
        :ok
    end
  end

  # `_require_installation_ready`, `app/delivery_production.py:119-133`.
  defp require_installation_ready(nil) do
    {:error, "save delivery production work before marking Ready for Installation"}
  end

  defp require_installation_ready(plan) do
    blocked =
      Enum.any?(Map.get(plan, "steps") || [], fn step ->
        not object?(step) or Map.get(step, "status") != "complete"
      end)

    if blocked do
      {:error,
       "delivery Ready for Installation is blocked until every production step is complete"}
    else
      :ok
    end
  end

  defp current_production_plan(nil, _principal), do: {:ok, nil}

  defp current_production_plan(current_payload, principal) do
    validate_production_plan(
      Map.get(current_payload, "productionPlan"),
      Map.get(current_payload, "procurementPlan"),
      principal
    )
  end

  defp production_transition(_plan, nil, _current_plan, _status, _payload, _principal), do: :ok

  defp production_transition(plan, current_payload, current_plan, status, payload, principal) do
    current_status = Map.get(current_payload, "status")

    cond do
      MapSet.member?(@production_frozen, current_status) ->
        freeze(
          plan,
          current_plan,
          "delivery production plan is immutable after Ready for Installation"
        )

      MapSet.member?(@pre_production, current_status) ->
        reject_unless_nil(plan, "enter Production before authoring delivery production work")

      current_status != "production" ->
        :ok

      status == "readyForInstallation" ->
        cond do
          is_nil(current_plan) ->
            {:error, "save delivery production work before marking Ready for Installation"}

          not Value.py_eq?(plan, current_plan) ->
            {:error,
             "delivery production plan must be saved before the Ready for Installation transition"}

          true ->
            require_installation_ready(plan)
        end

      status != "production" or Value.py_eq?(plan, current_plan) ->
        :ok

      is_nil(plan) ->
        {:error, "delivery production plan cannot be cleared"}

      true ->
        revision_step(
          payload,
          plan,
          current_plan,
          "delivery production plan",
          principal,
          @production_messages
        )
    end
  end

  # `_validate_plan`, `app/delivery_production.py:60-116`.
  defp validate_production_plan(nil, _procurement_plan, _principal), do: {:ok, nil}

  defp validate_production_plan(plan, _procurement_plan, _principal) when not object?(plan),
    do: {:error, "delivery production plan is malformed"}

  defp validate_production_plan(plan, procurement_plan, principal) do
    if not object?(procurement_plan) do
      {:error, "delivery production plan requires a frozen procurement plan"}
    else
      with :ok <- positive_revision(plan, "delivery production plan"),
           :ok <-
             frozen_reference(
               plan,
               "procurementPlanRevision",
               procurement_plan,
               "delivery production plan procurement revision does not match frozen procurement"
             ),
           :ok <-
             required_string(plan, "updatedAt", "delivery production plan updatedAt is required"),
           {:ok, steps} <-
             non_empty_list(
               plan,
               "steps",
               "delivery production plan requires at least one production step"
             ),
           {:ok, _seen} <- unique_ids(steps, "id", "delivery production step"),
           :ok <- production_steps(steps) do
        validated(plan, Provenance.check(plan, principal, false, @production_messages))
      end
    end
  end

  defp production_steps(steps) do
    Enum.reduce_while(steps, :ok, fn step, :ok ->
      cond do
        not object?(step) ->
          {:halt, {:error, "delivery production step is malformed"}}

        is_nil(Value.normalized(Map.get(step, "id"))) ->
          {:halt, {:error, "delivery production step id is required"}}

        is_nil(Value.normalized(Map.get(step, "description"))) ->
          {:halt, {:error, "delivery production step description is required"}}

        not Value.member?(Map.get(step, "status"), @production_step_statuses) ->
          {:halt, {:error, "delivery production step status is invalid"}}

        true ->
          case optional_string(step, "note", "delivery production step note is invalid") do
            :ok -> {:cont, :ok}
            error -> {:halt, error}
          end
      end
    end)
  end

  # ---------------------------------------------------------------------------
  # installation -- app/delivery_installation.py
  # ---------------------------------------------------------------------------

  @doc "`validate_installation_handoff`, `app/delivery_installation.py:297-546`."
  @spec installation(map(), map() | nil, map(), String.t()) :: :ok | {:error, String.t()}
  def installation(payload, current_payload, principal, status) do
    with {:ok, schedule} <-
           validate_schedule(
             Map.get(payload, "installationSchedule"),
             Map.get(payload, "productionPlan"),
             principal
           ),
         :ok <- schedule_frozen_by_completion(current_payload, schedule, principal),
         {:ok, completion} <-
           validate_completion(
             Map.get(payload, "installationCompletion"),
             schedule,
             principal
           ),
         {:ok, closeout} <-
           validate_closeout(Map.get(payload, "deliveryCloseout"), completion, principal),
         :ok <- installation_stage(schedule, completion, closeout, status) do
      installation_transition(
        payload,
        schedule,
        completion,
        closeout,
        current_payload,
        principal,
        status
      )
    end
  end

  defp installation_stage(schedule, completion, closeout, status) do
    cond do
      status in ["engineeringReview", "materialsRequired", "procurement", "production"] and
          not is_nil(schedule) ->
        {:error,
         "delivery installation schedule can only be authored after Production is complete"}

      status in ["scheduled", "installed", "complete"] and is_nil(schedule) ->
        {:error, "save delivery installation schedule before moving work to Scheduled"}

      status in [
        "engineeringReview",
        "materialsRequired",
        "procurement",
        "production",
        "readyForInstallation"
      ] and not is_nil(completion) ->
        {:error, "delivery installation completion can only be authored while Scheduled"}

      status in ["installed", "complete"] and is_nil(completion) ->
        {:error, "save installation completion evidence before marking delivery Installed"}

      status != "complete" and not is_nil(closeout) ->
        {:error, "delivery closeout can only be recorded with the Complete transition"}

      status == "complete" and is_nil(closeout) ->
        {:error, "record delivery closeout before marking delivery Complete"}

      true ->
        :ok
    end
  end

  defp installation_transition(
         _payload,
         _schedule,
         _completion,
         _closeout,
         nil,
         _principal,
         _status
       ),
       do: :ok

  defp installation_transition(
         payload,
         schedule,
         completion,
         closeout,
         current_payload,
         principal,
         status
       ) do
    with {:ok, current_schedule} <-
           validate_schedule(
             Map.get(current_payload, "installationSchedule"),
             Map.get(current_payload, "productionPlan"),
             principal
           ),
         {:ok, current_completion} <-
           validate_completion(
             Map.get(current_payload, "installationCompletion"),
             current_schedule,
             principal
           ),
         {:ok, current_closeout} <-
           validate_closeout(
             Map.get(current_payload, "deliveryCloseout"),
             current_completion,
             principal
           ) do
      installed_or_editable(
        payload,
        schedule,
        completion,
        closeout,
        Map.get(current_payload, "status"),
        current_schedule,
        current_completion,
        current_closeout,
        status,
        principal
      )
    end
  end

  defp installed_or_editable(
         payload,
         schedule,
         completion,
         closeout,
         current_status,
         current_schedule,
         current_completion,
         current_closeout,
         status,
         principal
       ) do
    cond do
      MapSet.member?(@installation_frozen, current_status) ->
        cond do
          not Value.py_eq?(schedule, current_schedule) ->
            {:error, "delivery installation schedule is immutable after installation is recorded"}

          not Value.py_eq?(completion, current_completion) ->
            {:error,
             "delivery installation completion is immutable after installation is recorded"}

          not Value.py_eq?(closeout, current_closeout) ->
            {:error, "delivery closeout is immutable after delivery is Complete"}

          true ->
            :ok
        end

      current_status == "installed" ->
        installed_transition(
          payload,
          schedule,
          completion,
          closeout,
          current_schedule,
          current_completion,
          current_closeout,
          status,
          principal
        )

      not MapSet.member?(@installation_editable, current_status) ->
        cond do
          not is_nil(schedule) ->
            {:error, "enter Ready for Installation before authoring an installation schedule"}

          not is_nil(completion) ->
            {:error, "enter Scheduled before authoring installation completion"}

          true ->
            :ok
        end

      current_status == "readyForInstallation" ->
        ready_transition(payload, schedule, completion, current_schedule, status, principal)

      status == "installed" ->
        recording_installation(schedule, completion, current_schedule, current_completion)

      status != "scheduled" ->
        :ok

      true ->
        scheduled_transition(
          payload,
          schedule,
          completion,
          current_schedule,
          current_completion,
          principal
        )
    end
  end

  defp installed_transition(
         payload,
         schedule,
         completion,
         closeout,
         current_schedule,
         current_completion,
         current_closeout,
         status,
         principal
       ) do
    cond do
      not Value.py_eq?(schedule, current_schedule) ->
        {:error, "delivery installation schedule is immutable after installation is recorded"}

      not Value.py_eq?(completion, current_completion) ->
        {:error, "delivery installation completion is immutable after installation is recorded"}

      status == "complete" ->
        cond do
          not is_nil(current_closeout) ->
            {:error, "delivery closeout is already recorded"}

          is_nil(closeout) ->
            {:error, "record delivery closeout before marking delivery Complete"}

          Map.get(closeout, "updatedAt") != Map.get(payload, "updatedAt") ->
            {:error, "delivery closeout updatedAt must match delivery execution updatedAt"}

          true ->
            Provenance.check(closeout, principal, true, installation_messages("closeout"))
        end

      not Value.py_eq?(closeout, current_closeout) ->
        {:error, "delivery closeout can only be recorded with the Complete transition"}

      true ->
        :ok
    end
  end

  defp ready_transition(payload, schedule, completion, current_schedule, status, principal) do
    cond do
      not is_nil(completion) ->
        {:error, "enter Scheduled before authoring installation completion"}

      status == "scheduled" ->
        cond do
          is_nil(current_schedule) ->
            {:error, "save delivery installation schedule before moving work to Scheduled"}

          not Value.py_eq?(schedule, current_schedule) ->
            {:error,
             "delivery installation schedule must be saved before the Scheduled transition"}

          true ->
            :ok
        end

      status != "readyForInstallation" or Value.py_eq?(schedule, current_schedule) ->
        :ok

      is_nil(schedule) ->
        {:error, "delivery installation schedule cannot be cleared"}

      true ->
        with :ok <-
               installation_revision_step(
                 payload,
                 schedule,
                 current_schedule,
                 "delivery installation schedule"
               ),
             :ok <- require_installer_assignment(schedule) do
          Provenance.check(schedule, principal, true, installation_messages("schedule"))
        end
    end
  end

  defp recording_installation(schedule, completion, current_schedule, current_completion) do
    cond do
      is_nil(current_schedule) ->
        {:error, "delivery installation schedule is required before recording installation"}

      is_nil(current_completion) ->
        {:error, "save installation completion evidence before marking delivery Installed"}

      not Value.py_eq?(schedule, current_schedule) ->
        {:error, "delivery installation schedule must be saved before recording installation"}

      not Value.py_eq?(completion, current_completion) ->
        {:error, "delivery installation completion must be saved before recording installation"}

      true ->
        :ok
    end
  end

  defp scheduled_transition(
         payload,
         schedule,
         completion,
         current_schedule,
         current_completion,
         principal
       ) do
    schedule_changed = not Value.py_eq?(schedule, current_schedule)
    completion_changed = not Value.py_eq?(completion, current_completion)

    cond do
      schedule_changed and completion_changed ->
        {:error,
         "delivery installation may revise either schedule or completion per mutation, not both"}

      schedule_changed ->
        cond do
          not is_nil(current_completion) ->
            {:error,
             "delivery installation schedule is frozen once completion evidence is recorded"}

          is_nil(schedule) ->
            {:error, "delivery installation schedule cannot be cleared"}

          true ->
            with :ok <-
                   installation_revision_step(
                     payload,
                     schedule,
                     current_schedule,
                     "delivery installation schedule"
                   ),
                 :ok <- require_installer_assignment(schedule) do
              Provenance.check(schedule, principal, true, installation_messages("schedule"))
            end
        end

      not completion_changed ->
        :ok

      is_nil(completion) ->
        {:error, "delivery installation completion cannot be cleared"}

      true ->
        with :ok <-
               installation_revision_step(
                 payload,
                 completion,
                 current_completion,
                 "delivery installation completion"
               ) do
          Provenance.check(completion, principal, true, installation_messages("completion"))
        end
    end
  end

  # The pre-transition freeze, `app/delivery_installation.py:311-322`: while
  # Scheduled with completion evidence already recorded, the schedule may not
  # move at all.
  defp schedule_frozen_by_completion(nil, _schedule, _principal), do: :ok

  defp schedule_frozen_by_completion(current_payload, schedule, principal) do
    if Map.get(current_payload, "status") == "scheduled" and
         not is_nil(Map.get(current_payload, "installationCompletion")) do
      with {:ok, current_schedule} <-
             validate_schedule(
               Map.get(current_payload, "installationSchedule"),
               Map.get(current_payload, "productionPlan"),
               principal
             ) do
        if Value.py_eq?(schedule, current_schedule) do
          :ok
        else
          {:error,
           "delivery installation schedule is frozen once completion evidence is recorded"}
        end
      end
    else
      :ok
    end
  end

  defp installation_messages(label) do
    {
      "delivery installation #{label} provenance is incomplete",
      "delivery installation #{label} organization is invalid",
      "delivery installation #{label} does not match authenticated provenance"
    }
  end

  # `_validate_schedule`, `app/delivery_installation.py:109-161`.
  defp validate_schedule(nil, _production_plan, _principal), do: {:ok, nil}

  defp validate_schedule(schedule, _production_plan, _principal) when not object?(schedule),
    do: {:error, "delivery installation schedule is malformed"}

  defp validate_schedule(schedule, production_plan, principal) do
    cond do
      not object?(production_plan) ->
        {:error, "delivery installation schedule requires a frozen production plan"}

      not production_ready?(production_plan) ->
        {:error, "delivery installation schedule requires completed Production work"}

      true ->
        with :ok <- positive_revision(schedule, "delivery installation schedule"),
             :ok <-
               frozen_reference(
                 schedule,
                 "productionPlanRevision",
                 production_plan,
                 "delivery installation schedule production revision does not match frozen Production"
               ),
             :ok <-
               required_string(
                 schedule,
                 "scheduledFor",
                 "delivery installation scheduledFor is required"
               ),
             :ok <-
               required_string(
                 schedule,
                 "updatedAt",
                 "delivery installation schedule updatedAt is required"
               ),
             :ok <- validate_installer_assignment(Map.get(schedule, "installerAssignment")),
             :ok <-
               optional_string(schedule, "note", "delivery installation schedule note is invalid") do
          validated(
            schedule,
            Provenance.check(schedule, principal, false, installation_messages("schedule"))
          )
        end
    end
  end

  # `_production_ready`, `app/delivery_installation.py:95-107`.
  defp production_ready?(production_plan) do
    steps = Map.get(production_plan, "steps")

    is_list(steps) and steps != [] and
      Enum.all?(steps, fn step -> object?(step) and Map.get(step, "status") == "complete" end)
  end

  # `_validate_installer_assignment`, `app/delivery_installation.py:63-86`.
  defp validate_installer_assignment(nil), do: :ok

  defp validate_installer_assignment(value) when not object?(value) do
    {:error, "delivery installation installer assignment is malformed"}
  end

  defp validate_installer_assignment(value) do
    cond do
      is_nil(Value.normalized(Map.get(value, "id"))) ->
        {:error, "delivery installation installer assignment id is required"}

      is_nil(Value.normalized(Map.get(value, "displayName"))) ->
        {:error, "delivery installation installer displayName is required"}

      true ->
        case Map.get(value, "phone") do
          nil ->
            :ok

          phone ->
            if is_nil(Value.normalized(phone)) do
              {:error, "delivery installation installer phone is invalid"}
            else
              :ok
            end
        end
    end
  end

  defp require_installer_assignment(schedule) do
    if is_nil(Map.get(schedule, "installerAssignment")) do
      {:error,
       "delivery installation installer assignment is required for a new schedule revision"}
    else
      :ok
    end
  end

  # `_validate_completion`, `app/delivery_installation.py:196-247`.
  defp validate_completion(nil, _schedule, _principal), do: {:ok, nil}

  defp validate_completion(completion, _schedule, _principal) when not object?(completion),
    do: {:error, "delivery installation completion is malformed"}

  defp validate_completion(completion, schedule, principal) do
    if is_nil(schedule) do
      {:error, "delivery installation completion requires a saved installation schedule"}
    else
      with :ok <- positive_revision(completion, "delivery installation completion"),
           :ok <-
             frozen_reference(
               completion,
               "installationScheduleRevision",
               schedule,
               "delivery installation completion schedule revision does not match saved schedule"
             ),
           :ok <-
             required_string(
               completion,
               "completedAt",
               "delivery installation completedAt is required"
             ),
           :ok <-
             required_string(
               completion,
               "updatedAt",
               "delivery installation completion updatedAt is required"
             ),
           :ok <-
             optional_string(
               completion,
               "note",
               "delivery installation completion note is invalid"
             ),
           {:ok, _references} <-
             validated_references(Map.get(completion, "evidenceReferences"), "completion", true) do
        validated(
          completion,
          Provenance.check(completion, principal, false, installation_messages("completion"))
        )
      end
    end
  end

  # `_validate_closeout`, `app/delivery_installation.py:250-294`.
  defp validate_closeout(nil, _completion, _principal), do: {:ok, nil}

  defp validate_closeout(closeout, _completion, _principal) when not object?(closeout),
    do: {:error, "delivery closeout is malformed"}

  defp validate_closeout(closeout, completion, principal) do
    cond do
      is_nil(completion) ->
        {:error, "delivery closeout requires recorded installation completion"}

      Value.py_ne?(Map.get(closeout, "revision"), 1) or
          is_boolean(Map.get(closeout, "revision")) ->
        {:error, "delivery closeout revision must be exactly one"}

      true ->
        closeout_body(closeout, completion, principal)
    end
  end

  defp closeout_body(closeout, completion, principal) do
    revision = Map.get(closeout, "installationCompletionRevision")

    if not Value.int?(revision) or not Value.py_eq?(revision, Map.get(completion, "revision")) do
      {:error, "delivery closeout completion revision does not match installed completion"}
    else
      with :ok <- required_string(closeout, "closedAt", "delivery closeout closedAt is required"),
           :ok <-
             required_string(closeout, "updatedAt", "delivery closeout updatedAt is required"),
           :ok <- optional_string(closeout, "note", "delivery closeout note is invalid"),
           {:ok, references} <-
             validated_references(Map.get(closeout, "evidenceReferences"), "closeout", false) do
        if references == [] and is_nil(Value.normalized(Map.get(closeout, "note"))) do
          {:error, "delivery closeout requires a final note or at least one evidence reference"}
        else
          validated(
            closeout,
            Provenance.check(closeout, principal, false, installation_messages("closeout"))
          )
        end
      end
    end
  end

  # `_validated_references`, `app/delivery_installation.py:164-193`.
  defp validated_references(references, label, require_nonempty) do
    suffix =
      if require_nonempty,
        do: "delivery installation #{label} requires at least one evidence reference",
        else: "delivery installation #{label} evidence references are malformed"

    if is_list(references) and (not require_nonempty or references != []) do
      collect_references(references, label, MapSet.new(), [])
    else
      {:error, suffix}
    end
  end

  defp collect_references([], _label, _seen, acc), do: {:ok, Enum.reverse(acc)}

  defp collect_references([reference | rest], label, seen, acc) do
    case reference_shape(reference, label) do
      :ok ->
        key = {Map.get(reference, "itemID"), Map.get(reference, "documentID")}

        if MapSet.member?(seen, key) do
          {:error, "delivery installation #{label} evidence references must be unique"}
        else
          collect_references(rest, label, MapSet.put(seen, key), [reference | acc])
        end

      {:error, _message} = error ->
        error
    end
  end

  defp reference_shape(reference, label) do
    cond do
      not object?(reference) ->
        {:error, "delivery installation #{label} evidence reference is malformed"}

      is_nil(Value.normalized(Map.get(reference, "itemID"))) or
          is_nil(Value.normalized(Map.get(reference, "documentID"))) ->
        {:error,
         "delivery installation #{label} evidence reference requires itemID and documentID"}

      true ->
        :ok
    end
  end

  # ---------------------------------------------------------------------------
  # shared field helpers
  # ---------------------------------------------------------------------------

  defp freeze(proposed, current, message) do
    if Value.py_ne?(proposed, current), do: {:error, message}, else: :ok
  end

  defp reject_unless_nil(plan, message) do
    if is_nil(plan), do: :ok, else: {:error, message}
  end

  # `expected_revision = 1 if current is None else current["revision"] + 1`,
  # then the timestamp anchor, then the current-actor provenance check.
  defp revision_step(payload, plan, current, label, principal, messages) do
    with :ok <- revision_number(plan, current, label),
         :ok <- timestamp_anchor(payload, plan, label) do
      Provenance.check(plan, principal, true, messages)
    end
  end

  # The installation reference runs the same two rules but inserts the installer
  # assignment requirement between the timestamp anchor and the provenance
  # check, so it does those two itself and this only covers the shared part.
  defp installation_revision_step(payload, plan, current, label) do
    with :ok <- revision_number(plan, current, label),
         :ok <- timestamp_anchor(payload, plan, label) do
      :ok
    end
  end

  defp timestamp_anchor(payload, plan, label) do
    if Map.get(plan, "updatedAt") == Map.get(payload, "updatedAt") do
      :ok
    else
      {:error, "#{label} updatedAt must match delivery execution updatedAt"}
    end
  end

  defp revision_number(plan, nil, label) do
    if Map.get(plan, "revision") == 1 do
      :ok
    else
      {:error, "#{label} revision must advance by exactly one"}
    end
  end

  defp revision_number(plan, current, label) do
    expected = Map.get(current, "revision") + 1

    if Map.get(plan, "revision") == expected do
      :ok
    else
      {:error, "#{label} revision must advance by exactly one"}
    end
  end

  defp positive_revision(value, label) do
    revision = Map.get(value, "revision")

    if Value.int?(revision) and revision >= 1 do
      :ok
    else
      {:error, "#{label} revision is invalid"}
    end
  end

  defp required_string(value, key, message) do
    if is_nil(Value.normalized(Map.get(value, key))), do: {:error, message}, else: :ok
  end

  # `note is not None and not isinstance(note, str)` -- absent and null both pass.
  defp optional_string(value, key, message) do
    case Map.get(value, key) do
      nil -> :ok
      note when is_binary(note) -> :ok
      _other -> {:error, message}
    end
  end

  defp non_empty_list(value, key, message) do
    case Map.get(value, key) do
      list when is_list(list) and list != [] -> {:ok, list}
      _other -> {:error, message}
    end
  end

  defp decimal(value, name) do
    case Decimal.parse(value) do
      {:ok, decimal} -> {:ok, decimal}
      :error -> {:error, "#{name} is invalid"}
    end
  end

  defp frozen_reference(value, key, source, message) do
    revision = Map.get(value, key)

    if Value.int?(revision) and revision >= 1 and
         Value.py_eq?(revision, Map.get(source, "revision")) do
      :ok
    else
      {:error, message}
    end
  end

  defp unique_ids(entries, key, label) do
    Enum.reduce_while(entries, {:ok, MapSet.new()}, fn entry, {:ok, seen} ->
      id = if object?(entry), do: Value.normalized(Map.get(entry, key)), else: nil

      cond do
        is_nil(id) ->
          {:halt, {:error, "#{label} id is required"}}

        MapSet.member?(seen, id) ->
          {:halt, {:error, "#{label} ids must be unique"}}

        true ->
          {:cont, {:ok, MapSet.put(seen, id)}}
      end
    end)
  end

  defp required_id(entry, key, message) do
    case Value.normalized(Map.get(entry, key)) do
      nil -> {:error, message}
      id -> {:ok, id}
    end
  end
end
