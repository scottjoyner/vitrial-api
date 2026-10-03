defmodule VitrialSync.PrefetchTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{Canonical, Prefetch}

  alias VitrialSync.Canonical.{
    Customer,
    Entity,
    Item,
    ItemChild,
    Principal,
    Project,
    ProjectChild,
    ProjectSector
  }

  @org "org-prefetch"

  # The six generic entity types the pull routes through `resolve_visible`, from
  # `tests/test_pull_prefetch_equivalence.py:32`. `delivery_execution` is not in
  # this list: it goes through the other twin.
  @generic_types ~w(customer project project_sector item quotation measurement)

  # `PROJECT_CHILD_TYPES` and `ITEM_CHILD_TYPES` written out rather than read from
  # `VitrialSync.Prefetch`, so the oracle below cannot inherit a mistake from the
  # module it is checking.
  @project_child_types MapSet.new(["quotation", "delivery_execution"])

  @item_child_types MapSet.new([
                      "measurement",
                      "evidence",
                      "customer_requirement",
                      "configuration",
                      "configuration_version",
                      "blocker",
                      "item_audit_event"
                    ])

  defp principal(opts \\ []) do
    %Principal{
      user_id: "user-prefetch",
      organization_id: @org,
      membership_id: "membership-prefetch",
      session_id: "session-prefetch",
      authorization_revision: 1,
      capabilities: MapSet.new(["sync"]),
      all_customers: Keyword.get(opts, :all_customers, true),
      all_projects: Keyword.get(opts, :all_projects, true),
      customer_ids: MapSet.new(Keyword.get(opts, :customer_ids, [])),
      project_ids: MapSet.new(Keyword.get(opts, :project_ids, []))
    }
  end

  defp world(attrs) do
    struct!(%Canonical.World{}, attrs)
  end

  # -- the scenario, mirroring tests/test_pull_prefetch_equivalence.py:75-163 ---

  defp scenario do
    rows =
      world(
        customers: [
          %Customer{customer_id: "cust-mine"},
          %Customer{customer_id: "cust-theirs"}
        ],
        projects: [
          %Project{project_id: "proj-mine", customer_id: "cust-mine"},
          %Project{project_id: "proj-theirs", customer_id: "cust-theirs"}
        ],
        project_sectors: [
          # A sector whose project does not exist at all.
          %ProjectSector{
            project_sector_id: "sector-orphan",
            project_id: "proj-absent",
            sector_id: "s"
          },
          %ProjectSector{
            project_sector_id: "sector-mine",
            project_id: "proj-mine",
            sector_id: "s"
          }
        ],
        items: [
          %Item{item_id: "item-mine", project_id: "proj-mine"},
          %Item{item_id: "item-orphan", project_id: "proj-absent"},
          %Item{item_id: "item-absent-parent", project_id: "proj-mine"}
        ],
        project_children: [
          %ProjectChild{entity_type: "quotation", entity_id: "q-mine", project_id: "proj-mine"},
          %ProjectChild{
            entity_type: "quotation",
            entity_id: "q-orphan",
            project_id: "proj-absent"
          }
        ],
        item_children: [
          %ItemChild{entity_type: "measurement", entity_id: "m-mine", item_id: "item-mine"},
          %ItemChild{entity_type: "measurement", entity_id: "m-no-item", item_id: "item-absent"}
        ]
      )

    principals = %{
      "broad" => principal(),
      "narrow" =>
        principal(
          all_customers: false,
          all_projects: false,
          customer_ids: ["cust-mine"],
          project_ids: ["proj-mine"]
        ),
      "customer-only" => principal(all_customers: true, all_projects: false),
      "project-no-customer" =>
        principal(all_customers: false, all_projects: false, project_ids: ["proj-mine"])
    }

    ids = [
      "cust-mine",
      "cust-theirs",
      "cust-absent",
      "proj-mine",
      "proj-theirs",
      "proj-absent",
      "sector-mine",
      "sector-orphan",
      "sector-absent",
      "item-mine",
      "item-orphan",
      "item-absent",
      "q-mine",
      "q-orphan",
      "q-absent",
      "m-mine",
      "m-no-item",
      "m-absent"
    ]

    # Every id requested for every type, which is what makes the loader load
    # every row the oracle might read.
    cases = for type <- @generic_types, id <- ids, do: {type, id}

    {rows, principals, cases}
  end

  # -- the oracle --------------------------------------------------------------

  # `record_is_visible`, `app/ownership.py:974-1014`, transcribed to answer from
  # the same loaded rows one lookup at a time instead of from `db.get`.
  #
  # This is the property being exploited by the reference's own test, inverted:
  # `record_is_visible` only ever calls `db.get`, so a stub session answers from a
  # table of rows and drives the shipped function unmodified. The BEAM tree has no
  # shipped per-record resolver to drive -- the port replaces it rather than
  # carrying it -- so the oracle is this transcription, and that is a real limit
  # on what the comparison below proves: it shows the batched twin agrees with a
  # fresh transcription of the original, not with shipped code. The set membership
  # and the scope predicates are written out here precisely so the transcription is
  # independent of the module it checks.
  defp record_is_visible(principal, entity_type, entity_id, rows) do
    scope = Canonical.Scope.from_principal(principal)

    cond do
      entity_type == "customer" ->
        customer = db_get(rows, :customers, entity_id)
        not is_nil(customer) and Canonical.Scope.can_access_customer?(scope, customer.customer_id)

      entity_type == "project" ->
        project = db_get(rows, :projects, entity_id)

        not is_nil(project) and
          Canonical.Scope.can_access_project?(scope, project.project_id, project.customer_id)

      entity_type == "project_sector" ->
        sector = db_get(rows, :sectors, entity_id)

        if is_nil(sector) do
          false
        else
          project = db_get(rows, :projects, sector.project_id)

          not is_nil(project) and
            Canonical.Scope.can_access_project?(scope, project.project_id, project.customer_id)
        end

      entity_type == "item" ->
        item = db_get(rows, :items, entity_id)

        if is_nil(item) do
          false
        else
          project = db_get(rows, :projects, item.project_id)

          not is_nil(project) and
            Canonical.Scope.can_access_project?(scope, project.project_id, project.customer_id)
        end

      MapSet.member?(@project_child_types, entity_type) ->
        child = db_get(rows, :project_children, {entity_type, entity_id})

        if is_nil(child) do
          false
        else
          project = db_get(rows, :projects, child.project_id)

          not is_nil(project) and
            Canonical.Scope.can_access_project?(scope, project.project_id, project.customer_id)
        end

      MapSet.member?(@item_child_types, entity_type) ->
        child = db_get(rows, :item_children, {entity_type, entity_id})

        if is_nil(child) do
          false
        else
          item = db_get(rows, :items, child.item_id)

          if is_nil(item) do
            false
          else
            project = db_get(rows, :projects, item.project_id)

            not is_nil(project) and
              Canonical.Scope.can_access_project?(scope, project.project_id, project.customer_id)
          end
        end

      true ->
        false
    end
  end

  # `delivery_execution_is_visible`, `app/delivery_execution.py:874-896`, same
  # transposition.
  defp delivery_execution_is_visible(principal, entity_id, rows) do
    scope = Canonical.Scope.from_principal(principal)

    entity = db_get(rows, :entities, {"delivery_execution", entity_id})

    if is_nil(entity) or not is_nil(entity.deleted_at) or
         is_nil(Canonical.payload_object(entity)) do
      false
    else
      child = db_get(rows, :project_children, {"delivery_execution", entity_id})

      if is_nil(child) or not is_nil(child.deleted_at) do
        false
      else
        project = db_get(rows, :projects, child.project_id)

        if is_nil(project) or not is_nil(project.deleted_at) do
          false
        else
          Canonical.Scope.can_access_project?(scope, project.project_id, project.customer_id)
        end
      end
    end
  end

  # `db.get(Model, primary_key)`, answered from the loaded rows. One linear scan
  # per lookup is the point: this is the shape the batched twin is not allowed to
  # have.
  defp db_get(rows, :customers, id), do: Enum.find(rows.customers, &(&1.customer_id == id))

  defp db_get(rows, :projects, id), do: Enum.find(rows.projects, &(&1.project_id == id))

  defp db_get(rows, :sectors, id),
    do: Enum.find(rows.project_sectors, &(&1.project_sector_id == id))

  defp db_get(rows, :items, id), do: Enum.find(rows.items, &(&1.item_id == id))

  defp db_get(rows, :project_children, {type, id}),
    do: Enum.find(rows.project_children, &(&1.entity_type == type and &1.entity_id == id))

  defp db_get(rows, :item_children, {type, id}),
    do: Enum.find(rows.item_children, &(&1.entity_type == type and &1.entity_id == id))

  defp db_get(rows, :entities, {type, id}),
    do: Enum.find(rows.entities, &(&1.entity_type == type and &1.entity_id == id))

  # ---------------------------------------------------------------------------
  # the equivalence the twins exist to preserve
  # ---------------------------------------------------------------------------

  describe "resolve_visible against record_is_visible, case by case" do
    test "the batched twin answers what the per-record function answers" do
      {rows, principals, cases} = scenario()

      for {name, principal} <- principals,
          {entity_type, entity_id} <- cases do
        context = Prefetch.load_page_context(principal, cases, rows)

        expected = record_is_visible(principal, entity_type, entity_id, rows)
        actual = Prefetch.resolve_visible(context, entity_type, entity_id)

        assert actual == expected,
               "principal=#{name} entity_type=#{entity_type} entity_id=#{entity_id}: " <>
                 "batched=#{actual} original=#{expected}"
      end
    end

    test "the comparison can fail: sabotaging the twin is detected" do
      # Guards against the harness agreeing with the twin for the wrong reason.
      # The twin is the new code under test, so that is the side to sabotage.
      {rows, principals, cases} = scenario()
      principal = principals["broad"]
      context = Prefetch.load_page_context(principal, cases, rows)

      assert record_is_visible(principal, "item", "item-mine", rows) == true

      sabotaged = %{context | projects: %{}}

      assert Prefetch.resolve_visible(sabotaged, "item", "item-mine") == false
    end

    test "a scope that can reach nothing is not a special case" do
      # A principal with neither customer nor project scope sees nothing, and the
      # twin and the original agree on that for every branch.
      {rows, _principals, cases} = scenario()
      nobody = principal(all_customers: false, all_projects: false)
      context = Prefetch.load_page_context(nobody, cases, rows)

      for {entity_type, entity_id} <- cases do
        assert Prefetch.resolve_visible(context, entity_type, entity_id) ==
                 record_is_visible(nobody, entity_type, entity_id, rows)
      end
    end

    test "an entity type no branch claims is invisible, exactly as the original's fall-through" do
      {rows, principals, cases} = scenario()
      principal = principals["broad"]
      context = Prefetch.load_page_context(principal, cases, rows)

      for type <- ~w(widget delivery_execution_snapshot) do
        refute Prefetch.resolve_visible(context, type, "x1")
      end
    end
  end

  # The delivery fixture: one flag per `deleted_at` hop the delivery twin checks.
  defp delivery_rows(entity_deleted, child_deleted, project_deleted) do
    stamp = "2026-09-17T14:00:00Z"

    world(
      entities: [
        %Entity{
          entity_type: "delivery_execution",
          entity_id: "de-1",
          payload: %{"id" => "de-1"},
          deleted_at: if(entity_deleted, do: stamp),
          server_revision: 1
        }
      ],
      project_children: [
        %ProjectChild{
          entity_type: "delivery_execution",
          entity_id: "de-1",
          project_id: "proj-1",
          deleted_at: if(child_deleted, do: stamp)
        }
      ],
      projects: [
        %Project{
          project_id: "proj-1",
          customer_id: "cust-1",
          deleted_at: if(project_deleted, do: stamp)
        }
      ]
    )
  end

  describe "resolve_delivery_visible against delivery_execution_is_visible" do
    test "the twin keeps the deleted_at checks at every hop that the original has" do
      principal = principal()
      changes = [{"delivery_execution", "de-1"}]

      for {label, flags} <- [
            {"clean", {false, false, false}},
            {"deleted payload", {true, false, false}},
            {"deleted child", {false, true, false}},
            {"deleted project", {false, false, true}},
            {"all deleted", {true, true, true}}
          ] do
        {entity_deleted, child_deleted, project_deleted} = flags
        rows = delivery_rows(entity_deleted, child_deleted, project_deleted)

        context = Prefetch.load_page_context(principal, changes, rows)

        expected = delivery_execution_is_visible(principal, "de-1", rows)
        actual = Prefetch.resolve_delivery_visible(context, "de-1")

        assert actual == expected, "#{label}: batched=#{actual} original=#{expected}"
      end
    end

    test "a payload column that is not an object is not a payload" do
      principal = principal()

      rows =
        delivery_rows(false, false, false)
        |> Map.update!(:entities, fn [_only] ->
          [
            %Entity{
              entity_type: "delivery_execution",
              entity_id: "de-1",
              payload: nil,
              server_revision: 1
            }
          ]
        end)

      context = Prefetch.load_page_context(principal, [{"delivery_execution", "de-1"}], rows)

      refute Prefetch.resolve_delivery_visible(context, "de-1")
      assert delivery_execution_is_visible(principal, "de-1", rows) == false
    end

    test "a project out of scope hides the record, in both" do
      narrow = principal(all_customers: true, all_projects: false)
      rows = delivery_rows(false, false, false)
      context = Prefetch.load_page_context(narrow, [{"delivery_execution", "de-1"}], rows)

      refute Prefetch.resolve_delivery_visible(context, "de-1")
      assert delivery_execution_is_visible(narrow, "de-1", rows) == false
    end
  end

  # ---------------------------------------------------------------------------
  # the asymmetry between the two twins
  # ---------------------------------------------------------------------------

  describe "the deleted_at asymmetry, preserved in both directions" do
    test "a soft-deleted generic row is still delivered" do
      # `record_is_visible`'s docstring, app/ownership.py:951-963: the client
      # cannot infer a deletion from an absence, so the tombstone has to be
      # delivered carrying `deletedAt`. Adding a deleted_at check to the generic
      # twin would make every server-side deletion permanently invisible.
      stamp = "2026-09-17T14:00:00Z"

      rows =
        world(
          customers: [%Customer{customer_id: "cust-1", deleted_at: stamp}],
          projects: [%Project{project_id: "proj-1", customer_id: "cust-1", deleted_at: stamp}],
          project_sectors: [
            %ProjectSector{project_sector_id: "sec-1", project_id: "proj-1", deleted_at: stamp}
          ],
          items: [
            %Item{
              item_id: "item-1",
              project_id: "proj-1",
              project_sector_id: "sec-1",
              deleted_at: stamp
            }
          ],
          project_children: [
            %ProjectChild{
              entity_type: "quotation",
              entity_id: "q-1",
              project_id: "proj-1",
              deleted_at: stamp
            }
          ],
          item_children: [
            %ItemChild{
              entity_type: "measurement",
              entity_id: "m-1",
              item_id: "item-1",
              deleted_at: stamp
            }
          ]
        )

      # Every type has to be on the page. `load_page_context` loads a table only
      # for the types the page asks about, so a page of quotations alone would
      # answer `customer` with "not loaded" rather than with the tombstone, and
      # this test would be measuring the loader's selection rather than the
      # twin's handling of `deleted_at`.
      principal = principal()

      page = [
        {"customer", "cust-1"},
        {"project", "proj-1"},
        {"project_sector", "sec-1"},
        {"item", "item-1"},
        {"quotation", "q-1"},
        {"measurement", "m-1"}
      ]

      context = Prefetch.load_page_context(principal, page, rows)

      for {type, id} <- [
            {"customer", "cust-1"},
            {"project", "proj-1"},
            {"project_sector", "sec-1"},
            {"item", "item-1"},
            {"quotation", "q-1"}
          ] do
        assert Prefetch.resolve_visible(context, type, id),
               "#{type}: a soft-deleted generic row is still delivered"

        assert record_is_visible(principal, type, id, rows),
               "#{type}: the original agrees"
      end

      assert Prefetch.resolve_visible(context, "measurement", "m-1")
    end

    test "the same tombstones hide a delivery record, because that question is different" do
      stamp = "2026-09-17T14:00:00Z"

      rows =
        world(
          entities: [
            %Entity{
              entity_type: "delivery_execution",
              entity_id: "de-1",
              payload: %{"id" => "de-1"},
              server_revision: 1
            }
          ],
          project_children: [
            %ProjectChild{
              entity_type: "delivery_execution",
              entity_id: "de-1",
              project_id: "proj-1",
              deleted_at: stamp
            }
          ],
          projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}]
        )

      principal = principal()
      context = Prefetch.load_page_context(principal, [{"delivery_execution", "de-1"}], rows)

      refute Prefetch.resolve_delivery_visible(context, "de-1")
    end
  end

  # ---------------------------------------------------------------------------
  # load_page_context
  # ---------------------------------------------------------------------------

  describe "load_page_context supplies every row the resolvers read" do
    test "a live, in-scope record of every type is visible after a batched load" do
      # The point of calling the loader rather than hand-building a context: an
      # earlier draft of the reference excluded `delivery_execution` from the
      # project-child load, which made every delivery record invisible with no
      # error at all, and a test that hand-populated the map stayed green.
      rows =
        world(
          customers: [%Customer{customer_id: "cust-1"}],
          projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}],
          project_sectors: [
            %ProjectSector{project_sector_id: "sec-1", project_id: "proj-1", sector_id: "s"}
          ],
          items: [%Item{item_id: "item-1", project_id: "proj-1", project_sector_id: "sec-1"}],
          item_children: [
            %ItemChild{entity_type: "evidence", entity_id: "e-1", item_id: "item-1"}
          ],
          project_children: [
            %ProjectChild{entity_type: "quotation", entity_id: "q-1", project_id: "proj-1"}
          ]
        )

      principal = principal()

      for {type, id} <- [
            {"customer", "cust-1"},
            {"project", "proj-1"},
            {"project_sector", "sec-1"},
            {"item", "item-1"},
            {"quotation", "q-1"},
            {"evidence", "e-1"}
          ] do
        context = Prefetch.load_page_context(principal, [{type, id}], rows)

        assert Prefetch.resolve_visible(context, type, id),
               "#{type}: the loader must supply every row the resolver reads"
      end
    end

    test "the delivery project child is loaded, which is the wiring that broke" do
      rows =
        world(
          entities: [
            %Entity{
              entity_type: "delivery_execution",
              entity_id: "de-1",
              payload: %{"id" => "de-1"},
              server_revision: 1
            }
          ],
          project_children: [
            %ProjectChild{
              entity_type: "delivery_execution",
              entity_id: "de-1",
              project_id: "proj-1"
            }
          ],
          projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}]
        )

      principal = principal()
      context = Prefetch.load_page_context(principal, [{"delivery_execution", "de-1"}], rows)

      assert Map.has_key?(context.project_children, {"delivery_execution", "de-1"})
      assert Map.has_key?(context.entities, {"delivery_execution", "de-1"})
      assert Map.has_key?(context.projects, "proj-1")
      assert Prefetch.resolve_delivery_visible(context, "de-1")
    end

    test "the maps are keyed the way the resolvers look them up" do
      # `app/pull_prefetch.py:140-143`: keyed by the raw `(organization_id, ...)`
      # primary key, every lookup missed.
      rows =
        world(
          customers: [%Customer{customer_id: "cust-1"}],
          projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}],
          items: [%Item{item_id: "item-1", project_id: "proj-1"}],
          project_children: [
            %ProjectChild{entity_type: "quotation", entity_id: "q-1", project_id: "proj-1"}
          ],
          entities: [
            %Entity{entity_type: "quotation", entity_id: "q-1", payload: %{}, server_revision: 1}
          ]
        )

      context =
        Prefetch.load_page_context(
          principal(),
          [{"customer", "cust-1"}, {"quotation", "q-1"}],
          rows
        )

      assert Map.keys(context.customers) == ["cust-1"]
      assert Map.keys(context.projects) == ["proj-1"]
      assert Map.keys(context.project_children) == [{"quotation", "q-1"}]
      assert Map.keys(context.entities) == [{"quotation", "q-1"}]
    end

    test "only the ids the page asks about are loaded" do
      rows =
        world(
          customers: [%Customer{customer_id: "cust-1"}, %Customer{customer_id: "cust-other"}],
          projects: [
            %Project{project_id: "proj-1", customer_id: "cust-1"},
            %Project{project_id: "proj-other", customer_id: "cust-other"}
          ]
        )

      context = Prefetch.load_page_context(principal(), [{"customer", "cust-1"}], rows)

      assert Map.keys(context.customers) == ["cust-1"]
      # No project change and nothing pointing at a project, so none is loaded.
      assert context.projects == %{}
    end

    test "a project is loaded even when it is only reachable through a sector" do
      rows =
        world(
          project_sectors: [
            %ProjectSector{project_sector_id: "sec-1", project_id: "proj-1", sector_id: "s"}
          ],
          projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}]
        )

      context = Prefetch.load_page_context(principal(), [{"project_sector", "sec-1"}], rows)

      assert Map.keys(context.projects) == ["proj-1"]
      assert Prefetch.resolve_visible(context, "project_sector", "sec-1")
    end

    test "an item reachable only from an item child is loaded, and so is its project" do
      # The second hop. An item's project id is only knowable once the item row is
      # loaded, so this cannot be folded into the project query.
      rows =
        world(
          items: [%Item{item_id: "item-1", project_id: "proj-1", project_sector_id: "sec-1"}],
          item_children: [
            %ItemChild{entity_type: "evidence", entity_id: "e-1", item_id: "item-1"}
          ],
          projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}]
        )

      context = Prefetch.load_page_context(principal(), [{"evidence", "e-1"}], rows)

      assert Map.keys(context.items) == ["item-1"]
      assert Map.keys(context.projects) == ["proj-1"]
      assert Prefetch.resolve_visible(context, "evidence", "e-1")
    end

    test "a project is loaded for a direct project change as well" do
      rows = world(projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}])

      context = Prefetch.load_page_context(principal(), [{"project", "proj-1"}], rows)

      assert Map.keys(context.projects) == ["proj-1"]
    end

    test "a change may be a tuple, a map or a page candidate" do
      rows = world(projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}])

      from_tuple = Prefetch.load_page_context(principal(), [{"project", "proj-1"}], rows)

      from_map =
        Prefetch.load_page_context(
          principal(),
          [%{entity_type: "project", entity_id: "proj-1"}],
          rows
        )

      candidate = %VitrialSync.Page.Candidate{
        sequence: 7,
        entity_type: "project",
        entity_id: "proj-1"
      }

      from_candidate = Prefetch.load_page_context(principal(), [candidate], rows)

      assert from_tuple == from_map
      assert from_candidate == from_map
    end

    test "a change with no identity is refused rather than silently dropped" do
      assert_raise ArgumentError, fn ->
        Prefetch.load_page_context(principal(), [%{sequence: 1}], Canonical.empty_world())
      end
    end

    test "the context carries the organization and the principal's own scope" do
      rows = world(customers: [%Customer{customer_id: "cust-1"}])
      narrow = principal(all_customers: false, customer_ids: ["cust-1"])

      context = Prefetch.load_page_context(narrow, [{"customer", "cust-1"}], rows)

      assert context.organization_id == @org
      assert context.scope == Canonical.Scope.from_principal(narrow)
    end
  end

  describe "the type sets the dispatch shares with the authorization path" do
    test "they are the reference's two sets" do
      assert Prefetch.project_child_types() == MapSet.new(["quotation", "delivery_execution"])
      assert Prefetch.item_child_types() == @item_child_types
      assert Prefetch.delivery_entity_type() == "delivery_execution"
    end

    test "the entity type the twins agree on is the one the delivery gate owns" do
      # `VitrialSync.Delivery.entity_type/0` refuses the generic path for
      # delivery_execution (SUSPECT-6); the pull routes it to the other twin.
      assert VitrialSync.Delivery.entity_type() == Prefetch.delivery_entity_type()
    end

    test "the generic twin still has a delivery_execution branch, because record_is_visible has one" do
      # `PROJECT_CHILD_TYPES` contains it, so `record_is_visible("delivery_execution",
      # ...)` is a valid call -- it just is not the one `pull_since` makes.
      rows =
        world(
          project_children: [
            %ProjectChild{
              entity_type: "delivery_execution",
              entity_id: "de-1",
              project_id: "proj-1"
            }
          ],
          projects: [%Project{project_id: "proj-1", customer_id: "cust-1"}]
        )

      principal = principal()
      context = Prefetch.load_page_context(principal, [{"delivery_execution", "de-1"}], rows)

      assert Prefetch.resolve_visible(context, "delivery_execution", "de-1") ==
               record_is_visible(principal, "delivery_execution", "de-1", rows)
    end
  end
end
