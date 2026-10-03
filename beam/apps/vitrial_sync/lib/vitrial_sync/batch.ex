defmodule VitrialSync.Batch do
  @moduledoc """
  The walk over one push batch: `_apply_push_once`'s loop
  (`app/sync_service.py:227-476`), made pure.

  The records are sorted into `VitrialSync.EntityOrder` order -- canonical
  parents before the children that reference them, original index breaking
  ties -- and walked once. Each record goes through
  `VitrialSync.Admission.decide/6` (steps 1-4, then `VitrialSync.Authority`
  steps 5-8), and every record settles into exactly one outcome, one event,
  and zero or more database effects.

  The full step table, and where each step lives here:

  | # | step | where |
  |---|---|---|
  | 1 | missing idempotency key -> `missing_client_mutation_id`, nothing written | `Admission` |
  | 2 | prior `clientMutationID` -> `idempotent_replay` / `rejected_mutation_id_reuse` / `mutation_id_collision`, nothing written | `Admission` |
  | 3 | optimistic concurrency -> `stale_revision`, mutation + fingerprint rows | `Admission` |
  | 4 | payload decode -> `invalid_mutation`, mutation + fingerprint rows | `Admission` |
  | -- | **NOT PORTED: `validate_assembly_contract_mutation`** (`app/sync_service.py:319-323`, `app/assembly_contract.py`, the validator at `:294-330`) runs here in the reference, immediately after decode and before the item-delete guard. Until its own vertical lands, a decode-valid but assembly-contract-invalid `configuration`/`quotation` payload proceeds into `Authority` here where the reference rejects it as `invalid_mutation`. Tracked as VITR-V015 in `trackers/vitrial-api-TRACKER.md`; the drift guard pins the reference's decode -> validate order so this gap cannot silently close on the reference side either. | -- |
  | 5 | item-delete guard | `Authority` |
  | 6 | lifecycle (every type except `delivery_execution`) | `Authority` |
  | 7 | authority: the delivery gate, or the generic ownership plan | `Authority` |
  | 8 | evidence GC enqueue | `Authority` |
  | 9 | commit: revision = max + 1, the canonical upsert, mutation + fingerprint + changelog rows | this module, as `Effects` |

  ## What this module deliberately does NOT do

  Revision assignment, the canonical row write, the mutation row and the
  changelog write are database effects, and a pure function cannot do them.
  They come back in `%Effects{}` for the caller to apply in ONE transaction --
  the same transaction the reference's `await db.commit()` ends
  (`app/sync_service.py:445`). Three further pieces of `_apply_push_once` are
  caller work by the same argument and are returned as data instead of being
  done here: the `sync_mutations_total` increment and `sync.mutation_result`
  log per event (`app/metrics.py:396-402`, `app/sync_service.py:454-456`),
  and the post-commit `max(SyncChangeLog.sequence)` read that `finish/2`
  turns into `nextCursor` (`app/sync_service.py:458-462`).

  ## The working scope

  `EffectiveScope.from_principal(principal)` is taken once at entry
  (`app/sync_service.py:237`) and then WIDENED as the walk proceeds:
  `apply_ownership_plan` mutates the scope object in place on every generic
  create (`scope.customer_ids.add(plan.entity_id)` at `app/ownership.py:865`,
  `scope.project_ids.add` at `:884`, unconditional on the create arm). That
  entry-scope object is what `authorize_record` and
  `authorize_delivery_execution` read for every LATER record
  (`app/delivery_execution.py:770`), so the reference's flagship behaviour --
  one batch creating a Customer and then a Project under it, in arbitrary
  client order -- depends on it (`VitrialSync.EntityOrder`'s moduledoc,
  `ownership.ex`'s "two halves"). A scope frozen at entry rejects that batch.

  The widening derives from the accepted record's PLAN create arms, not from
  the containment-gated `scope_grant` delta: `_grant_created_scope`
  (`app/ownership.py:984-1010`) grants a membership bump only when the id was
  not already in the membership, while the scope widening at `:865`/`:884` is
  unconditional on the create arm. Deriving the widening from the gated delta
  would diverge in the seeded-membership edge, where the reference widens the
  scope but grants no bump. Delivery records widen nothing
  (`apply_delivery_execution_ownership`, `app/delivery_execution.py:849`,
  writes a project-child row and nothing else).

  ## Commit visibility inside the walk

  The reference's per-accept `await db.flush()` (`app/sync_service.py:443`)
  is what makes each record's writes visible to the next: a second record
  reusing the first's `clientMutationID` must see the row the first wrote,
  and an update following a create must see the create's NEW revision. The
  walk threads the same visibility through its `Store`: accepted and rejected
  mutations land in `priors`/`fingerprints`, canonical upserts land in the
  world, the membership accumulates grants, and `max_revision` advances.
  `tests/test_push_preread_deferred.py:240-279` is the reference test for
  exactly this, and `batch_test.exs` pins both sides of it.
  """

  alias VitrialSync.{
    Admission,
    Canonical,
    Cursor,
    EntityOrder,
    Fingerprint,
    MutationEvent,
    Ownership,
    Record
  }

  @sync_capability "sync"

  defmodule Store do
    @moduledoc """
    The per-request storage the walk reads, as data.

    `priors` and `fingerprints` are the `SyncMutation` and
    `SyncMutationFingerprint` rows already committed, keyed by
    `clientMutationID`; `world` is the canonical state; `max_revision` is the
    highest `server_revision` the organization has committed, which
    `_next_revision` (`app/sync_service.py:102-108`) reads as
    `coalesce(max(server_revision), 0) + 1` on every accept.
    """

    defstruct [:priors, :fingerprints, :world, :max_revision]

    @type t :: %__MODULE__{
            priors: %{optional(String.t()) => Admission.prior()},
            fingerprints: %{optional(String.t()) => String.t()},
            world: Canonical.World.t(),
            max_revision: non_neg_integer()
          }
  end

  defmodule Effects do
    @moduledoc """
    Every database effect the walk decided, for the caller to apply in one
    transaction.

    The lists are in processing order. `organization_authorization_revision`
    is the FINAL threaded integer -- the entry value plus one per accepted
    record whose ownership effects carry `authorization_revision_bump?` --
    because `_grant_created_scope` bumps the organization row once per grant
    whose membership delta is non-empty (`app/ownership.py:1002-1010`), so
    two grants in one batch bump by 2 and the caller writes that one value
    rather than reading a boolean per grant.
    """

    defstruct mutation_rows: [],
              fingerprint_rows: [],
              entity_upserts: [],
              changelog_rows: [],
              membership_grants: [],
              evidence_gc_rows: [],
              organization_authorization_revision: 0

    @type t :: %__MODULE__{
            mutation_rows: [map()],
            fingerprint_rows: [map()],
            entity_upserts: [map()],
            changelog_rows: [map()],
            membership_grants: [
              %{optional(:customer_ids) => [String.t()], optional(:project_ids) => [String.t()]}
            ],
            evidence_gc_rows: [VitrialSync.EvidenceGC.request()],
            organization_authorization_revision: non_neg_integer()
          }
  end

  defmodule Result do
    @moduledoc """
    `SyncResult` (`app/schemas.py:97-100`): the push wire response.

    `wire/1` renders it under the wire's three keys. `next_cursor` stays
    `nil` until `VitrialSync.Batch.finish/2` -- any default computed inside
    the walk (say `max_revision + accepted_count`) diverges whenever a
    concurrent commit interleaves the change log's global autoincrement
    (`app/models.py:214`), which is why the reference reads the sequence
    only after the commit (`app/sync_service.py:458-462`).
    """

    defstruct [:accepted_record_ids, :rejected_record_ids, :next_cursor]

    @type t :: %__MODULE__{
            accepted_record_ids: [String.t()],
            rejected_record_ids: [String.t()],
            next_cursor: String.t() | nil
          }

    @spec wire(t()) :: %{
            required(String.t()) => [String.t()] | String.t() | nil
          }
    def wire(%__MODULE__{} = result) do
      %{
        "acceptedRecordIDs" => result.accepted_record_ids,
        "rejectedRecordIDs" => result.rejected_record_ids,
        "nextCursor" => result.next_cursor
      }
    end
  end

  defmodule Run do
    @moduledoc """
    One walk of one batch.

    `results` is keyed by ORIGINAL batch index (the reference's
    `outcomes[original_index]`, `app/sync_service.py:238`), `events` is in
    PROCESSING order (the reference appends inside the sorted loop), and
    `result` is `nil` until `VitrialSync.Batch.finish/2` runs with the
    caller's post-commit sequence read.
    """

    defstruct [:records, :results, :events, :effects, :result]

    @type t :: %__MODULE__{
            records: [Record.t()],
            results: %{optional(non_neg_integer()) => Admission.Outcome.t()},
            events: [MutationEvent.t()],
            effects: Effects.t(),
            result: Result.t() | nil
          }
  end

  @doc """
  Walk one batch.

  Returns `{:ok, run}`, or `{:error, :capability_missing}` when the principal
  does not hold the `sync` capability (`app/sync_service.py:228-229`), or
  `{:error, :organization_unavailable}` when the store's world has no
  organization row (`app/sync_service.py:231-235` -- the reference locks the
  row `for update` before anything else and refuses to proceed without it).
  """
  @spec run([Record.t()], String.t() | nil, Canonical.Principal.t(), Store.t()) ::
          {:ok, Run.t()} | {:error, :capability_missing} | {:error, :organization_unavailable}
  def run(records, device_id, principal, %Store{} = store) do
    with :ok <- capability_guard(principal),
         :ok <- organization_guard(store) do
      {:ok, walk(records, device_id, principal, store)}
    end
  end

  @doc """
  Build the wire response, given the caller's post-commit
  `max(SyncChangeLog.sequence)` read.

  `acceptedRecordIDs` and `rejectedRecordIDs` are emitted in the client's
  ORIGINAL order regardless of processing order
  (`app/sync_service.py:463-465`); every record that did not land `accepted`
  is in the rejected list, because `outcomes.get(index) is not True` is the
  reference's filter. `nil` reads as sequence 0 -- `int(seq or 0)` at `:466`.
  """
  @spec finish(Run.t(), non_neg_integer() | nil) :: Run.t()
  def finish(%Run{} = run, max_sequence) do
    by_original_order =
      run.records
      |> Enum.with_index()
      |> Enum.map(fn {record, index} -> {record, Map.fetch!(run.results, index)} end)

    accepted =
      for {record, outcome} <- by_original_order, outcome.status == :accepted, do: record.id

    rejected =
      for {record, outcome} <- by_original_order, outcome.status != :accepted, do: record.id

    result = %Result{
      accepted_record_ids: accepted,
      rejected_record_ids: rejected,
      next_cursor: Cursor.encode(max_sequence || 0)
    }

    %{run | result: result}
  end

  # ---------------------------------------------------------------------------
  # the walk
  # ---------------------------------------------------------------------------

  defp capability_guard(principal) do
    if Canonical.capable?(principal, @sync_capability),
      do: :ok,
      else: {:error, :capability_missing}
  end

  defp organization_guard(%Store{world: %Canonical.World{organization: nil}}),
    do: {:error, :organization_unavailable}

  defp organization_guard(%Store{}), do: :ok

  defp walk(records, device_id, principal, %Store{} = store) do
    scope = Canonical.Scope.from_principal(principal)

    entry_revision =
      case store.world.organization do
        %Canonical.Organization{authorization_revision: revision} -> revision
        nil -> 0
      end

    state = %{
      store: store,
      scope: scope,
      device_id: device_id,
      effects: %Effects{organization_authorization_revision: entry_revision},
      results: %{},
      events: []
    }

    # `Enum.with_index` yields `{record, index}`; `EntityOrder.sort_records/1`
    # orders `{index, record}` pairs -- the flip is the call site's job, here
    # and everywhere else the two meet.
    ordered =
      records
      |> Enum.with_index()
      |> Enum.map(fn {record, index} -> {index, record} end)
      |> EntityOrder.sort_records()

    state =
      Enum.reduce(ordered, state, fn {index, record}, acc ->
        walk_record(acc, index, record, principal)
      end)

    %Run{
      records: records,
      results: state.results,
      events: Enum.reverse(state.events),
      effects: state.effects,
      result: nil
    }
  end

  defp walk_record(state, index, record, principal) do
    store = state.store
    # `db.get(SyncEntity, (org, entityType, entityID))` -- from the threaded
    # world, so a record sees what earlier records in this batch wrote.
    current = Canonical.entity(store.world, record.entity_type, record.entity_id)

    context = %{
      prior: Map.get(store.priors, record.client_mutation_id),
      prior_fingerprint: Map.get(store.fingerprints, record.client_mutation_id),
      entity_exists: not is_nil(current),
      current_server_revision: if(current, do: current.server_revision, else: nil)
    }

    case Admission.decide(record, context, current, principal, state.scope, store.world) do
      %Admission.Outcome{} = outcome ->
        # Steps 1-4 settled it. Rows go out only when the outcome says the
        # arm writes them: step 1 and step 2 never do, steps 3 and 4 always.
        state
        |> write_mutation_if(record, principal, outcome)
        |> settle(index, record, outcome)

      {:rejected, code} ->
        # Steps 5-8 refused. The arm itself means the row is ALWAYS written
        # (`app/sync_service.py:390-393`) and resultServerRevision comes from
        # this batch's threaded view of the entity (`:395-401`).
        outcome = %Admission.Outcome{
          status: :rejected,
          reason: code,
          result_revision: revision_of(current),
          writes_mutation_row?: true,
          payload: nil
        }

        state
        |> write_mutation(record, principal, "rejected", outcome.result_revision)
        |> settle(index, record, outcome)

      {:ok, effects} ->
        accept(state, index, record, principal, effects)
    end
  end

  # Steps 1-4 outcome: settle it, writing the mutation + fingerprint rows only
  # on the arms that write them.
  defp settle(state, index, record, outcome) do
    store =
      if outcome.writes_mutation_row? do
        # The row the NEXT record reads: a duplicate `clientMutationID` later
        # in the same batch must find this rejection/decoding already spent.
        put_priors(state.store, record, to_string(outcome.status), outcome.result_revision)
      else
        state.store
      end

    %{state | store: store}
    |> record_outcome(index, record, outcome)
  end

  # The accept path, `app/sync_service.py:404-443`.
  defp accept(state, index, record, principal, effects) do
    revision = state.store.max_revision + 1
    payload = Map.fetch!(effects, :payload)
    tombstone? = Record.tombstone?(record)

    # 406-421: the canonical row, payload nil'd out on a tombstone.
    entity_row = %{
      organization_id: principal.organization_id,
      entity_type: record.entity_type,
      entity_id: record.entity_id,
      server_revision: revision,
      payload: if(tombstone?, do: nil, else: payload),
      updated_at: record.updated_at,
      deleted_at: record.deleted_at
    }

    # 427-434
    changelog_row = %{
      organization_id: principal.organization_id,
      entity_type: record.entity_type,
      entity_id: record.entity_id,
      server_revision: revision,
      client_mutation_id: record.client_mutation_id,
      operation: if(tombstone?, do: "delete", else: "upsert")
    }

    outcome = %Admission.Outcome{
      status: :accepted,
      reason: "committed",
      result_revision: revision,
      writes_mutation_row?: true,
      payload: payload
    }

    state
    |> write_mutation(record, principal, "accepted", revision)
    |> advance_world(effects, entity_row, revision)
    |> apply_grant(effects)
    |> widen_working_scope(effects[:plan])
    |> add_effects(entity_row, changelog_row, effects.evidence_gc)
    |> settle(index, record, outcome)
  end

  defp revision_of(nil), do: nil
  defp revision_of(%Canonical.Entity{server_revision: revision}), do: revision

  # `_record_mutation` (`app/sync_service.py:151-171`): one mutation row and
  # one fingerprint row, and both become visible to the rest of the batch --
  # `db.add` inside the loop plus the per-accept flush is what does that in
  # the reference.
  defp write_mutation(state, record, principal, status, result_revision) do
    # `struct!/2` rather than struct-update syntax: `state` is a plain map, so
    # the compiler cannot see that `state.effects` is an `%Effects{}`.
    effects =
      struct!(
        state.effects,
        mutation_rows:
          state.effects.mutation_rows ++
            [mutation_row(record, principal, status, result_revision, state.device_id)],
        fingerprint_rows: state.effects.fingerprint_rows ++ [fingerprint_row(record, principal)]
      )

    %{state | effects: effects, store: put_priors(state.store, record, status, result_revision)}
  end

  defp write_mutation_if(
         state,
         record,
         principal,
         %Admission.Outcome{writes_mutation_row?: true} = outcome
       ) do
    write_mutation(state, record, principal, to_string(outcome.status), outcome.result_revision)
  end

  defp write_mutation_if(state, _record, _principal, %Admission.Outcome{
         writes_mutation_row?: false
       }),
       do: state

  defp mutation_row(record, principal, status, result_revision, device_id) do
    # `_mutation_row`, `app/sync_service.py:127-148`.
    %{
      organization_id: principal.organization_id,
      client_mutation_id: record.client_mutation_id,
      submitted_by_user_id: principal.user_id,
      membership_id: principal.membership_id,
      session_id: principal.session_id,
      authorization_revision: principal.authorization_revision,
      device_id: device_id,
      entity_type: record.entity_type,
      entity_id: record.entity_id,
      base_server_revision: record.base_server_revision,
      result_server_revision: result_revision,
      result_status: status
    }
  end

  defp fingerprint_row(record, principal) do
    %{
      organization_id: principal.organization_id,
      client_mutation_id: record.client_mutation_id,
      request_fingerprint: Fingerprint.of(Record.mutation(record))
    }
  end

  defp put_priors(%Store{} = store, record, status, result_revision) do
    prior = %{
      entity_type: record.entity_type,
      entity_id: record.entity_id,
      base_server_revision: record.base_server_revision,
      result_status: status,
      result_server_revision: result_revision
    }

    %Store{
      store
      | priors: Map.put(store.priors, record.client_mutation_id, prior),
        fingerprints:
          Map.put(
            store.fingerprints,
            record.client_mutation_id,
            Fingerprint.of(Record.mutation(record))
          )
    }
  end

  # The store the NEXT record reads: the canonical mirror row, the
  # ownership-plan rows, `max_revision`. This is `await db.flush()` at
  # `app/sync_service.py:443` -- per-accept, so the rest of the batch sees it.
  defp advance_world(state, effects, entity_row, revision) do
    entities = upsert_by(state.store.world.entities, entity_row, &{&1.entity_type, &1.entity_id})
    world = apply_canonical_effects(%{state.store.world | entities: entities}, effects)
    %{state | store: %{state.store | world: world, max_revision: revision}}
  end

  defp record_outcome(state, index, record, outcome) do
    %{
      state
      | results: Map.put(state.results, index, outcome),
        events: state.events ++ [MutationEvent.of(record, state.device_id, outcome)]
    }
  end

  # -- world threading -------------------------------------------------------

  defp apply_canonical_effects(world, %{authority: :generic, ownership: %{upserts: upserts}})
       when is_list(upserts) do
    Enum.reduce(upserts, world, &apply_upsert/2)
  end

  # Delivery ownership writes exactly one project-child row and never widens
  # scope (`apply_delivery_execution_ownership`, `app/delivery_execution.py:849`).
  defp apply_canonical_effects(world, %{authority: :delivery, ownership: %{project_child: child}}) do
    row = %Canonical.ProjectChild{
      entity_type: child.entity_type,
      entity_id: child.entity_id,
      project_id: child.project_id,
      deleted_at: child.deleted_at
    }

    %{
      world
      | project_children: upsert_by(world.project_children, row, &{&1.entity_type, &1.entity_id})
    }
  end

  defp apply_canonical_effects(world, _effects), do: world

  # The ownership upsert tuples `VitrialSync.Ownership.apply_plan/3` produces.
  defp apply_upsert({:customer, customer_id, deleted_at}, world) do
    row = %Canonical.Customer{customer_id: customer_id, deleted_at: deleted_at}
    %{world | customers: upsert_by(world.customers, row, & &1.customer_id)}
  end

  defp apply_upsert({:project, project_id, customer_id, deleted_at}, world) do
    row = %Canonical.Project{
      project_id: project_id,
      customer_id: customer_id,
      deleted_at: deleted_at
    }

    %{world | projects: upsert_by(world.projects, row, & &1.project_id)}
  end

  defp apply_upsert({:project_sector, id, project_id, sector_id, deleted_at}, world) do
    row = %Canonical.ProjectSector{
      project_sector_id: id,
      project_id: project_id,
      sector_id: sector_id,
      deleted_at: deleted_at
    }

    %{world | project_sectors: upsert_by(world.project_sectors, row, & &1.project_sector_id)}
  end

  defp apply_upsert({:item, id, project_id, project_sector_id, deleted_at}, world) do
    row = %Canonical.Item{
      item_id: id,
      project_id: project_id,
      project_sector_id: project_sector_id,
      deleted_at: deleted_at
    }

    %{world | items: upsert_by(world.items, row, & &1.item_id)}
  end

  defp apply_upsert({:project_child, entity_type, entity_id, project_id, deleted_at}, world) do
    row = %Canonical.ProjectChild{
      entity_type: entity_type,
      entity_id: entity_id,
      project_id: project_id,
      deleted_at: deleted_at
    }

    %{
      world
      | project_children: upsert_by(world.project_children, row, &{&1.entity_type, &1.entity_id})
    }
  end

  defp apply_upsert({:item_child, entity_type, entity_id, item_id, deleted_at}, world) do
    row = %Canonical.ItemChild{
      entity_type: entity_type,
      entity_id: entity_id,
      item_id: item_id,
      deleted_at: deleted_at
    }

    %{world | item_children: upsert_by(world.item_children, row, &{&1.entity_type, &1.entity_id})}
  end

  defp upsert_by(rows, row, key_fun) do
    # New row first: `Canonical`'s accessors scan from the head, so the row
    # this record wrote is the one the next record reads. The reference's
    # `db.get` + `db.add` / in-place mutation is the same upsert-by-key.
    [row | Enum.reject(rows, &(key_fun.(&1) == key_fun.(row)))]
  end

  # -- the membership grant and the bump -------------------------------------

  # `_grant_created_scope` (`app/ownership.py:984-1010`): the membership-row
  # effect and the organization bump, one each per non-empty grant. The grant
  # is ALSO threaded into the world, so the per-record containment gate sees
  # prior grants -- the fresh `db.get(Membership, ...)` at `:991` reads back
  # what earlier records in the same batch wrote. Delivery effects carry no
  # `scope_grant` and never match.
  defp apply_grant(state, %{ownership: %{scope_grant: grant, authorization_revision_bump?: true}}) do
    membership = state.store.world.membership || %Canonical.Membership{}

    membership = %{
      membership
      | customer_ids: membership.customer_ids ++ grant.customer_ids,
        project_ids: membership.project_ids ++ grant.project_ids
    }

    effects =
      struct!(
        state.effects,
        membership_grants: state.effects.membership_grants ++ [grant],
        organization_authorization_revision: state.effects.organization_authorization_revision + 1
      )

    %{
      state
      | effects: effects,
        store: %{state.store | world: %{state.store.world | membership: membership}}
    }
  end

  defp apply_grant(state, _effects), do: state

  # -- the working scope -----------------------------------------------------

  # `apply_ownership_plan` widens the ENTRY scope object in place on every
  # generic create -- `app/ownership.py:865` for a customer, `:884` for a
  # project, unconditional on the create arm. Deliberately NOT derived from
  # the containment-gated `scope_grant` delta; see the moduledoc. Delivery
  # carries no plan and widens nothing.
  defp widen_working_scope(%{scope: scope} = state, %Ownership.Plan{
         entity_type: "customer",
         is_create: true,
         entity_id: entity_id
       }) do
    %{state | scope: %{scope | customer_ids: MapSet.put(scope.customer_ids, entity_id)}}
  end

  defp widen_working_scope(%{scope: scope} = state, %Ownership.Plan{
         entity_type: "project",
         is_create: true,
         entity_id: entity_id
       }) do
    %{state | scope: %{scope | project_ids: MapSet.put(scope.project_ids, entity_id)}}
  end

  defp widen_working_scope(state, _plan), do: state

  # -- effects accumulation ---------------------------------------------------

  defp add_effects(state, entity_row, changelog_row, evidence_gc) do
    effects =
      struct!(
        state.effects,
        entity_upserts: state.effects.entity_upserts ++ [entity_row],
        changelog_rows: state.effects.changelog_rows ++ [changelog_row],
        evidence_gc_rows:
          if evidence_gc do
            state.effects.evidence_gc_rows ++ [evidence_gc]
          else
            state.effects.evidence_gc_rows
          end
      )

    %{state | effects: effects}
  end
end
