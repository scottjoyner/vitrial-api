defmodule VitrialSync.Prefetch do
  @moduledoc """
  Pull visibility resolution for a whole page at once.

  `load_page_context` (`app/pull_prefetch.py:116-231`), `resolve_visible`
  (`234-291`) and `resolve_delivery_visible` (`294-314`), reached from
  `app/sync_service.py:527-538`.

  ## The rule this module exists to obey

  `resolve_visible` must return exactly what `record_is_visible`
  (`app/ownership.py:942-1014`) returns, for every input -- and `resolve_delivery_visible`
  exactly what `delivery_execution_is_visible` (`app/delivery_execution.py:868-896`)
  returns. Not "the same in the cases I checked". The same, by construction.

  Two things protect that, and both are the reason the branches below are written
  as one parallel pair rather than tidied into a shared helper:

    1. **Same order of checks, including the early returns.** A missing row and a
       row the principal cannot reach both yield `false`, and they must stay
       indistinguishable from the outside. Merging two branches that happen to
       agree today is how a later change to one of them stops being a change to
       both.
    2. **No new rules.** In particular the generic branches do *not* check
       `deleted_at`, because `record_is_visible` does not. The delivery branch
       does, because `delivery_execution_is_visible` does. That asymmetry is
       pre-existing behaviour and it is deliberate in both directions:

       `record_is_visible`'s docstring (`app/ownership.py:951-968`) explains the
       generic half -- a tombstone has to be *delivered*, carrying `deletedAt`,
       because the client cannot infer a deletion from an absence, and filtering
       soft-deleted rows out would make every server-side deletion permanently
       invisible to the local copy. The delivery half asks a different question: a
       delivery execution is a state machine, and a tombstoned one re-entering the
       advance path would be a state violation rather than a stale record.

  `tests/test_pull_prefetch_equivalence.py` is the reference's own guard on this;
  `test/vitrial_sync/prefetch_test.exs` is its counterpart here.

  ## What "pure" costs, and what it buys

  `load_page_context` in the reference issues set-based queries. Here the page is
  already loaded and the rows arrive as data (`VitrialSync.Canonical.World`), so
  this module is a *selection* rather than a fetch: which rows of the loaded world
  belong in the context, and under which key.

  That is not a smaller job than it looks. The reference's population order is
  load-bearing in three ways, and all three are reproduced here:

    * **The key shape.** Single-id maps are keyed by the bare id; composite maps by
      `{entity_type, entity_id}`. The reference's own comment
      (`app/pull_prefetch.py:140-143`) records that keying by the raw
      `(organization_id, ...)` primary key made every lookup miss.
    * **`delivery_execution` is loaded as a project child.** The pull routes it
      through `resolve_delivery_visible`, which reads the child from this same map.
      Excluding it -- which an earlier draft of the reference did, under a comment
      claiming the opposite -- made every delivery record invisible with no error
      and no warning, just an empty page.
    * **The dependent hops.** An item's project is only knowable once the item row
      is loaded, and a sector's project only once the sector row is. The reference
      collects those into one `IN` per hop so the query count stays fixed at two
      rather than one per item; here the same two passes decide what goes in the
      map. Over-fetching by a bounded amount is the point: a second round of
      dependent queries is what this module exists to avoid.

  ## Where it plugs in

  `VitrialSync.Page.assemble/2` takes `VitrialSync.Page.Candidate` rows whose
  `visible?` and `entity_present?` flags are already decided. Those are the two
  lookups this module makes -- `resolve_visible/3` (or `resolve_delivery_visible/2`
  for a delivery record) and the entity lookup below. A caller that has a context
  and a change list resolves both per change and hands the candidates to
  `VitrialSync.Page.assemble/2`; the page loop itself does not change.
  """

  alias VitrialSync.Canonical
  alias VitrialSync.Canonical.{Scope, World}

  defmodule Context do
    @moduledoc """
    `PageContext`, `app/pull_prefetch.py:63-85`.

    Every map is keyed the way the resolvers look up, so a missing key means
    exactly what a `None` return from `db.get` meant: the row does not exist.
    That equivalence is what lets the resolvers below read like the per-record
    originals rather than defensively re-checking existence.
    """
    @type t :: %__MODULE__{
            organization_id: String.t(),
            scope: Scope.t(),
            customers: %{String.t() => Canonical.Customer.t()},
            projects: %{String.t() => Canonical.Project.t()},
            sectors: %{String.t() => Canonical.ProjectSector.t()},
            items: %{String.t() => Canonical.Item.t()},
            project_children: %{
              {String.t(), String.t()} => Canonical.ProjectChild.t()
            },
            item_children: %{{String.t(), String.t()} => Canonical.ItemChild.t()},
            entities: %{{String.t(), String.t()} => Canonical.Entity.t()}
          }

    defstruct [
      :organization_id,
      :scope,
      customers: %{},
      projects: %{},
      sectors: %{},
      items: %{},
      project_children: %{},
      item_children: %{},
      entities: %{}
    ]
  end

  @delivery_entity_type "delivery_execution"

  # `PROJECT_CHILD_TYPES`, `app/ownership.py:78`. The same two strings
  # `VitrialSync.Ownership` refuses or routes elsewhere.
  @project_child_types MapSet.new(["quotation", "delivery_execution"])

  # `ITEM_CHILD_TYPES`, `app/ownership.py:75`, which is
  # `set(ITEM_CHILD_CAPABILITIES) | {"item_audit_event"}`.
  @item_child_types MapSet.new([
                      "measurement",
                      "evidence",
                      "customer_requirement",
                      "configuration",
                      "configuration_version",
                      "blocker",
                      "item_audit_event"
                    ])

  @doc "`DELIVERY_ENTITY_TYPE`, `app/delivery_execution.py:22`."
  @spec delivery_entity_type() :: String.t()
  def delivery_entity_type, do: @delivery_entity_type

  @doc "`PROJECT_CHILD_TYPES`, `app/ownership.py:78`."
  @spec project_child_types() :: MapSet.t(String.t())
  def project_child_types, do: @project_child_types

  @doc "`ITEM_CHILD_TYPES`, `app/ownership.py:75`."
  @spec item_child_types() :: MapSet.t(String.t())
  def item_child_types, do: @item_child_types

  @doc """
  Load the rows this page's visibility decisions could depend on.

  `changes` is the page's change-log rows, in scan order. Each needs an
  `entity_type` and an `entity_id`; a `VitrialSync.Page.Candidate`, a plain map
  with those two keys, or a `{entity_type, entity_id}` tuple all work, because the
  only thing read off a change is which `(type, id)` pairs the page is asking
  about.

  `world` is the loaded page: the canonical rows the storage layer handed back,
  as data. The selection below decides which of them belong in the context, in the
  reference's order, and the two dependent hops -- sectors to projects, items to
  projects -- are made explicit rather than left to the caller's queries.
  """
  @spec load_page_context(
          Canonical.Principal.t(),
          [map() | tuple() | struct()],
          World.t()
        ) :: Context.t()
  def load_page_context(%Canonical.Principal{} = principal, changes, %World{} = world) do
    by_type = group_by_type(changes)

    # Payloads for the whole page, fetched whether or not the record turns out to
    # be visible. The original fetched only for visible records; over-fetching here
    # is one pass instead of up to one per visible record.
    entities =
      index(world.entities, entity_pairs(by_type), &{&1.entity_type, &1.entity_id})

    customers = index(world.customers, Map.get(by_type, "customer", []), & &1.customer_id)

    # `delivery_execution` is a project child that the pull routes through
    # `resolve_delivery_visible` (extra `deleted_at` checks at each hop) rather than
    # `resolve_visible`. It still has to be *loaded* here, because
    # `resolve_delivery_visible` reads the child from this same map to make that
    # decision. Excluding it silently makes every delivery record invisible.
    project_children =
      index(
        world.project_children,
        type_and_id_pairs(by_type, @project_child_types),
        &{&1.entity_type, &1.entity_id}
      )

    item_children =
      index(
        world.item_children,
        type_and_id_pairs(by_type, @item_child_types),
        &{&1.entity_type, &1.entity_id}
      )

    sectors =
      index(world.project_sectors, Map.get(by_type, "project_sector", []), & &1.project_sector_id)

    # Second hop: every project id any loaded row points at. Collected before the
    # project map is built so the project selection does not depend on which
    # branches turned out to be live.
    wanted_projects =
      by_type
      |> Map.get("project", [])
      |> then(&MapSet.new/1)
      |> MapSet.union(MapSet.new(Map.values(project_children), & &1.project_id))
      |> MapSet.union(MapSet.new(Map.values(sectors), & &1.project_id))

    # Direct items plus any items reachable from item children, then the projects
    # those point at. These cannot be folded into `wanted_projects` earlier: an
    # item's project id is only known once the item row is loaded.
    reachable_item_ids =
      MapSet.new(Map.get(by_type, "item", []))
      |> MapSet.union(MapSet.new(Map.values(item_children), & &1.item_id))

    items = index(world.items, MapSet.to_list(reachable_item_ids), & &1.item_id)

    wanted_projects =
      MapSet.union(wanted_projects, MapSet.new(Map.values(items), & &1.project_id))

    projects = index(world.projects, MapSet.to_list(wanted_projects), & &1.project_id)

    %Context{
      organization_id: principal.organization_id,
      scope: Scope.from_principal(principal),
      customers: customers,
      projects: projects,
      sectors: sectors,
      items: items,
      project_children: project_children,
      item_children: item_children,
      entities: entities
    }
  end

  # `by_type.setdefault(change.entity_type, []).append(change.entity_id)`.
  # Insertion-ordered rather than sorted: the reference collects in scan order and
  # nothing here depends on the order, but preserving it keeps a diff of two
  # selections readable.
  defp group_by_type(changes) do
    Enum.reduce(changes, %{}, fn change, acc ->
      {type, id} = change_key(change)
      Map.update(acc, type, [id], &[id | &1])
    end)
  end

  defp change_key({type, id}) when is_binary(type) and is_binary(id), do: {type, id}

  defp change_key(%{entity_type: type, entity_id: id}), do: {type, id}

  defp change_key(change) do
    raise ArgumentError,
          "a pull change needs an entity_type and an entity_id, got: #{inspect(change)}"
  end

  # Every `{type, id}` pair the page is asking about, in page order.
  defp entity_pairs(by_type) do
    Enum.flat_map(by_type, fn {type, ids} -> Enum.map(ids, &{type, &1}) end)
  end

  # `PROJECT_CHILD_TYPES & by_type.keys()`, then each of that type's ids.
  defp type_and_id_pairs(by_type, types) do
    by_type
    |> Enum.filter(fn {type, _ids} -> MapSet.member?(types, type) end)
    |> entity_pairs()
  end

  # `db.get(Model, (org, ...))` restricted to `keys`, keyed the way the resolvers
  # read the map. A row the query did not return is simply absent, which is the
  # same signal `None` was.
  defp index(rows, keys, key_fun) do
    wanted = MapSet.new(keys)

    Enum.reduce(rows, %{}, fn row, acc ->
      key = key_fun.(row)

      if MapSet.member?(wanted, key) do
        Map.put(acc, key, row)
      else
        acc
      end
    end)
  end

  @doc """
  Batched twin of `record_is_visible`, `app/ownership.py:975-1014`.

  Each branch below mirrors the original's branch of the same name, in the same
  order, with the same early returns. The `cond` is the reference's `if` chain:
  Python's bare `return` inside a branch becomes the value of that `cond` clause,
  and the two `if sector is None: return False` forms become nested `if`s, because
  Elixir has no `return`.

  Reads of `ctx.*` use `Map.get/2`, which yields `nil` for an absent key -- the
  same signal the original's `db.get` returning `None` produced.
  """
  @spec resolve_visible(Context.t(), String.t(), String.t()) :: boolean()
  def resolve_visible(%Context{} = ctx, entity_type, entity_id) do
    scope = ctx.scope

    cond do
      entity_type == "customer" ->
        customer = Map.get(ctx.customers, entity_id)
        not is_nil(customer) and Scope.can_access_customer?(scope, customer.customer_id)

      entity_type == "project" ->
        project = Map.get(ctx.projects, entity_id)

        not is_nil(project) and
          Scope.can_access_project?(scope, project.project_id, project.customer_id)

      entity_type == "project_sector" ->
        sector = Map.get(ctx.sectors, entity_id)

        if is_nil(sector) do
          false
        else
          project = Map.get(ctx.projects, sector.project_id)

          not is_nil(project) and
            Scope.can_access_project?(scope, project.project_id, project.customer_id)
        end

      entity_type == "item" ->
        item = Map.get(ctx.items, entity_id)

        if is_nil(item) do
          false
        else
          project = Map.get(ctx.projects, item.project_id)

          not is_nil(project) and
            Scope.can_access_project?(scope, project.project_id, project.customer_id)
        end

      MapSet.member?(@project_child_types, entity_type) ->
        child = Map.get(ctx.project_children, {entity_type, entity_id})

        if is_nil(child) do
          false
        else
          project = Map.get(ctx.projects, child.project_id)

          not is_nil(project) and
            Scope.can_access_project?(scope, project.project_id, project.customer_id)
        end

      MapSet.member?(@item_child_types, entity_type) ->
        child = Map.get(ctx.item_children, {entity_type, entity_id})

        if is_nil(child) do
          false
        else
          item = Map.get(ctx.items, child.item_id)

          if is_nil(item) do
            false
          else
            project = Map.get(ctx.projects, item.project_id)

            not is_nil(project) and
              Scope.can_access_project?(scope, project.project_id, project.customer_id)
          end
        end

      true ->
        false
    end
  end

  @doc """
  Batched twin of `delivery_execution_is_visible`, `app/delivery_execution.py:874-896`.

  Keeps that function's `deleted_at` checks at every hop. The generic branches in
  `resolve_visible/3` do *not* check `deleted_at`, because `record_is_visible`
  does not; the asymmetry is pre-existing and preserved in both directions.

  The four nested `if`s are the reference's four nested `if`s, kept nested rather
  than collapsed into a single `cond` so the correspondence stays checkable by
  reading the two side by side.
  """
  @spec resolve_delivery_visible(Context.t(), String.t()) :: boolean()
  def resolve_delivery_visible(%Context{} = ctx, entity_id) do
    entity = Map.get(ctx.entities, {@delivery_entity_type, entity_id})

    if is_nil(entity) or not is_nil(entity.deleted_at) or
         is_nil(Canonical.payload_object(entity)) do
      false
    else
      child = Map.get(ctx.project_children, {@delivery_entity_type, entity_id})

      if is_nil(child) or not is_nil(child.deleted_at) do
        false
      else
        project = Map.get(ctx.projects, child.project_id)

        if is_nil(project) or not is_nil(project.deleted_at) do
          false
        else
          Scope.can_access_project?(ctx.scope, project.project_id, project.customer_id)
        end
      end
    end
  end
end
