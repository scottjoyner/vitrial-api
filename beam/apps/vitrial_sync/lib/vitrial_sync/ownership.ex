defmodule VitrialSync.Ownership do
  @moduledoc """
  S-61, admission step 7b: generic record authorization and its ownership plan.

  `authorize_record` (`app/ownership.py:357-796`) followed by
  `apply_ownership_plan` (`765-901`), reached from
  `app/sync_service.py:363-374`. This is the path every entity type takes
  **except** `delivery_execution`, which is refused here and authorized by
  `VitrialSync.Delivery` instead (S-60, SUSPECT-6).

  ## Two halves, and why they are separated

  `authorize/8` is a pure chain of questions about the world and returns a plan:
  which canonical table this record belongs in, which parent it hangs from, and
  whether it is a create or an update. It writes nothing.

  `apply_plan/2` is the write side, and it re-checks immutability with slightly
  different wording (S-118) because the check is against the row as it is *now*,
  after everything the authority path decided. It returns the rows it would write
  as data, because a pure function cannot write, and returns the scope grant the
  reference's `_grant_created_scope` would apply as data too -- that is how a
  self-created Customer becomes visible to the rest of the same batch.

  ## Immutability is checked only when the client supplies the field

  S-106, and it is the single most misread rule in this file. Every parent check
  reads `supplied = payload.get(key)` and refuses only `if supplied is not None and
  supplied != canonical`. A client may **omit** `projectID` on an update and the
  server keeps the canonical value. Omitting is not a way to *change* the parent;
  it is a way not to restate it. Tightening this to "the key must be present" would
  reject every client that updates a payload incrementally, which is most of them.

  ## The two ownership tables

  `CanonicalProjectChild` holds `quotation` and `delivery_execution`.
  `CanonicalItemChild` holds the six item sidecars plus `item_audit_event`. Two of
  those are append-only (S-110): `item_audit_event` and `configuration_version`
  can be created once and never updated or tombstoned.

  ## Design-snapshot binding

  A quotation line may carry a `designSnapshot`, which must be bound to the
  canonical frozen `ConfigurationVersion` it names. The shape of both snapshots
  is validated before this step runs -- in the reference by
  `validate_assembly_contract_mutation` (`app/sync_service.py:319-323`,
  `app/assembly_contract.py`, validator at `:294-330`) -- so
  `bind_design_snapshot/2` here works on already validated objects and compares
  them directly. That ordering is a precondition, not an accident: re-validating
  the shapes here would be a second copy of a contract that the reference
  validates once. On the BEAM, however, NOTHING yet establishes that
  precondition -- the validator is unported (tracked as VITR-V015 in
  `trackers/vitrial-api-TRACKER.md`; the gap is named in `VitrialSync.Batch`'s
  step table, and `drift_test.exs` pins the reference's decode -> validate
  order) -- so this module is faithful to the reference's *order*, not yet to
  its *guarantee*.
  """

  alias VitrialSync.{Canonical, Provenance, Value}

  # `isinstance(x, dict)` sits in guard position in the Python, and the guards
  # below mirror it. A guard may not call a function -- `Value.object?/1` is one
  # -- so the one-line body is written out once here as a guard macro.
  defguardp object?(value) when is_map(value) and not is_struct(value)

  @item_child_capabilities %{
    "measurement" => "item.measurements.manage",
    "evidence" => "item.evidence.manage",
    "customer_requirement" => "item.requirements.manage",
    "configuration" => "item.configuration.manage",
    "configuration_version" => "item.configuration.manage",
    "blocker" => "item.blockers.manage"
  }

  @item_child_types MapSet.new(Map.keys(@item_child_capabilities) ++ ["item_audit_event"])
  @append_only_types MapSet.new(["item_audit_event", "configuration_version"])
  @project_child_types MapSet.new(["quotation", "delivery_execution"])

  @quotation_capability_by_status %{
    "sent" => "quotation.send",
    "approved" => "quotation.approve",
    "draft" => "quotation.create",
    "ready" => "quotation.create",
    "rejected" => "quotation.create",
    "superseded" => "quotation.create"
  }

  defmodule Plan do
    @moduledoc """
    `OwnershipPlan`, `app/ownership.py:58-70`.

    A frozen dataclass in the reference; a struct here because the plan crosses a
    function boundary and a bare map would lose its shape. `is_create` is named
    rather than inferred from a nil parent, because "the canonical row is absent"
    and "the canonical row exists with a null parent" are different situations.
    """
    @type t :: %__MODULE__{
            entity_type: String.t(),
            entity_id: String.t(),
            is_create: boolean(),
            deleted_at: term(),
            customer_id: String.t() | nil,
            project_id: String.t() | nil,
            project_sector_id: String.t() | nil,
            sector_id: String.t() | nil,
            item_id: String.t() | nil
          }

    defstruct [
      :entity_type,
      :entity_id,
      :deleted_at,
      :customer_id,
      :project_id,
      :project_sector_id,
      :sector_id,
      :item_id,
      is_create: false
    ]
  end

  @doc """
  `authorize_record/9`, `app/ownership.py:357-796`.

  `generic_entity_exists` is `current is not None` in the reference and is a
  separate argument here rather than being read from the world, because the
  admission context already carries it and a second source of truth for "does
  this record exist" is how a create gets mistaken for an update.

  Returns `{:ok, plan}` or `{:error, message}`.

      iex> alias VitrialSync.{Canonical, Ownership}
      iex> principal = %Canonical.Principal{organization_id: "org", capabilities: MapSet.new()}
      iex> scope = Canonical.Scope.from_principal(principal)
      iex> Ownership.authorize("customer", "c1", %{}, nil, principal, scope, false, Canonical.empty_world())
      {:error, "customer.create capability required"}
  """
  @spec authorize(
          String.t(),
          String.t(),
          map(),
          term(),
          Canonical.Principal.t(),
          Canonical.Scope.t(),
          boolean(),
          Canonical.World.t()
        ) :: {:ok, Plan.t()} | {:error, String.t()}
  def authorize(
        entity_type,
        entity_id,
        payload,
        deleted_at,
        principal,
        scope,
        generic_entity_exists,
        world
      ) do
    deleting = not is_nil(deleted_at)

    case entity_type do
      "customer" ->
        authorize_customer(
          entity_id,
          payload,
          deleting,
          principal,
          scope,
          generic_entity_exists,
          world,
          deleted_at
        )

      "project" ->
        authorize_project(
          entity_id,
          payload,
          deleting,
          principal,
          scope,
          generic_entity_exists,
          world,
          deleted_at
        )

      "project_sector" ->
        authorize_project_sector(
          entity_id,
          payload,
          deleting,
          principal,
          scope,
          generic_entity_exists,
          world,
          deleted_at
        )

      "item" ->
        authorize_item(
          entity_id,
          payload,
          deleting,
          principal,
          scope,
          generic_entity_exists,
          world,
          deleted_at
        )

      "quotation" ->
        authorize_quotation(
          entity_id,
          payload,
          deleting,
          principal,
          scope,
          generic_entity_exists,
          world,
          deleted_at
        )

      other ->
        authorize_other(
          other,
          entity_id,
          payload,
          deleting,
          principal,
          scope,
          generic_entity_exists,
          world,
          deleted_at
        )
    end
  end

  # An unknown type is not an item child. `app/ownership.py:788-796` falls off
  # the end of its chain and says why: "Falling through denies the write, which
  # is the direction to fail." A `delivery_execution` that somehow reached this
  # path -- S-60 dispatches it elsewhere, and SUSPECT-6 records what drifted when
  # it did not -- is refused by the same arm.
  defp authorize_other(
         entity_type,
         entity_id,
         payload,
         deleting,
         principal,
         scope,
         generic_exists,
         world,
         deleted_at
       ) do
    if MapSet.member?(@item_child_types, entity_type) do
      authorize_item_child(
        entity_type,
        entity_id,
        payload,
        deleting,
        principal,
        scope,
        generic_exists,
        world,
        deleted_at
      )
    else
      {:error, "canonical authorization is not implemented for #{entity_type}"}
    end
  end

  # ---------------------------------------------------------------------------
  # customer -- app/ownership.py:369-390
  # ---------------------------------------------------------------------------

  defp authorize_customer(
         entity_id,
         payload,
         deleting,
         principal,
         scope,
         generic_exists,
         world,
         deleted_at
       ) do
    case Canonical.customer(world, entity_id) do
      nil ->
        with :ok <- absent_canonical("Customer", generic_exists, deleting),
             :ok <- capability(principal, "customer.create"),
             :ok <- identity(payload, entity_id, false) do
          {:ok,
           %Plan{
             entity_type: "customer",
             entity_id: entity_id,
             is_create: true,
             deleted_at: deleted_at,
             customer_id: entity_id
           }}
        end

      canonical ->
        with :ok <- capability(principal, "customer.edit"),
             :ok <- in_scope_customer(scope, canonical),
             :ok <- identity(payload, entity_id, deleting) do
          with :ok <- delete_safe_customer(world, canonical, deleting) do
            {:ok,
             %Plan{
               entity_type: "customer",
               entity_id: entity_id,
               is_create: false,
               deleted_at: deleted_at,
               customer_id: canonical.customer_id
             }}
          end
        end
    end
  end

  defp in_scope_customer(scope, canonical) do
    if Canonical.Scope.can_access_customer?(scope, canonical.customer_id) do
      :ok
    else
      {:error, "Customer is outside authorized scope"}
    end
  end

  defp delete_safe_customer(_world, _canonical, false), do: :ok

  defp delete_safe_customer(world, canonical, true) do
    if Canonical.customer_has_live_projects?(world, canonical.customer_id) do
      {:error, "Customer cannot be deleted while Projects remain active"}
    else
      :ok
    end
  end

  # ---------------------------------------------------------------------------
  # project -- app/ownership.py:392-419
  # ---------------------------------------------------------------------------

  defp authorize_project(
         entity_id,
         payload,
         deleting,
         principal,
         scope,
         generic_exists,
         world,
         deleted_at
       ) do
    case Canonical.project(world, entity_id) do
      nil ->
        with :ok <- absent_canonical("Project", generic_exists, deleting),
             :ok <- capability(principal, "project.create"),
             :ok <- identity(payload, entity_id, false),
             {:ok, customer_id} <- value(payload, "customerID"),
             :ok <- active_customer(world, customer_id),
             :ok <- customer_in_scope(scope, customer_id) do
          {:ok,
           %Plan{
             entity_type: "project",
             entity_id: entity_id,
             is_create: true,
             deleted_at: deleted_at,
             customer_id: customer_id,
             project_id: entity_id
           }}
        end

      canonical ->
        with :ok <- capability(principal, "project.edit"),
             {:ok, _canonical_project} <- project_for_scope(world, scope, canonical.project_id),
             :ok <- identity(payload, entity_id, deleting),
             :ok <-
               immutable(
                 payload,
                 "customerID",
                 canonical.customer_id,
                 "Project Customer ownership is immutable"
               ) do
          with :ok <- delete_safe_project(world, canonical.project_id, deleting) do
            {:ok,
             %Plan{
               entity_type: "project",
               entity_id: entity_id,
               is_create: false,
               deleted_at: deleted_at,
               customer_id: canonical.customer_id,
               project_id: canonical.project_id
             }}
          end
        end
    end
  end

  defp active_customer(world, customer_id) do
    case Canonical.customer(world, customer_id) do
      %Canonical.Customer{deleted_at: nil} -> :ok
      _other -> {:error, "Project parent Customer is not canonical and active"}
    end
  end

  defp customer_in_scope(scope, customer_id) do
    if Canonical.Scope.can_access_customer?(scope, customer_id) do
      :ok
    else
      {:error, "Project parent Customer is outside authorized scope"}
    end
  end

  defp delete_safe_project(_world, _project_id, false), do: :ok

  defp delete_safe_project(world, project_id, true) do
    if Canonical.project_has_live_children?(world, project_id) do
      {:error, "Project cannot be deleted while canonical children remain active"}
    else
      :ok
    end
  end

  # ---------------------------------------------------------------------------
  # project_sector -- app/ownership.py:421-455
  # ---------------------------------------------------------------------------

  defp authorize_project_sector(
         entity_id,
         payload,
         deleting,
         principal,
         scope,
         generic_exists,
         world,
         deleted_at
       ) do
    case Canonical.project_sector(world, entity_id) do
      nil ->
        with :ok <- absent_canonical("ProjectSector", generic_exists, deleting),
             :ok <- capability(principal, "project.edit"),
             :ok <- identity(payload, entity_id, false),
             {:ok, project_id} <- value(payload, "projectID"),
             {:ok, sector_id} <- value(payload, "sectorID"),
             {:ok, project} <- project_for_scope(world, scope, project_id) do
          {:ok,
           %Plan{
             entity_type: "project_sector",
             entity_id: entity_id,
             is_create: true,
             deleted_at: deleted_at,
             customer_id: project.customer_id,
             project_id: project.project_id,
             project_sector_id: entity_id,
             sector_id: sector_id
           }}
        end

      canonical ->
        with :ok <- capability(principal, "project.edit"),
             {:ok, project} <- project_for_scope(world, scope, canonical.project_id),
             :ok <- identity(payload, entity_id, deleting),
             :ok <-
               immutable(
                 payload,
                 "projectID",
                 canonical.project_id,
                 "ProjectSector Project ownership is immutable"
               ),
             :ok <-
               immutable(
                 payload,
                 "sectorID",
                 canonical.sector_id,
                 "ProjectSector Sector identity is immutable"
               ) do
          with :ok <- delete_safe_sector(world, canonical.project_sector_id, deleting) do
            {:ok,
             %Plan{
               entity_type: "project_sector",
               entity_id: entity_id,
               is_create: false,
               deleted_at: deleted_at,
               customer_id: project.customer_id,
               project_id: canonical.project_id,
               project_sector_id: canonical.project_sector_id,
               sector_id: canonical.sector_id
             }}
          end
        end
    end
  end

  defp delete_safe_sector(_world, _sector_id, false), do: :ok

  defp delete_safe_sector(world, sector_id, true) do
    if Canonical.project_sector_has_live_items?(world, sector_id) do
      {:error, "ProjectSector cannot be deleted while Items remain active"}
    else
      :ok
    end
  end

  # ---------------------------------------------------------------------------
  # item -- app/ownership.py:457-515
  # ---------------------------------------------------------------------------

  defp authorize_item(
         entity_id,
         payload,
         deleting,
         principal,
         scope,
         generic_exists,
         world,
         deleted_at
       ) do
    case Canonical.item(world, entity_id) do
      nil ->
        with :ok <- absent_canonical("Item", generic_exists, deleting),
             :ok <- capability(principal, "item.create"),
             :ok <- identity(payload, entity_id, false),
             {:ok, project_id} <- value(payload, "projectID"),
             {:ok, sector_id} <- value(payload, "projectSectorID"),
             {:ok, project} <- project_for_scope(world, scope, project_id),
             {:ok, sector} <-
               sector_of_project(
                 world,
                 sector_id,
                 project_id,
                 "Item ProjectSector is not canonical for its Project"
               ) do
          {:ok,
           %Plan{
             entity_type: "item",
             entity_id: entity_id,
             is_create: true,
             deleted_at: deleted_at,
             customer_id: project.customer_id,
             project_id: project.project_id,
             project_sector_id: sector.project_sector_id
           }}
        end

      canonical ->
        with :ok <- capability(principal, "item.edit"),
             :ok <- item_has_sector(canonical),
             {:ok, project} <- project_for_scope(world, scope, canonical.project_id),
             {:ok, _canonical_sector} <-
               sector_of_project(
                 world,
                 canonical.project_sector_id,
                 canonical.project_id,
                 "Item canonical ProjectSector is missing or inactive"
               ),
             :ok <- identity(payload, entity_id, deleting),
             :ok <-
               immutable(
                 payload,
                 "projectID",
                 canonical.project_id,
                 "Item Project ownership is immutable"
               ),
             :ok <-
               immutable(
                 payload,
                 "projectSectorID",
                 canonical.project_sector_id,
                 "Item ProjectSector ownership is immutable"
               ) do
          with :ok <- delete_safe_item(world, canonical.item_id, deleting) do
            {:ok,
             %Plan{
               entity_type: "item",
               entity_id: entity_id,
               is_create: false,
               deleted_at: deleted_at,
               customer_id: project.customer_id,
               project_id: canonical.project_id,
               project_sector_id: canonical.project_sector_id
             }}
          end
        end
    end
  end

  defp item_has_sector(%Canonical.Item{project_sector_id: nil}) do
    {:error, "existing Item lacks canonical ProjectSector ownership"}
  end

  defp item_has_sector(_canonical), do: :ok

  # S-112's other half. Reached only after `VitrialSync.ItemDeleteGuard` has
  # already cleared the audit history, so both halves have run.
  defp delete_safe_item(_world, _item_id, false), do: :ok

  defp delete_safe_item(world, item_id, true) do
    if Canonical.item_has_live_sidecars?(world, item_id) do
      {:error, "Item cannot be deleted while sidecar/history records remain active"}
    else
      :ok
    end
  end

  defp sector_of_project(world, sector_id, project_id, message) do
    case Canonical.project_sector(world, sector_id) do
      %Canonical.ProjectSector{deleted_at: nil, project_id: ^project_id} = sector ->
        {:ok, sector}

      _other ->
        {:error, message}
    end
  end

  # ---------------------------------------------------------------------------
  # item children -- app/ownership.py:517-659
  # ---------------------------------------------------------------------------

  defp authorize_item_child(
         entity_type,
         entity_id,
         payload,
         deleting,
         principal,
         scope,
         generic_exists,
         world,
         deleted_at
       ) do
    canonical = Canonical.item_child(world, entity_type, entity_id)

    # Both guards are conditional on the CANONICAL row being absent
    # (`app/ownership.py:597-598`: `if canonical is None and generic_entity_exists`,
    # `if canonical is None and deleting`). Unlike the customer/project arms,
    # where the guard runs only inside the nil branch, this arm splits later, so
    # the nil-conditions travel WITH the arguments: an update of an existing
    # child -- SyncEntity row present, canonical child present -- must pass, and
    # firing on `generic_exists` alone refused every one of them.
    with :ok <- append_only_guard(entity_type, canonical, generic_exists, deleting),
         :ok <-
           absent_canonical(
             entity_type,
             is_nil(canonical) and generic_exists,
             deleting and is_nil(canonical)
           ),
         :ok <- identity(payload, entity_id, deleting),
         {:ok, item_id} <- resolve_item_id(entity_type, entity_id, payload, canonical, world),
         {:ok, item, project} <- item_for_scope(world, scope, item_id),
         :ok <- item_child_capability(entity_type, payload, principal, item_id, item, deleting) do
      with :ok <- item_child_references(entity_type, entity_id, payload, item_id, world, deleting),
           :ok <-
             configuration_status_capability(
               world,
               entity_type,
               entity_id,
               payload,
               principal,
               deleting
             ),
           :ok <- blocker_history(entity_type, entity_id, payload, item_id, principal, deleting) do
        {:ok,
         %Plan{
           entity_type: entity_type,
           entity_id: entity_id,
           is_create: is_nil(canonical),
           deleted_at: deleted_at,
           customer_id: project.customer_id,
           project_id: project.project_id,
           project_sector_id: item.project_sector_id,
           item_id: item_id
         }}
      end
    end
  end

  # S-110.
  defp append_only_guard(entity_type, canonical, generic_exists, _deleting) do
    if MapSet.member?(@append_only_types, entity_type) do
      if not is_nil(canonical) or generic_exists do
        {:error, "#{entity_type} history is append-only"}
      else
        :ok
      end
    else
      :ok
    end
  end

  # S-110, create half: a `configuration_version` carries its configuration
  # nested, so the item comes from inside the configuration rather than from the
  # envelope, and the base configuration must already be a canonical child of the
  # same item.
  defp resolve_item_id("configuration_version", entity_id, payload, nil, world) do
    case Map.get(payload, "configuration") do
      configuration when not object?(configuration) ->
        {:error, "configuration_version payload is malformed"}

      configuration ->
        with {:ok, item_id} <- value(configuration, "itemID"),
             {:ok, configuration_id} <- value(configuration, "id"),
             :ok <- valid_version(configuration),
             :ok <- version_identity(entity_id, configuration_id, configuration) do
          base_configuration(world, configuration_id, item_id)
        end
    end
  end

  defp resolve_item_id(_entity_type, _entity_id, payload, nil, _world),
    do: value(payload, "itemID")

  defp resolve_item_id(entity_type, _entity_id, payload, canonical, _world) do
    supplied_item = supplied_item_id(entity_type, payload)

    if not is_nil(supplied_item) and not Value.py_eq?(supplied_item, canonical.item_id) do
      {:error, "#{entity_type} Item ownership is immutable"}
    else
      {:ok, canonical.item_id}
    end
  end

  defp base_configuration(world, configuration_id, item_id) do
    case Canonical.active_item_child(world, "configuration", configuration_id) do
      %Canonical.ItemChild{item_id: ^item_id} -> {:ok, item_id}
      _other -> {:error, "configuration_version base configuration is not canonical"}
    end
  end

  defp supplied_item_id("configuration_version", payload) do
    case Map.get(payload, "configuration") do
      configuration when object?(configuration) -> Map.get(configuration, "itemID")
      _other -> nil
    end
  end

  defp supplied_item_id(_entity_type, payload), do: Map.get(payload, "itemID")

  defp valid_version(configuration) do
    version = Map.get(configuration, "version")

    if Value.int?(version) and version >= 1 do
      :ok
    else
      {:error, "configuration_version version is invalid"}
    end
  end

  defp version_identity(entity_id, configuration_id, configuration) do
    if entity_id == "#{configuration_id}#v#{Map.get(configuration, "version")}" do
      :ok
    else
      {:error, "configuration_version identity is invalid"}
    end
  end

  defp item_child_capability("item_audit_event", payload, principal, item_id, item, _deleting) do
    with :ok <- audit_capability(payload, principal),
         :ok <-
           audit_snapshot(
             Map.get(payload, "after"),
             item_id,
             item,
             "resulting",
             "item audit resulting Item identity is invalid",
             "item audit parent relationship is not canonical"
           ),
         :ok <- audit_before(payload, item_id, item) do
      Provenance.require_current(payload, principal)
    end
  end

  defp item_child_capability(entity_type, _payload, principal, _item_id, _item, _deleting) do
    capability(principal, Map.fetch!(@item_child_capabilities, entity_type))
  end

  # S-109: the capability is derived from the event `kind`, not fixed per type.
  defp audit_capability(payload, principal) do
    case Map.get(payload, "kind") do
      "created" ->
        capability(principal, "item.create")

      "updated" ->
        capability(principal, "item.edit")

      "commercial_readiness_changed" ->
        with :ok <- capability(principal, "item.readiness.change") do
          # `payload.get("readinessOverrideUsed") is True` -- an identity test
          # against the singleton, not a truthiness test.
          if Map.get(payload, "readinessOverrideUsed") === true do
            capability(principal, "item.readiness.override")
          else
            :ok
          end
        end

      _other ->
        {:error, "item audit kind is invalid"}
    end
  end

  defp audit_snapshot(snapshot, item_id, item, _which, identity_message, parent_message) do
    cond do
      not Value.object?(snapshot) or Map.get(snapshot, "id") != item_id ->
        {:error, identity_message}

      Map.get(snapshot, "projectID") != item.project_id or
          Map.get(snapshot, "projectSectorID") != item.project_sector_id ->
        {:error, parent_message}

      true ->
        :ok
    end
  end

  defp audit_before(payload, item_id, item) do
    case Map.get(payload, "before") do
      nil ->
        :ok

      before ->
        audit_snapshot(
          before,
          item_id,
          item,
          "prior",
          "item audit prior Item identity is invalid",
          "item audit prior parent relationship is not canonical"
        )
    end
  end

  # S-107 / S-108 / the evidence delete guard, `app/ownership.py:599-629, 647-651`.
  defp item_child_references(entity_type, entity_id, payload, item_id, world, deleting) do
    case {entity_type, deleting} do
      {"measurement", false} ->
        require_references(payload, item_id, "evidenceReferences", "evidence", world)

      {"customer_requirement", false} ->
        require_references(payload, item_id, "evidenceReferenceIDs", "evidence", world)

      {type, false} when type in ["configuration", "configuration_version"] ->
        configuration_references(type, payload, item_id, world)

      {"evidence", true} ->
        if Canonical.evidence_referenced?(world, entity_id) do
          {:error, "Evidence cannot be deleted while referenced"}
        else
          :ok
        end

      _other ->
        :ok
    end
  end

  defp require_references(payload, item_id, key, reference_type, world) do
    case Map.get(payload, key, []) do
      references when is_list(references) ->
        Enum.reduce_while(references, :ok, fn reference, :ok ->
          case item_references_ok?(reference, item_id, reference_type, world) do
            :ok -> {:cont, :ok}
            error -> {:halt, error}
          end
        end)

      _other ->
        {:error, "invalid #{reference_type} reference"}
    end
  end

  defp item_references_ok?(reference, item_id, reference_type, world) do
    cond do
      not is_binary(reference) or reference == "" ->
        {:error, "invalid #{reference_type} reference"}

      true ->
        case Canonical.active_item_child(world, reference_type, reference) do
          %Canonical.ItemChild{item_id: ^item_id} -> :ok
          _other -> {:error, "#{reference_type} reference is not canonical for the same Item"}
        end
    end
  end

  defp configuration_references(entity_type, payload, item_id, world) do
    configuration =
      if entity_type == "configuration",
        do: payload,
        else: Map.get(payload, "configuration")

    if not Value.object?(configuration) do
      {:error, "configuration payload is malformed"}
    else
      with {:ok, requirements} <- reference_list(configuration, "customerRequirements"),
           {:ok, blockers} <- reference_list(configuration, "blockerIDs") do
        with :ok <-
               require_references(
                 %{"customerRequirements" => requirements},
                 item_id,
                 "customerRequirements",
                 "customer_requirement",
                 world
               ) do
          require_references(%{"blockerIDs" => blockers}, item_id, "blockerIDs", "blocker", world)
        end
      end
    end
  end

  defp reference_list(configuration, key) do
    case Map.get(configuration, key, []) do
      list when is_list(list) -> {:ok, list}
      _other -> {:error, "configuration references are invalid"}
    end
  end

  # The `item.configuration.finalize` / `item.configuration.reopen` pair.
  defp configuration_status_capability(world, entity_type, entity_id, payload, principal, false)
       when entity_type in ["configuration", "configuration_version"] do
    configuration =
      if entity_type == "configuration",
        do: payload,
        else: Map.get(payload, "configuration")

    with :ok <- require_object(configuration, "configuration payload is malformed") do
      status = Map.get(configuration, "status")
      current_id = Map.get(configuration, "id", entity_id)
      current_status = current_configuration_status(world, current_id)

      cond do
        status == "complete" and current_status != "complete" ->
          capability(principal, "item.configuration.finalize")

        current_status == "complete" and status != "complete" ->
          capability(principal, "item.configuration.reopen")

        true ->
          :ok
      end
    end
  end

  defp configuration_status_capability(
         _world,
         _entity_type,
         _entity_id,
         _payload,
         _principal,
         _deleting
       ),
       do: :ok

  # `_current_payload/4`, `app/ownership.py:245-256`: the stored payload if it is
  # an object, and `nil` otherwise.
  defp current_configuration_status(world, configuration_id) do
    case Canonical.entity(world, "configuration", configuration_id) do
      nil ->
        nil

      row ->
        case Canonical.payload_object(row) do
          nil -> nil
          payload -> Map.get(payload, "status")
        end
    end
  end

  # The `blocker` connected-history check, `app/ownership.py:632-651`. Distinct
  # from `VitrialSync.Lifecycle`: that one validates the state machine, this one
  # checks that the history is *connected* and *current*.
  defp blocker_history("blocker", entity_id, payload, item_id, principal, false) do
    with {:ok, events} <- blocker_events(payload),
         :ok <- connected_events(events, entity_id, item_id, principal) do
      Provenance.require_current(List.last(events), principal)
    end
  end

  defp blocker_history(_entity_type, _entity_id, _payload, _item_id, _principal, _deleting),
    do: :ok

  defp blocker_events(payload) do
    case Map.get(payload, "events") do
      events when is_list(events) and events != [] ->
        {:ok, events}

      _other ->
        {:error, "connected blocker history requires events"}
    end
  end

  defp connected_events(events, entity_id, item_id, principal) do
    Enum.reduce_while(events, :ok, fn event, :ok ->
      cond do
        not Value.object?(event) ->
          {:halt, {:error, "blocker event is malformed"}}

        Map.get(event, "blockerID") != entity_id or Map.get(event, "itemID") != item_id ->
          {:halt, {:error, "blocker event identity is invalid"}}

        Value.normalized(Map.get(event, "organizationID")) != principal.organization_id ->
          {:halt, {:error, "blocker event organization is invalid"}}

        is_nil(Map.get(event, "actorID")) or is_nil(Map.get(event, "membershipID")) or
          is_nil(Map.get(event, "sessionID")) or is_nil(Map.get(event, "authorizationRevision")) ->
          {:halt, {:error, "blocker event provenance is incomplete"}}

        true ->
          {:cont, :ok}
      end
    end)
  end

  # ---------------------------------------------------------------------------
  # quotation -- app/ownership.py:661-758
  # ---------------------------------------------------------------------------

  defp authorize_quotation(
         entity_id,
         payload,
         deleting,
         principal,
         scope,
         generic_exists,
         world,
         deleted_at
       ) do
    canonical = Canonical.project_child(world, "quotation", entity_id)

    with {:ok, project_id} <-
           quotation_project(canonical, entity_id, payload, deleting, generic_exists),
         {:ok, project} <- project_for_scope(world, scope, project_id),
         :ok <- quotation_body(entity_id, project, payload, deleting, principal, scope, world) do
      {:ok,
       %Plan{
         entity_type: "quotation",
         entity_id: entity_id,
         is_create: is_nil(canonical),
         deleted_at: deleted_at,
         customer_id: project.customer_id,
         project_id: project.project_id
       }}
    end
  end

  defp quotation_project(nil, entity_id, payload, deleting, generic_exists) do
    with :ok <- absent_canonical("quotation", generic_exists, deleting),
         :ok <- identity(payload, entity_id, false) do
      value(payload, "projectID")
    end
  end

  defp quotation_project(canonical, entity_id, payload, deleting, _generic_exists) do
    with :ok <- identity(payload, entity_id, deleting),
         :ok <-
           immutable(
             payload,
             "projectID",
             canonical.project_id,
             "Quotation Project ownership is immutable"
           ) do
      {:ok, canonical.project_id}
    end
  end

  defp quotation_body(entity_id, project, payload, deleting, principal, scope, world) do
    if deleting do
      # S-115: a tombstone needs only `quotation.create`. It is not blocked by the
      # content lock, and it is not blocked by the structural checks below.
      capability(principal, "quotation.create")
    else
      with :ok <- quotation_customer(payload, project),
           :ok <- quotation_capability(payload, principal),
           :ok <- quotation_lines(entity_id, project, payload, scope, world) do
        quotation_events(payload, principal)
      end
    end
  end

  defp quotation_customer(payload, project) do
    if Map.get(payload, "customerID") == project.customer_id do
      :ok
    else
      {:error, "Quotation Customer does not match canonical Project"}
    end
  end

  defp quotation_capability(payload, principal) do
    case Map.fetch(@quotation_capability_by_status, Map.get(payload, "status")) do
      {:ok, capability_name} -> capability(principal, capability_name)
      :error -> {:error, "Quotation status is invalid"}
    end
  end

  # S-116: lines must be non-empty, every line's item must be in scope and belong
  # to the same project, and a line carrying a design snapshot must be bound to
  # the frozen ConfigurationVersion it names.
  defp quotation_lines(_entity_id, project, payload, scope, world) do
    case Map.get(payload, "lines", []) do
      lines when is_list(lines) and lines != [] ->
        Enum.reduce_while(lines, :ok, fn line, :ok ->
          case quotation_line(line, project, scope, world) do
            :ok -> {:cont, :ok}
            error -> {:halt, error}
          end
        end)

      _other ->
        {:error, "Quotation must contain canonical Item lines"}
    end
  end

  defp quotation_line(line, project, scope, world) do
    if Value.object?(line) do
      with {:ok, item_id} <- value(line, "itemID"),
           {:ok, item, _item_project} <- item_for_scope(world, scope, item_id),
           :ok <- line_project(item, project),
           :ok <- design_snapshot(line, world) do
        :ok
      end
    else
      {:error, "Quotation line is malformed"}
    end
  end

  defp line_project(item, project) do
    if item.project_id == project.project_id do
      :ok
    else
      {:error, "Quotation line Item belongs to another Project"}
    end
  end

  defp design_snapshot(line, world) do
    case Map.get(line, "designSnapshot") do
      nil ->
        :ok

      snapshot when not object?(snapshot) ->
        {:error,
         "Quotation assembly design does not match canonical ConfigurationVersion: quotation line designSnapshot must be an object"}

      snapshot ->
        case Map.get(line, "configurationVersionID") do
          version_id when is_binary(version_id) ->
            bind_design_snapshot(snapshot, version_id, line, world)

          # The reference compares the line's own `configurationVersionID` only
          # when it is a string -- `if isinstance(version_id, str) and
          # snapshot.configurationVersionID != version_id` -- so a non-string is
          # not compared at all, and neither is it here.
          _not_a_string ->
            :ok
        end
    end
  end

  defp bind_design_snapshot(snapshot, version_id, line, world) do
    with {:ok, canonical} <- canonical_configuration_version(world, version_id),
         :ok <- snapshot_names_canonical(snapshot, version_id),
         {:ok, assembly} <- frozen_assembly(canonical),
         :ok <- snapshot_item_matches(snapshot, assembly, line),
         :ok <- snapshot_fields_match(snapshot, assembly),
         :ok <- section_summary_matches(snapshot, assembly) do
      :ok
    else
      {:error, _message} = error -> error
    end
  end

  defp canonical_configuration_version(world, version_id) do
    case Canonical.entity(world, "configuration_version", version_id) do
      %Canonical.Entity{deleted_at: nil} = row ->
        case Canonical.payload_object(row) do
          nil ->
            {:error, "Quotation assembly ConfigurationVersion is not canonical and active"}

          payload ->
            {:ok, payload}
        end

      _other ->
        {:error, "Quotation assembly ConfigurationVersion is not canonical and active"}
    end
  end

  defp snapshot_names_canonical(snapshot, version_id) do
    if Map.get(snapshot, "configurationVersionID") == version_id do
      :ok
    else
      {:error,
       "Quotation assembly design does not match canonical ConfigurationVersion: quotation designSnapshot does not reference canonical ConfigurationVersion"}
    end
  end

  defp frozen_assembly(canonical) do
    case Map.get(canonical, "configuration") do
      configuration when object?(configuration) ->
        case Map.get(configuration, "assembly") do
          assembly when object?(assembly) ->
            {:ok, {configuration, assembly}}

          _absent_or_malformed ->
            {:error,
             "Quotation assembly design does not match canonical ConfigurationVersion: canonical ConfigurationVersion has no frozen assembly snapshot"}
        end

      _other ->
        {:error,
         "Quotation assembly design does not match canonical ConfigurationVersion: canonical configuration_version payload is malformed"}
    end
  end

  defp snapshot_item_matches(_snapshot, {configuration, _assembly}, line) do
    if is_binary(Map.get(line, "itemID")) and
         Map.get(configuration, "itemID") == Map.get(line, "itemID") do
      :ok
    else
      {:error,
       "Quotation assembly design does not match canonical ConfigurationVersion: quotation designSnapshot ConfigurationVersion belongs to another Item"}
    end
  end

  defp snapshot_fields_match(snapshot, {_configuration, assembly}) do
    checks = [
      {"templateID", Map.get(assembly, "templateID"), Map.get(snapshot, "templateID")},
      {"templateVersion", Map.get(assembly, "templateVersion"),
       Map.get(snapshot, "templateVersion")},
      {"overall", Map.get(assembly, "overall"), Map.get(snapshot, "overall")},
      {"referenceData", Map.get(assembly, "referenceData"), Map.get(snapshot, "referenceData")},
      {"renderDescriptor", Map.get(assembly, "renderDescriptor"),
       Map.get(snapshot, "renderDescriptor")}
    ]

    Enum.reduce_while(checks, :ok, fn {label, canonical, proposed}, :ok ->
      if Value.py_eq?(canonical, proposed) do
        {:cont, :ok}
      else
        {:halt,
         {:error,
          "Quotation assembly design does not match canonical ConfigurationVersion: quotation designSnapshot #{label} differs from canonical ConfigurationVersion"}}
      end
    end)
  end

  defp section_summary_matches(snapshot, {_configuration, assembly}) do
    expected =
      Enum.map(List.wrap(Map.get(assembly, "sections")), fn section ->
        %{
          "sectionType" => Map.get(section, "sectionType"),
          "operation" => Map.get(section, "operation")
        }
      end)

    actual =
      Enum.map(List.wrap(Map.get(snapshot, "sectionSummary")), fn entry ->
        %{
          "sectionType" => Map.get(entry, "sectionType"),
          "operation" => Map.get(entry, "operation")
        }
      end)

    if Value.py_eq?(actual, expected) do
      :ok
    else
      {:error,
       "Quotation assembly design does not match canonical ConfigurationVersion: quotation designSnapshot section summary differs from canonical ConfigurationVersion"}
    end
  end

  defp quotation_events(payload, principal) do
    case Map.get(payload, "events") do
      events when is_list(events) ->
        with :ok <- well_formed_quotation_events(events, principal) do
          if Value.member?(
               Map.get(payload, "status"),
               MapSet.new(["sent", "approved", "rejected", "superseded"])
             ) do
            require_transition_provenance(events, principal)
          else
            :ok
          end
        end

      nil ->
        if Value.member?(
             Map.get(payload, "status"),
             MapSet.new(["sent", "approved", "rejected", "superseded"])
           ) do
          {:error, "Quotation lifecycle transition requires provenance"}
        else
          :ok
        end

      _other ->
        {:error, "Quotation event history is invalid"}
    end
  end

  defp well_formed_quotation_events(events, principal) do
    Enum.reduce_while(events, :ok, fn event, :ok ->
      cond do
        not Value.object?(event) ->
          {:halt, {:error, "Quotation event is malformed"}}

        true ->
          organization_id = Map.get(event, "organizationID")

          if not is_nil(organization_id) and organization_id != principal.organization_id do
            {:halt, {:error, "Quotation event organization is invalid"}}
          else
            {:cont, :ok}
          end
      end
    end)
  end

  defp require_transition_provenance([], _principal),
    do: {:error, "Quotation lifecycle transition requires provenance"}

  defp require_transition_provenance(events, principal) do
    Provenance.require_current(List.last(events), principal)
  end

  # ---------------------------------------------------------------------------
  # shared helpers -- app/ownership.py:104-256
  # ---------------------------------------------------------------------------

  defp absent_canonical(label, generic_exists, deleting) do
    cond do
      generic_exists -> {:error, "existing #{label} lacks canonical ownership"}
      deleting -> {:error, "cannot tombstone an unknown #{label}"}
      true -> :ok
    end
  end

  defp capability(principal, capability_name) do
    if Canonical.capable?(principal, capability_name) do
      :ok
    else
      {:error, "#{capability_name} capability required"}
    end
  end

  # `_require_identity/3`, `app/ownership.py:111-116`. A missing `id` is allowed
  # only while deleting, because a tombstone may carry an empty payload.
  defp identity(payload, entity_id, true) do
    if is_nil(Map.get(payload, "id")) do
      :ok
    else
      check_identity(payload, entity_id)
    end
  end

  defp identity(payload, entity_id, false), do: check_identity(payload, entity_id)

  defp check_identity(payload, entity_id) do
    case Map.get(payload, "id") do
      value when is_binary(value) ->
        if String.trim(value) == entity_id do
          :ok
        else
          {:error, "payload identity does not match canonical entityID"}
        end

      _other ->
        {:error, "payload identity does not match canonical entityID"}
    end
  end

  # S-106: the immutability check reads the *incoming* payload and fires only
  # when the client supplied the field at all.
  defp immutable(payload, key, canonical_value, message) do
    supplied = Map.get(payload, key)

    if not is_nil(supplied) and not Value.py_eq?(supplied, canonical_value) do
      {:error, message}
    else
      :ok
    end
  end

  defp value(payload, key) do
    case Value.required_string(payload, key) do
      nil -> {:error, "#{key} is required for canonical ownership"}
      text -> {:ok, text}
    end
  end

  defp require_object(value, message) do
    if Value.object?(value), do: :ok, else: {:error, message}
  end

  # `_project_for_scope/4`, `app/ownership.py:176-187`.
  defp project_for_scope(world, scope, project_id) do
    case Canonical.project(world, project_id) do
      %Canonical.Project{deleted_at: nil} = project ->
        if Canonical.Scope.can_access_project?(scope, project.project_id, project.customer_id) do
          {:ok, project}
        else
          {:error, "Project is outside authorized scope"}
        end

      _other ->
        {:error, "Project is not canonical and active"}
    end
  end

  # `_item_for_scope/5`, `app/ownership.py:190-212`. Three distinct rejections,
  # which S-103 pins: the item must be live, it must have a sector, and that
  # sector must be live and belong to the item's project.
  defp item_for_scope(world, scope, item_id) do
    case Canonical.item(world, item_id) do
      %Canonical.Item{deleted_at: nil} = item ->
        with :ok <- item_has_sector(item),
             {:ok, project} <- project_for_scope(world, scope, item.project_id),
             {:ok, _sector} <-
               sector_of_project(
                 world,
                 item.project_sector_id,
                 item.project_id,
                 "Item ProjectSector is not canonical and active"
               ) do
          {:ok, item, project}
        end

      _other ->
        {:error, "Item is not canonical and active"}
    end
  end

  # ---------------------------------------------------------------------------
  # apply_ownership_plan -- app/ownership.py:765-901
  # ---------------------------------------------------------------------------

  @doc """
  `apply_ownership_plan/5`, `app/ownership.py:765-901`.

  Returns the rows the reference would write, plus the scope grant
  `_grant_created_scope` (`app/ownership.py:944-966`) would apply. The grant is
  what lets a client create a Customer and then a Project under it in one batch:
  the second record is authorized against a scope the first record widened.

  `authorization_revision_bump?` says whether the grant actually changed the
  membership. Bumping the organization revision is what makes every in-flight
  mutation asserting current provenance fail, so it is the revocation mechanism
  (S-121) and the flag says exactly whether it is needed.

      iex> alias VitrialSync.{Canonical, Ownership}
      iex> plan = %Ownership.Plan{entity_type: "customer", entity_id: "c1", is_create: true,
      ...>   deleted_at: nil, customer_id: "c1"}
      iex> principal = %Canonical.Principal{organization_id: "org", user_id: "u1",
      ...>   membership_id: "m1", session_id: "s1", authorization_revision: 1}
      iex> world = %Canonical.World{membership: %Canonical.Membership{active: true,
      ...>   organization_id: "org", user_id: "u1"}, organization: %Canonical.Organization{}}
      iex> Ownership.apply_plan(plan, world, principal)
      {:ok, %{upserts: [{:customer, "c1", nil}], scope_grant: %{customer_ids: ["c1"], project_ids: []}, authorization_revision_bump?: true}}
  """
  @spec apply_plan(Plan.t(), Canonical.World.t(), Canonical.Principal.t()) ::
          {:ok, map()} | {:error, String.t()}
  def apply_plan(%Plan{} = plan, %Canonical.World{} = world, %Canonical.Principal{} = principal) do
    do_apply(plan, world, principal, blank())
  end

  defp blank, do: %{upserts: [], customer_ids: [], project_ids: []}

  defp empty_grant, do: %{customer_ids: [], project_ids: []}

  defp result(effect, grant, upsert) do
    customers = effect.customer_ids ++ grant.customer_ids
    projects = effect.project_ids ++ grant.project_ids

    %{
      upserts: effect.upserts ++ [upsert],
      scope_grant: %{customer_ids: customers, project_ids: projects},
      authorization_revision_bump?: customers != [] or projects != []
    }
  end

  defp do_apply(plan, world, principal, effect) do
    case plan.entity_type do
      "customer" ->
        with {:ok, grant} <- new_customer_grant(plan, world, principal) do
          {:ok, result(effect, grant, customer_row(plan))}
        end

      "project" ->
        with {:ok, grant} <- new_project_grant(plan, world, principal) do
          {:ok, result(effect, grant, project_row(plan))}
        end

      "project_sector" ->
        with :ok <-
               require_plan_field(
                 plan.project_id,
                 "ProjectSector ownership plan is missing project_id or sector_id"
               ),
             :ok <-
               require_plan_field(
                 plan.sector_id,
                 "ProjectSector ownership plan is missing project_id or sector_id"
               ),
             :ok <- project_sector_write(plan, world) do
          {:ok, result(effect, empty_grant(), project_sector_row(plan))}
        end

      "item" ->
        with :ok <-
               require_plan_field(
                 plan.project_id,
                 "Item ownership plan is missing project_id or project_sector_id"
               ),
             :ok <-
               require_plan_field(
                 plan.project_sector_id,
                 "Item ownership plan is missing project_id or project_sector_id"
               ),
             :ok <- item_write(plan, world) do
          {:ok, result(effect, empty_grant(), item_row(plan))}
        end

      other ->
        if MapSet.member?(@project_child_types, other) do
          apply_project_child(plan, world, effect)
        else
          apply_item_child(plan, world, effect)
        end
    end
  end

  defp require_plan_field(nil, message), do: {:error, message}
  defp require_plan_field(_value, _message), do: :ok

  # A created Customer also widens the creator's customer scope, and the
  # membership guard runs on every create -- not only on one that changes
  # something -- so a membership revoked between authentication and this write
  # refuses the mutation.
  defp new_customer_grant(plan, world, principal) do
    if is_nil(Canonical.customer(world, plan.entity_id)) do
      grant(principal, world, %{customer_ids: [plan.entity_id], project_ids: []})
    else
      {:ok, empty_grant()}
    end
  end

  # A created Project widens project scope only. `_grant_created_scope` takes one
  # argument per axis, and the Project branch passes `project_id` alone.
  defp new_project_grant(plan, world, principal) do
    case Canonical.project(world, plan.entity_id) do
      nil ->
        grant(principal, world, %{customer_ids: [], project_ids: [plan.entity_id]})

      %Canonical.Project{customer_id: customer_id} ->
        if Value.py_eq?(customer_id, plan.customer_id) do
          {:ok, empty_grant()}
        else
          {:error, "Project Customer ownership is immutable"}
        end
    end
  end

  # `_grant_created_scope`, `app/ownership.py:944-966`.
  defp grant(principal, world, requested) do
    membership = world.membership

    cond do
      is_nil(membership) or is_nil(world.organization) or membership.active != true ->
        {:error, "creator membership is not canonical and active"}

      membership.organization_id != principal.organization_id or
          membership.user_id != principal.user_id ->
        {:error, "creator membership is not canonical and active"}

      true ->
        customer_ids =
          if requested.customer_ids == [] or membership.all_customers,
            do: [],
            else: requested.customer_ids -- membership.customer_ids

        project_ids =
          if requested.project_ids == [] or membership.all_projects,
            do: [],
            else: requested.project_ids -- membership.project_ids

        {:ok, %{customer_ids: customer_ids, project_ids: project_ids}}
    end
  end

  defp project_sector_write(plan, world) do
    case Canonical.project_sector(world, plan.entity_id) do
      nil ->
        :ok

      %Canonical.ProjectSector{project_id: project_id, sector_id: sector_id} ->
        if project_id == plan.project_id and sector_id == plan.sector_id do
          :ok
        else
          {:error, "ProjectSector ownership is immutable"}
        end
    end
  end

  defp item_write(plan, world) do
    case Canonical.item(world, plan.entity_id) do
      nil ->
        :ok

      %Canonical.Item{project_id: project_id, project_sector_id: sector_id} ->
        if project_id == plan.project_id and sector_id == plan.project_sector_id do
          :ok
        else
          {:error, "Item canonical ownership is immutable"}
        end
    end
  end

  # The project-child label is `entity_type.replace("_", " ").capitalize()` in
  # the reference, which is what makes the message read "Delivery execution
  # Project ownership is immutable" rather than repeating the generic wording.
  defp apply_project_child(plan, world, effect) do
    case plan.project_id do
      nil ->
        {:error, "Project-child ownership plan is missing project_id"}

      project_id ->
        case Canonical.project_child(world, plan.entity_type, plan.entity_id) do
          nil ->
            {:ok,
             result(
               effect,
               empty_grant(),
               {:project_child, plan.entity_type, plan.entity_id, project_id, plan.deleted_at}
             )}

          %Canonical.ProjectChild{project_id: existing} ->
            if existing == project_id do
              {:ok,
               result(
                 effect,
                 empty_grant(),
                 {:project_child, plan.entity_type, plan.entity_id, project_id, plan.deleted_at}
               )}
            else
              {:error, "#{plan_label(plan.entity_type)} Project ownership is immutable"}
            end
        end
    end
  end

  defp apply_item_child(plan, world, effect) do
    cond do
      not MapSet.member?(@item_child_types, plan.entity_type) ->
        {:error, "unsupported ownership plan #{inspect(plan.entity_type)}"}

      is_nil(plan.item_id) ->
        {:error, "Item-child ownership plan is missing item_id"}

      true ->
        write_item_child(plan, world, effect)
    end
  end

  defp write_item_child(plan, world, effect) do
    upsert =
      {:item_child, plan.entity_type, plan.entity_id, plan.item_id, plan.deleted_at}

    case Canonical.item_child(world, plan.entity_type, plan.entity_id) do
      nil ->
        {:ok, result(effect, empty_grant(), upsert)}

      %Canonical.ItemChild{item_id: item_id} ->
        if item_id == plan.item_id do
          {:ok, result(effect, empty_grant(), upsert)}
        else
          {:error, "#{plan.entity_type} Item ownership is immutable"}
        end
    end
  end

  defp plan_label(entity_type) do
    entity_type |> String.replace("_", " ") |> String.capitalize()
  end

  defp customer_row(%Plan{} = plan), do: {:customer, plan.entity_id, plan.deleted_at}

  defp project_row(%Plan{} = plan),
    do: {:project, plan.entity_id, plan.customer_id, plan.deleted_at}

  defp project_sector_row(%Plan{} = plan),
    do: {:project_sector, plan.entity_id, plan.project_id, plan.sector_id, plan.deleted_at}

  defp item_row(%Plan{} = plan),
    do: {:item, plan.entity_id, plan.project_id, plan.project_sector_id, plan.deleted_at}
end
