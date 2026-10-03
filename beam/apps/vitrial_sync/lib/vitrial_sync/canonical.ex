defmodule VitrialSync.Canonical do
  @moduledoc """
  The canonical server state the admission decisions read, as data.

  `_apply_push_once` (`app/sync_service.py:227-476`) makes five decisions after
  the payload decodes, and every one of them asks the database the same kind of
  question: *does this canonical row exist, is it still live, and does its parent
  link agree?* Reproducing those questions as pure functions needs the rows
  themselves, so they arrive here as a plain struct.

  ## The world is a snapshot, not a database

  `World` holds lists and the accessors are linear scans. That is the trade this
  module makes, and it is the right one for a module whose only job is to be
  read:

    * every accessor returns `nil` for an absent row and a row otherwise, which
      is exactly what SQLAlchemy's `db.get` returns and what every
      `if canonical is None` branch in the Python is written against;
    * the predicates are the `SELECT ... WHERE ... LIMIT 1` queries, transcribed.
      Keeping them as predicates over rows rather than as precomputed booleans is
      what makes them testable -- a test can put a tombstoned audit event in the
      world and watch the guard pass, which is the whole content of S-112.

  Nothing here hides the scan, and nothing here assumes it is free. A production
  caller that holds indexes substitutes its own accessors.

  ## The organization is fixed by the principal

  Every canonical table in the Python is keyed `(organization_id, ...)` and every
  lookup passes `principal.organization_id`. The `World` therefore omits the
  organization column: one `World` is one organization's state and the principal
  names which. No code path in the reference reads a *different* organization's
  row, so nothing is lost.

  `Membership` and `Organization` are here because `_grant_created_scope`
  (`app/ownership.py:944-966`) checks the creator's membership is still active and
  bumps the organization revision when a self-created parent widened the creator's
  own scope.

  Value comparison goes through `VitrialSync.Value` rather than Elixir's `==`;
  read that module before changing a comparison here.
  """

  alias VitrialSync.Value

  defmodule Principal do
    @moduledoc "`app.auth.Principal`, `app/auth.py:20-29`."
    defstruct [
      :user_id,
      :organization_id,
      :membership_id,
      :session_id,
      :authorization_revision,
      all_customers: false,
      all_projects: false,
      capabilities: MapSet.new(),
      customer_ids: MapSet.new(),
      project_ids: MapSet.new()
    ]

    @type t :: %__MODULE__{
            user_id: String.t(),
            organization_id: String.t(),
            membership_id: String.t(),
            session_id: String.t(),
            authorization_revision: non_neg_integer(),
            all_customers: boolean(),
            all_projects: boolean(),
            capabilities: MapSet.t(String.t()),
            customer_ids: MapSet.t(String.t()),
            project_ids: MapSet.t(String.t())
          }
  end

  defmodule Scope do
    @moduledoc "`EffectiveScope`, `app/ownership.py:30-55`."
    defstruct all_customers: false,
              all_projects: false,
              customer_ids: MapSet.new(),
              project_ids: MapSet.new()

    @type t :: %__MODULE__{
            all_customers: boolean(),
            all_projects: boolean(),
            customer_ids: MapSet.t(String.t()),
            project_ids: MapSet.t(String.t())
          }

    @doc "`EffectiveScope.from_principal/1`, `app/ownership.py:36-43`."
    @spec from_principal(Principal.t()) :: t()
    def from_principal(%Principal{} = principal) do
      %__MODULE__{
        all_customers: principal.all_customers,
        all_projects: principal.all_projects,
        customer_ids: principal.customer_ids,
        project_ids: principal.project_ids
      }
    end

    @doc "`can_access_customer/1`, `app/ownership.py:45-46`."
    @spec can_access_customer?(t(), String.t()) :: boolean()
    def can_access_customer?(%__MODULE__{} = scope, customer_id) do
      scope.all_customers or MapSet.member?(scope.customer_ids, customer_id)
    end

    @doc "`can_access_project/2`, `app/ownership.py:48-51`."
    @spec can_access_project?(t(), String.t(), String.t()) :: boolean()
    def can_access_project?(%__MODULE__{} = scope, project_id, customer_id) do
      can_access_customer?(scope, customer_id) and
        (scope.all_projects or MapSet.member?(scope.project_ids, project_id))
    end
  end

  defmodule Customer do
    @moduledoc "`CanonicalCustomer`."
    defstruct [:customer_id, :deleted_at]
    @type t :: %__MODULE__{customer_id: String.t(), deleted_at: term()}
  end

  defmodule Project do
    @moduledoc "`CanonicalProject`."
    defstruct [:project_id, :customer_id, :deleted_at]
    @type t :: %__MODULE__{project_id: String.t(), customer_id: String.t(), deleted_at: term()}
  end

  defmodule ProjectSector do
    @moduledoc "`CanonicalProjectSector`."
    defstruct [:project_sector_id, :project_id, :sector_id, :deleted_at]

    @type t :: %__MODULE__{
            project_sector_id: String.t(),
            project_id: String.t(),
            sector_id: String.t(),
            deleted_at: term()
          }
  end

  defmodule Item do
    @moduledoc "`CanonicalItem`."
    defstruct [:item_id, :project_id, :project_sector_id, :deleted_at]

    @type t :: %__MODULE__{
            item_id: String.t(),
            project_id: String.t(),
            project_sector_id: String.t() | nil,
            deleted_at: term()
          }
  end

  defmodule ItemChild do
    @moduledoc """
    `CanonicalItemChild`: the sidecar table holding `item_audit_event`, `evidence`,
    `measurement`, `blocker`, `customer_requirement`, `configuration` and
    `configuration_version`.
    """
    defstruct [:entity_type, :entity_id, :item_id, :deleted_at]

    @type t :: %__MODULE__{
            entity_type: String.t(),
            entity_id: String.t(),
            item_id: String.t(),
            deleted_at: term()
          }
  end

  defmodule ProjectChild do
    @moduledoc "`CanonicalProjectChild`: holds `quotation` and `delivery_execution`."
    defstruct [:entity_type, :entity_id, :project_id, :deleted_at]

    @type t :: %__MODULE__{
            entity_type: String.t(),
            entity_id: String.t(),
            project_id: String.t(),
            deleted_at: term()
          }
  end

  defmodule Entity do
    @moduledoc """
    `SyncEntity`: the generic mirror row that stores the payload itself.

    `payload` is a `term()` rather than an object because the column is `json` and
    the reference explicitly tolerates a non-object there. Four separate call
    sites read it as
    `current.payload_json if current is not None and isinstance(current.payload_json, dict) else None`,
    which is `payload_object/1` here. A row whose payload is JSON `null` reads as
    "no payload", not as an empty object.
    """
    defstruct [:entity_type, :entity_id, :payload, :deleted_at, :server_revision]

    @type t :: %__MODULE__{
            entity_type: String.t(),
            entity_id: String.t(),
            payload: term(),
            deleted_at: term(),
            server_revision: non_neg_integer() | nil
          }
  end

  defmodule EvidenceBlob do
    @moduledoc """
    `EvidenceBlob`: the object-storage handle an `evidence` record points at.

    These three fields plus the organization are the whole of `queue_blob_gc`'s
    input (`app/evidence_gc.py:56-70`); it copies them onto the GC row verbatim.
    """
    defstruct [:document_id, :storage_provider, :object_key]

    @type t :: %__MODULE__{
            document_id: String.t(),
            storage_provider: String.t(),
            object_key: String.t()
          }
  end

  defmodule Membership do
    @moduledoc """
    The `Membership` row `_grant_created_scope` reads and widens.

    `customer_ids` and `project_ids` are lists, not sets, because the Python
    assigns `[*membership.customer_ids, customer_id]` and the order of the stored
    array is what the next authorization profile reports.
    """
    defstruct [
      :organization_id,
      :user_id,
      :active,
      all_customers: false,
      all_projects: false,
      customer_ids: [],
      project_ids: []
    ]

    @type t :: %__MODULE__{
            organization_id: String.t() | nil,
            user_id: String.t() | nil,
            active: boolean() | nil,
            all_customers: boolean(),
            all_projects: boolean(),
            customer_ids: [String.t()],
            project_ids: [String.t()]
          }
  end

  defmodule Organization do
    @moduledoc "`Organization.authorization_revision`, the value a `Principal` is built from."
    defstruct authorization_revision: 0
    @type t :: %__MODULE__{authorization_revision: non_neg_integer()}
  end

  defmodule World do
    @moduledoc """
    One organization's canonical state, as read by the admission decisions.

    `empty/0` is a legitimate world: it is what a brand-new organization looks
    like, and every create-path branch of `authorize_record` is written against
    exactly this.
    """
    defstruct customers: [],
              projects: [],
              project_sectors: [],
              items: [],
              item_children: [],
              project_children: [],
              entities: [],
              evidence_blobs: [],
              membership: nil,
              organization: nil

    @type t :: %__MODULE__{
            customers: [Customer.t()],
            projects: [Project.t()],
            project_sectors: [ProjectSector.t()],
            items: [Item.t()],
            item_children: [ItemChild.t()],
            project_children: [ProjectChild.t()],
            entities: [Entity.t()],
            evidence_blobs: [EvidenceBlob.t()],
            membership: Membership.t() | nil,
            organization: Organization.t() | nil
          }
  end

  @doc "A world with no canonical rows at all."
  @spec empty_world() :: World.t()
  def empty_world, do: %World{}

  @doc "`db.get(CanonicalCustomer, (org, customer_id))`."
  @spec customer(World.t(), String.t()) :: Customer.t() | nil
  def customer(%World{customers: rows}, customer_id),
    do: Enum.find(rows, &(&1.customer_id == customer_id))

  @doc "`db.get(CanonicalProject, (org, project_id))`."
  @spec project(World.t(), String.t()) :: Project.t() | nil
  def project(%World{projects: rows}, project_id),
    do: Enum.find(rows, &(&1.project_id == project_id))

  @doc "`db.get(CanonicalProjectSector, (org, project_sector_id))`."
  @spec project_sector(World.t(), String.t()) :: ProjectSector.t() | nil
  def project_sector(%World{project_sectors: rows}, id),
    do: Enum.find(rows, &(&1.project_sector_id == id))

  @doc "`db.get(CanonicalItem, (org, item_id))`."
  @spec item(World.t(), String.t()) :: Item.t() | nil
  def item(%World{items: rows}, item_id), do: Enum.find(rows, &(&1.item_id == item_id))

  @doc "`db.get(CanonicalItemChild, (org, entity_type, entity_id))`."
  @spec item_child(World.t(), String.t(), String.t()) :: ItemChild.t() | nil
  def item_child(%World{item_children: rows}, entity_type, entity_id) do
    Enum.find(rows, &(&1.entity_type == entity_type and &1.entity_id == entity_id))
  end

  @doc "`db.get(CanonicalProjectChild, (org, entity_type, entity_id))`."
  @spec project_child(World.t(), String.t(), String.t()) :: ProjectChild.t() | nil
  def project_child(%World{project_children: rows}, entity_type, entity_id) do
    Enum.find(rows, &(&1.entity_type == entity_type and &1.entity_id == entity_id))
  end

  @doc "`db.get(SyncEntity, (org, entity_type, entity_id))`."
  @spec entity(World.t(), String.t(), String.t()) :: Entity.t() | nil
  def entity(%World{entities: rows}, entity_type, entity_id) do
    Enum.find(rows, &(&1.entity_type == entity_type and &1.entity_id == entity_id))
  end

  @doc "`db.get(EvidenceBlob, (org, document_id))`."
  @spec evidence_blob(World.t(), String.t()) :: EvidenceBlob.t() | nil
  def evidence_blob(%World{evidence_blobs: rows}, document_id),
    do: Enum.find(rows, &(&1.document_id == document_id))

  @doc """
  `_active_item_child/4`, `app/ownership.py:215-225`: present **and** not
  tombstoned.

  Kept as one function because the difference between "absent" and "tombstoned"
  is load-bearing, and folding them together is exactly how a soft-deleted
  sidecar starts blocking deletes it should not.
  """
  @spec active_item_child(World.t(), String.t(), String.t()) :: ItemChild.t() | nil
  def active_item_child(%World{} = world, entity_type, entity_id) do
    case item_child(world, entity_type, entity_id) do
      %ItemChild{deleted_at: nil} = child -> child
      _otherwise -> nil
    end
  end

  @doc "The `SyncEntity.payload_json` reading four call sites share."
  @spec payload_object(Entity.t() | nil) :: map() | nil
  def payload_object(nil), do: nil
  def payload_object(%Entity{payload: payload}) when is_map(payload) and not is_struct(payload), do: payload
  def payload_object(%Entity{}), do: nil

  @doc """
  `_item_has_immutable_audit_history`, `app/sync_service.py:111-124`. S-58.

  A live `item_audit_event` for this item, and *only* audit events. The sibling
  guard `item_has_live_sidecars?/2` covers everything else; the two together are
  S-112, and an item dies only when both are clear.
  """
  @spec item_has_live_audit_history?(World.t(), String.t()) :: boolean()
  def item_has_live_audit_history?(%World{item_children: rows}, item_id) do
    Enum.any?(rows, fn row ->
      row.entity_type == "item_audit_event" and row.item_id == item_id and
        is_nil(row.deleted_at)
    end)
  end

  @doc "`_ensure_item_delete_safe`, `app/ownership.py:320-334`: S-112's other half."
  @spec item_has_live_sidecars?(World.t(), String.t()) :: boolean()
  def item_has_live_sidecars?(%World{item_children: rows}, item_id) do
    Enum.any?(rows, fn row ->
      row.entity_type != "item_audit_event" and row.item_id == item_id and
        is_nil(row.deleted_at)
    end)
  end

  @doc "`_ensure_customer_delete_safe`, `app/ownership.py:258-271`."
  @spec customer_has_live_projects?(World.t(), String.t()) :: boolean()
  def customer_has_live_projects?(%World{projects: rows}, customer_id) do
    Enum.any?(rows, &(&1.customer_id == customer_id and is_nil(&1.deleted_at)))
  end

  @doc """
  `_ensure_project_delete_safe`, `app/ownership.py:274-301`.

  Three tables in one guard: sectors, items, and project children of *any* type,
  so a live quotation blocks a project delete and not merely a live sector.
  """
  @spec project_has_live_children?(World.t(), String.t()) :: boolean()
  def project_has_live_children?(%World{} = world, project_id) do
    Enum.any?(world.project_sectors, &(&1.project_id == project_id and is_nil(&1.deleted_at))) or
      Enum.any?(world.items, &(&1.project_id == project_id and is_nil(&1.deleted_at))) or
      Enum.any?(world.project_children, &(&1.project_id == project_id and is_nil(&1.deleted_at)))
  end

  @doc "`_ensure_project_sector_delete_safe`, `app/ownership.py:304-317`."
  @spec project_sector_has_live_items?(World.t(), String.t()) :: boolean()
  def project_sector_has_live_items?(%World{items: rows}, project_sector_id) do
    Enum.any?(rows, &(&1.project_sector_id == project_sector_id and is_nil(&1.deleted_at)))
  end

  @doc """
  `_ensure_evidence_delete_safe`, `app/ownership.py:337-358`.

  A live `measurement` or `customer_requirement` whose payload lists this evidence
  id under `evidenceReferences` or `evidenceReferenceIDs`.

  The Python takes `item_id` as a parameter and then does not use it: the filter
  is by evidence id alone, across the whole organization. That is the reference
  behaviour and it is preserved here. An evidence id names one document, so a
  stricter filter would reject a delete the server permits.

  The comparison is by exact type rather than `Value.py_eq?/2` because the
  reference predicate is a PostgreSQL `jsonb @>` containment, and `jsonb` does not
  consider `1` and `1.0` equal.
  """
  @spec evidence_referenced?(World.t(), String.t()) :: boolean()
  def evidence_referenced?(%World{entities: rows}, evidence_id) do
    Enum.any?(rows, fn row ->
      row.entity_type in ["measurement", "customer_requirement"] and
        is_nil(row.deleted_at) and references_evidence?(row.payload, evidence_id)
    end)
  end

  defp references_evidence?(payload, evidence_id) when is_map(payload) do
    jsonb_array_contains?(Map.get(payload, "evidenceReferences"), evidence_id) or
      jsonb_array_contains?(Map.get(payload, "evidenceReferenceIDs"), evidence_id)
  end

  defp references_evidence?(_payload, _evidence_id), do: false

  defp jsonb_array_contains?(values, evidence_id) when is_list(values) do
    Enum.any?(values, &is_binary(&1) and &1 == evidence_id)
  end

  defp jsonb_array_contains?(_values, _evidence_id), do: false

  @doc "Whether `principal` holds `capability`."
  @spec capable?(Principal.t(), String.t()) :: boolean()
  def capable?(%Principal{capabilities: capabilities}, capability),
    do: MapSet.member?(capabilities, capability)

  @doc "A trimmed string, or `nil`. `VitrialSync.Value.normalized/1` under a shorter name."
  @spec normalized(term()) :: String.t() | nil
  defdelegate normalized(value), to: Value
end
