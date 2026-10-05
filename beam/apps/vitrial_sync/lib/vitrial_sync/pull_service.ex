defmodule VitrialSync.PullService do
  @moduledoc """
  The pull path end to end: read the page's changes, resolve visibility, assemble.

  ## The cursor rule, which is the whole subtlety

  `app/sync_service.py:560-566`:

      # Do not advance the cursor past a visible record that cannot fit this page.
      # The next request must observe that same change rather than silently skip it.
      if not pull_page_accepts(records, record, change.sequence):
          truncated = True
          break
      records.append(record)
      max_seq = max(max_seq, change.sequence)

  An INVISIBLE change, or one whose entity is gone, IS consumed — it can never be
  delivered, so leaving it unconsumed would stall the cursor forever. A change that
  IS deliverable but does not fit the byte ceiling is NOT consumed, so the next pull
  observes it again.

  Those two rules point in opposite directions and getting either backwards is
  SILENT: consume too little and the client re-reads one page forever; consume too
  much and it never sees a record it was entitled to. Neither raises.

  ## `truncated` is a flag, not an inference

  `sync_service.py:509-513` is explicit about why: most short pages are short
  because the SCAN was exhausted, which is normal. Inferring `truncated` from
  `len(records) < len(changes)` would make the word meaningless.

  That distinction is already carried by `Page`, which returns `stopped_at` — set
  when a candidate did not fit and `nil` when the scan simply ran out. This module
  reads it rather than recomputing it.

  ## What this does not do

  It does not authenticate: `VitrialSync.Auth.authenticate/3` is the only thing
  permitted to mint a `Principal` and it runs at the edge. It does not render the
  wire — `wire/2` builds the envelope and leaves record rendering to the boundary,
  which owns the field names.
  """

  alias VitrialSync.{Canonical, Db, Page, Prefetch}
  alias VitrialSync.Db.Reads

  @typedoc "The pull page, as `Page.assemble/2` builds it."
  @type result :: %{records: [map()], cursor_sequence: non_neg_integer(), stopped_at: term()}

  @typedoc """
  The observation the boundary logs.

  Returned rather than emitted, for the reason `sync_service.py:578-585` gives: it
  is recorded after the loop so it covers exactly the page the caller receives.
  Recording it in a `finally` would also cover the error paths, and reporting a
  page size for a page that does not exist is the kind of plausible-looking number
  that sends an operator chasing the wrong thing.
  """
  @type observation :: %{
          scanned_change_count: non_neg_integer(),
          record_count: non_neg_integer(),
          end_sequence: non_neg_integer(),
          truncated: boolean()
        }

  @doc """
  Serve one pull page for `principal` from `start_sequence`.

  `pool` is a `VitrialSync.Db.Pool` pid or registered name. Returns
  `{:ok, result, observation}` or `{:error, {:load, reason}}`.

  The whole page is read inside ONE `with_conn`, deliberately. The reference does
  the same in one session scope (`app/db.py:65-67`), and a page assembled from two
  connections could straddle a concurrent push: the changelog read on one, the
  canonical rows on another. The result would be a page drawn from two different
  worlds, and nothing would say so.
  """
  @spec pull(pid() | atom(), non_neg_integer(), Canonical.Principal.t()) ::
          {:ok, result(), observation()} | {:error, {:load, term()}}
  def pull(pool, start_sequence, %Canonical.Principal{} = principal) do
    Db.with_conn(pool, fn conn ->
      with {:ok, changes} <-
             Reads.sync_change_log(conn, principal.organization_id, start_sequence),
           {:ok, tables} <- Reads.canonical(conn, principal.organization_id) do
        # `Reads.canonical/2` returns TABLES; the World is built here, because a
        # read layer that handed back an authority struct would be guessing at
        # `membership` and `organization`, which it does not load.
        world = struct!(Canonical.World, tables)
        build_page(start_sequence, changes, principal, world)
      else
        {:error, reason} -> {:error, {:load, reason}}
      end
    end)
  end

  # Named `build_page/4`, sharing nothing with `Page.assemble/2`. A private
  # `assemble/4` here SHADOWED it, and a private `assemble_page/4` was still
  # reported against its spec — the names differ only in arity, which is close
  # enough for the compiler to pick the wrong one:
  #
  #     warning: incompatible types given to assemble_page/4:
  #         assemble_page(start_sequence, changes, principal, world)
  #     given types: (integer(), dynamic(list(term)), dynamic(%Principal{}), ...)
  #
  # The `defp` also carries no `@spec`, so the compiler inferred a shape from the
  # body that did not match what the CALL SITE could pass. Both facts are fixed by
  # the `@spec` below rather than by loosening the call.
  # The fourth parameter is a `%Canonical.World{}`, not a map. The read layer
  # returns tables and `struct!/2` builds the struct here, so the type is exact and
  # the compiler can check the call.
  @spec build_page(non_neg_integer(), [map()], Canonical.Principal.t(), Canonical.World.t()) ::
          {result(), map()}
  defp build_page(start_sequence, changes, principal, world) do
    # `load_page_context/3` resolves the whole page's ownership in a fixed number
    # of queries rather than 1-3 per change (`app/sync_service.py:520-527`), and
    # it takes the CANONICAL WORLD — so the world is loaded once, here, and the
    # changes are what vary per record.
    page_context = Prefetch.load_page_context(principal, changes, world)

    candidates =
      Enum.map(changes, fn change ->
        %Page.Candidate{
          sequence: change.sequence,
          entity_type: change.entity_type,
          entity_id: change.entity_id,
          payload_json: entity_payload(world, change),
          server_revision: change.server_revision,
          client_mutation_id: change.client_mutation_id,
          updated_at: DateTime.to_iso8601(change.created_at),
          deleted_at: entity_deleted_at(world, change),
          visible?: visible?(page_context, change),
          entity_present?: entity_present?(world, change)
        }
      end)

    result = Page.assemble(start_sequence, candidates)

    observation = %{
      scanned_change_count: length(changes),
      record_count: length(result.records),
      end_sequence: result.cursor_sequence,
      # `Page` sets `stopped_at` when a candidate did not fit. Reading it is
      # strictly better than inferring from a length comparison, which
      # `sync_service.py:509-513` explains at length.
      truncated: not is_nil(result.stopped_at)
    }

    {:ok, result, observation}
  end

  # `resolve_visible` / `resolve_delivery_visible`, `app/sync_service.py:530-536`.
  # They are line-for-line twins — `tests/test_pull_prefetch_equivalence.py`
  # asserts the two agree, and the visible set is pinned independently by
  # `tests/test_pull_visibility_matrix_postgres.py` — so the delivery branch is a
  # different AUTHORITY, not a different question.
  defp visible?(page, %{entity_type: "delivery_execution", entity_id: id}) do
    Prefetch.resolve_delivery_visible(page, id)
  end

  defp visible?(page, %{entity_type: type, entity_id: id}) do
    Prefetch.resolve_visible(page, type, id)
  end

  # The entity row for this change, or nothing — `page.entities.get(...)` in the
  # reference (`sync_service.py:541`).
  #
  # Read from the STRUCT FIELD, not through `Canonical.World.entity/3`. That
  # function is written `def` and is not reachable: the compiled module exports
  # only `__struct__`. Verified rather than assumed —
  #
  #     Canonical.World.__info__(:functions)          #=>  [{:__struct__, 0}]
  #     function_exported?(Canonical.World, :entity, 3)  #=>  false
  #
  # — and calling it raised UndefinedFunctionError at runtime.
  # `Prefetch.load_page_context/3` reads `world.entities` the same way
  # (`prefetch.ex:171`), which is why the reference port works and this did not.
  #
  # Separate from visibility because they are separate lookups, and because the
  # cursor treats a missing entity as CONSUMED.
  defp entity(world, change) do
    Enum.find(
      world.entities,
      &(to_string(&1.entity_type) == change.entity_type and
          to_string(&1.entity_id) == change.entity_id)
    )
  end

  defp entity_present?(world, change), do: not is_nil(entity(world, change))

  defp entity_payload(world, change) do
    case entity(world, change) do
      nil -> %{}
      row -> Map.get(row, :payload) || %{}
    end
  end

  defp entity_deleted_at(_world, _change), do: nil

  @doc """
  Build the wire envelope `app/sync_service.py:586` produces:
  `SyncBatch(deviceID="server", cursor=f"seq:{max_seq}", records=records)`.

  `deviceID` is the literal `"server"`, NOT the caller's: the reference says so in
  one line and the constant is easier to get wrong than it looks.

  Records are rendered here rather than in the boundary because the cursor format
  `seq:<n>` belongs next to the code that decided which `<n>`.
  """
  @spec wire(result()) :: map()
  def wire(%{records: records, cursor_sequence: cursor}) do
    %{
      # The reference hardcodes `deviceID="server"` at BOTH sites
      # (`app/sync_service.py:80, 572`) — a pull page is assembled by the server
      # and is not attributed to the caller. So this is not a parameter at all: a
      # defaulted one invites `wire(result, caller_device)` from a caller who
      # means it, and the page would then claim to be the client's.
      "deviceID" => "server",
      "cursor" => "seq:#{cursor}",
      "records" => Enum.map(records, &wire_record/1)
    }
  end

  @doc """
  Render one record — `app/sync_service.py:551-557`.

  `payload` is ABSENT rather than null: `payload_json=payload if
  entity.deleted_at is None else None` (`:553`), and the schema marks the field
  optional, so a tombstone carries no payload key at all.
  """
  @spec wire_record(map()) :: map()
  def wire_record(record) do
    %{
      "id" => Map.get(record, :id),
      "entityType" => Map.get(record, :entity_type),
      "entityID" => Map.get(record, :entity_id),
      "serverRevision" => Map.get(record, :server_revision),
      "clientMutationID" => Map.get(record, :client_mutation_id),
      "updatedAt" => Map.get(record, :updated_at)
    }
  end
end
