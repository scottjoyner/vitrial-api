defmodule VitrialSync.Db.Reads do
  @moduledoc """
  The READ side of the database boundary — the half that did not exist.

  ## Why this module had to be written

  `VitrialSync.Db` claims "everything above this module talks to PostgreSQL through
  the four names here". That was true of the WRITE side and false of the read side:
  `Writes` exported seven insert/update functions and there was **no read query
  anywhere in the estate** — not in `Rows`, not in `Db`, not in `Pool`. `Rows` is a
  pure decoder over `(fields, cells)`, and it has always been a decoder, so nothing
  was feeding it.

  That is why 21 of 23 routes answer 501. Every engine — `Batch`, `Admission`,
  `Authority`, `Page`, `Prefetch` — is a pure function over ALREADY-LOADED rows, and
  nothing loaded them. The gap was never a decision the engines were missing; it was
  the caller that never arrived.

  ## The shape, taken from the live tests

  Not invented here. `live_db_test.exs` already proved this sequence against a real
  PostgreSQL:

      Pgconn.query(conn, select, params)
      #=> {:ok, %{rows: [[cell, ...]], row_description: fields}}
      Rows.sync_entities(fields, hd(rows))

  so a read here is that pair, wrapped so a caller does not have to remember it:

      Reads.sync_entities(conn, organization_id)

  ## Text parameters only

  Bind parameters are normalized by `Codec.normalize/1` and sent as TEXT (format
  code 0). Nothing is ever interpolated into a SQL string: the one caller of this
  module that builds SQL chooses it from `@select` below, which is a compile-time
  constant, and every value arrives as a `$n` parameter.

  ## Failures are values

  A read returns `{:ok, rows}`, `{:ok, nil}` or `{:error, reason}` — never a raise.
  A missing row is `nil`, because "no prior mutation for this clientMutationID" is
  the ordinary answer to the question `priors` asks, not an error.
  """

  alias VitrialSync.Db.{Pgconn, Rows}

  @typedoc "A connection: the pid `Pool.with_conn/2` hands the caller."
  @type conn :: pid()

  # SELECTs as compile-time constants, one per table `Batch`/`Prefetch` need. The
  # column list is spelled out rather than `SELECT *` because `Codec.decode_row/2`
  # matches positionally: a column added to the table must be a deliberate change
  # here, or the decode silently shifts.
  @select_sync_entities """
  SELECT organization_id, entity_type, entity_id, server_revision, schema_version,
         payload_json, updated_at, deleted_at
    FROM sync_entities
   WHERE organization_id = $1
  """

  # The column list came from `information_schema`, not from the Python model and
  # not from memory. The first version of this omitted four NOT NULL columns
  # (`submitted_by_user_id`, `membership_id`, `session_id`, `authorization_revision`,
  # `device_id`) and carried a `created_at` the table does not have — the table's
  # timestamp is `received_at`. Reading it wrong shows up as a 23502 on INSERT and a
  # 42703 on SELECT, which is what the live test caught.
  @select_sync_mutations """
  SELECT organization_id, client_mutation_id, submitted_by_user_id, membership_id,
         session_id, authorization_revision, device_id, entity_type, entity_id,
         base_server_revision, result_status, result_server_revision, received_at
    FROM sync_mutations
   WHERE organization_id = $1 AND client_mutation_id = $2
  """

  @select_sync_mutation_fingerprints """
  SELECT organization_id, client_mutation_id, request_fingerprint
    FROM sync_mutation_fingerprints
   WHERE organization_id = $1 AND client_mutation_id = $2
  """

  # `MAX_SYNC_PULL_SCAN_CHANGES`, `app/sync_service.py:49`.
  @max_scan_changes 500

  @select_sync_change_log """
  SELECT sequence, organization_id, entity_type, entity_id, server_revision,
         client_mutation_id, operation, created_at
    FROM sync_change_log
   WHERE organization_id = $1 AND sequence > $2
   ORDER BY sequence ASC
   LIMIT $3
  """

  @doc """
  One page of `sync_change_log` rows after `start_sequence`, oldest first.

  `app/sync_service.py:498-506` — one query, one round trip:

      SELECT SyncChangeLog
       WHERE organization_id == ...
         AND sequence > start
       ORDER BY sequence ASC
       LIMIT 500

  The `LIMIT` is `MAX_SYNC_PULL_SCAN_CHANGES = 500` (`sync_service.py:49`), and it
  is a SCAN bound rather than a page size: `Page.assemble/2` walks these changes
  and stops when the response reaches its byte ceiling, so a change past the 500th
  may be visible to a client that has not reached it yet.

  Ordering is load-bearing and not incidental. `Page.assemble/2` advances the
  cursor by MAX over the changes it consumed, and `pull_since`
  (`sync_service.py:538`) advances `max_seq` past INVISIBLE changes precisely so
  a client is not offered the same invisible rows on every pull forever.
  """
  @spec sync_change_log(conn(), String.t(), non_neg_integer(), pos_integer()) ::
          {:ok, [map()]} | {:error, term()}
  def sync_change_log(conn, organization_id, start_sequence, limit \\ @max_scan_changes) do
    all(
      conn,
      @select_sync_change_log,
      [organization_id, start_sequence, limit],
      &Rows.sync_change_log/2
    )
  end

  @doc "The scan bound above, named where the page assembly can refer to it."
  @spec max_scan_changes() :: pos_integer()
  def max_scan_changes, do: @max_scan_changes

  @select_max_sequence """
  SELECT COALESCE(MAX(sequence), 0) FROM sync_change_log WHERE organization_id = $1
  """

  @select_organizations "SELECT id, authorization_revision, name FROM organizations"
  @select_organizations_one "SELECT id, authorization_revision, name FROM organizations WHERE id = $1"

  # From `information_schema` on a migrated database, not from the Python model.
  @select_auth_sessions """
  SELECT id, organization_id, membership_id, user_id, access_token_hash,
         issued_at, expires_at, revoked_at
    FROM auth_sessions
   WHERE access_token_hash = $1
  """

  @select_memberships_one """
  SELECT id, organization_id, user_id, active, all_customers, all_projects,
         customer_ids, project_ids, roles, capabilities
    FROM memberships
   WHERE id = $1
  """
  @select_memberships "SELECT * FROM memberships"
  @select_canonical_customers "SELECT * FROM canonical_customers"
  @select_canonical_projects "SELECT * FROM canonical_projects"
  @select_canonical_project_sectors "SELECT * FROM canonical_project_sectors"
  @select_canonical_items "SELECT * FROM canonical_items"
  @select_canonical_project_children "SELECT * FROM canonical_project_children"
  @select_canonical_item_children "SELECT * FROM canonical_item_children"
  @select_evidence_blobs "SELECT * FROM evidence_object_gc"

  @doc """
  The `SyncEntity` rows for an organization, newest revision last is NOT promised
  — the batch walk looks rows up by `(organization_id, entity_type, entity_id)`, so
  this returns them keyed for that rather than as a flat list.
  """
  @spec sync_entities(conn(), String.t()) ::
          {:ok, %{optional({String.t(), String.t()}) => map()}} | {:error, term()}
  def sync_entities(conn, organization_id) when is_binary(organization_id) do
    case all(conn, @select_sync_entities, [organization_id], &Rows.sync_entities/2) do
      {:ok, rows} ->
        {:ok,
         Map.new(rows, fn row ->
           {{row.organization_id, row.entity_type, row.entity_id}, row}
         end)}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @doc """
  The `SyncMutation` prior for one `clientMutationID`, or `nil`.

  `nil` is the ordinary answer, not a failure: it is how `Admission` learns this is
  a first-time mutation rather than a replay.
  """
  @spec sync_mutation(conn(), String.t(), String.t()) :: {:ok, map() | nil} | {:error, term()}
  def sync_mutation(conn, organization_id, client_mutation_id) do
    one(
      conn,
      @select_sync_mutations,
      [organization_id, client_mutation_id],
      &Rows.sync_mutations/2
    )
  end

  @doc "The stored `request_fingerprint` for one `clientMutationID`, or `nil`."
  @spec sync_mutation_fingerprint(conn(), String.t(), String.t()) ::
          {:ok, String.t() | nil} | {:error, term()}
  def sync_mutation_fingerprint(conn, organization_id, client_mutation_id) do
    case one(
           conn,
           @select_sync_mutation_fingerprints,
           [organization_id, client_mutation_id],
           &Rows.sync_mutation_fingerprints/2
         ) do
      {:ok, nil} -> {:ok, nil}
      {:ok, row} -> {:ok, Map.get(row, :request_fingerprint)}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  `max(sync_change_log.sequence)` for an organization — the `nextCursor` source.

  `COALESCE(..., 0)` in the query rather than in Elixir, because an empty result
  set and a NULL max are the same answer and the server already knows that.
  """
  @spec max_sequence(conn(), String.t()) :: {:ok, non_neg_integer()} | {:error, term()}
  def max_sequence(conn, organization_id) do
    case Pgconn.query(conn, @select_max_sequence, [organization_id]) do
      {:ok, %{rows: [[value] | _]}} -> {:ok, decode_int(value)}
      {:ok, _other} -> {:ok, 0}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc "Every organization row, as `Canonical.Organization` needs it."
  @spec organizations(conn()) :: {:ok, [map()]} | {:error, term()}
  def organizations(conn), do: all(conn, @select_organizations, [], &Rows.organizations/2)

  @doc "Memberships for the authenticated principal's organization."
  @spec memberships(conn()) :: {:ok, [map()]} | {:error, term()}
  def memberships(conn), do: all(conn, @select_memberships, [], &Rows.memberships/2)

  @doc """
  One organization's canonical state, as `Canonical.World` carries it.

  Returns the TABLES as lists plus the ONE organization row, and builds nothing:
  `World` is a plain struct and assembling it is the caller's job.
  """
  @spec canonical(conn(), String.t()) :: {:ok, map()} | {:error, term()}
  def canonical(conn, organization_id) do
    with {:ok, organizations} <- all(conn, @select_organizations, [], &Rows.organizations/2),
         {:ok, customers} <-
           all(conn, @select_canonical_customers, [], &Rows.canonical_customers/2),
         {:ok, projects} <- all(conn, @select_canonical_projects, [], &Rows.canonical_projects/2),
         {:ok, sectors} <-
           all(conn, @select_canonical_project_sectors, [], &Rows.canonical_project_sectors/2),
         {:ok, items} <- all(conn, @select_canonical_items, [], &Rows.canonical_items/2),
         {:ok, project_children} <-
           all(conn, @select_canonical_project_children, [], &Rows.canonical_project_children/2),
         {:ok, item_children} <-
           all(conn, @select_canonical_item_children, [], &Rows.canonical_item_children/2),
         {:ok, blobs} <- all(conn, @select_evidence_blobs, [], &Rows.evidence_blobs/2) do
      {:ok,
       %{
         # ONE organization, by id. `Batch.organization_guard/1` refuses the whole
         # batch when `World.organization` is nil, so a World built without this
         # makes every push answer `:organization_unavailable` — the wiring was
         # right and the LOAD incomplete, which no unit test of either would see.
         organization: Enum.find(organizations, &(&1.id == organization_id)),
         customers: customers,
         projects: projects,
         project_sectors: sectors,
         items: items,
         project_children: project_children,
         item_children: item_children,
         evidence_blobs: blobs
       }}
    end
  end

  # -- authentication ----------------------------------------------------------
  #
  # The three rows resolving a bearer token needs: `auth_sessions`,
  # `memberships`, `organizations`. These are what the push ROUTE was missing —
  # `Handlers.sync_push/2` compiled, wired to `PushService.push/4`, and could only
  # ever answer 401, because nothing loaded the session it was supposed to resolve
  # a principal from.
  #
  # Column lists from `information_schema`, not from the Python model, which is how
  # `sync_mutations` went wrong the first time.
  #
  # NOT HERE: the resolution of those three rows into a principal. It belongs
  # ABOVE both apps — see `VitrialSync.Auth` — because putting it here would make
  # the engine's database layer depend on the authentication app, which inverts
  # the layering and drags the rest of the umbrella in behind it.

  @doc """
  The `auth_sessions` row for an `access_token_hash`, or `nil`.

  Looked up BY HASH, which is the whole point of storing it that way: the
  plaintext bearer token never reaches a comparison, so a disclosure of this
  table yields digests rather than usable credentials (`app/auth.py:24-26, 45`).
  """
  @spec auth_session(conn(), String.t()) :: {:ok, map() | nil} | {:error, term()}
  def auth_session(conn, access_token_hash) when is_binary(access_token_hash) do
    one(
      conn,
      @select_auth_sessions,
      [access_token_hash],
      &Rows.auth_sessions/2
    )
  end

  @doc "The membership a session names, or `nil`. Cross-checked against the session's own ids by `VitrialAuth`."
  @spec membership(conn(), String.t()) :: {:ok, map() | nil} | {:error, term()}
  def membership(conn, membership_id) when is_binary(membership_id) do
    one(conn, @select_memberships_one, [membership_id], &Rows.memberships/2)
  end

  @doc "One organization by id, or `nil`. The `authorization_revision` here is what `Batch` threads."
  @spec organization(conn(), String.t()) :: {:ok, map() | nil} | {:error, term()}
  def organization(conn, organization_id) when is_binary(organization_id) do
    one(conn, @select_organizations_one, [organization_id], &Rows.organizations/2)
  end

  # -- the pair, once ------------------------------------------------------------

  defp all(conn, sql, params, decoder) do
    case Pgconn.query(conn, sql, params) do
      {:ok, %{rows: rows, row_description: fields}} ->
        {:ok, Enum.map(rows, &decoder.(fields, &1))}

      {:ok, %{rows: rows}} when rows == [] ->
        # A driver that omits RowDescription for an empty result leaves nothing to
        # decode, which is correct: there are no rows.
        {:ok, []}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp one(conn, sql, params, decoder) do
    with {:ok, rows} <- all(conn, sql, params, decoder) do
      {:ok, List.first(rows)}
    end
  end

  defp decode_int(value) when is_integer(value), do: value
  defp decode_int(value) when is_binary(value), do: String.to_integer(value)
  defp decode_int(_), do: 0
end
