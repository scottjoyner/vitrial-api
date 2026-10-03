defmodule VitrialSync.Db.Writes do
  @moduledoc """
  The write direction: `%VitrialSync.Batch.Effects{}` to `{sql, params}` pairs,
  for the caller to run inside ONE transaction (`app/sync_service.py:445`).

  This is the whole "ORM layer" this estate has, deliberately. The SQL is a
  literal string beside each encoder — there is no DSL to read past, no query
  builder to debug, and nothing to generate migrations. The doctrine rejects
  Ecto for exactly this reason (`vitrial-phase0/DEPENDENCY-DOCTRINE.md §3.2`):
  these writes are the tail of a hand-rolled state machine and read best as the
  SQL they are.

  Every parameter goes through `VitrialSync.Db.Codec.encode_param/1`, which is
  what makes the nil-bind connection-kill impossible here: `nil` becomes `:null`
  at every site, including the nullable `base_server_revision`,
  `result_server_revision`, `client_mutation_id`, `payload_json` and
  `deleted_at` columns — not just where convenient.

  ## The two holes in the Effects rows, filled at the boundary

  `VitrialSync.EvidenceGC.request` carries only
  `document_id/storage_provider/object_key/reason/grace_seconds`
  (`evidence_gc.ex:55-63`) — the decision is pure, so it carries no
  organization and no wall clock. The `evidence_object_gc` row needs both:

    * `organization_id` is sourced from the CALLER's organization context —
      the same value the reference copies off the blob row
      (`app/evidence_gc.py:65`), whose organization is the principal's;
    * `not_before = now + grace_seconds` is computed at write time, inside the
      transaction — exactly where the reference computes it (`app/evidence_gc.py:70`)
      and exactly what the request struct's own typedoc says the caller owes.

  Similarly, the `membership_grants` deltas are applied with an array append on
  the server's current value — the reference reads-modifies-writes
  `membership.customer_ids` inside the transaction (`app/ownership.py:1000-1006`),
  so the SQL does the append rather than trusting a stale client-side list.

  ## Deliberately open: `payload_json` stored bytes

  The reference stores SQLAlchemy's `json.dumps` output. Whether pull-page
  re-serialization makes stored-byte parity CONTRACTUAL is pinned against the
  reference in the next slice; until then `VitrialSync.JSON.encode/1` output is
  what lands, encode/decode is symmetric, and round-trip tests pin THIS side
  without claiming parity with the reference's bytes. If parity turns out
  contractual, the fix is one named encoder change here — that is the point of
  writing it down.
  """

  alias VitrialSync.Batch.Effects
  alias VitrialSync.Db.Codec

  @type statement :: {String.t(), [term()]}

  @doc """
  Render every effect as ordered `{sql, params}` statements.

  Order matches the Effects list order (processing order, parents before
  children via `EntityOrder`), with the membership grant, the GC rows and the
  organization revision bump last — the bump is a property of the whole batch,
  not of any record. `org_id` and `membership_id` come from the caller's
  principal context (the Effects carry neither); `now` is the transaction's
  timestamp, used for `not_before`.
  """
  @spec effects(
          Effects.t(),
          org_id :: String.t(),
          membership_id :: String.t(),
          now :: DateTime.t()
        ) :: [
          statement()
        ]
  def effects(%Effects{} = fx, org_id, membership_id, %DateTime{} = now) do
    mutation_inserts = Enum.map(fx.mutation_rows, &mutation_insert/1)
    fingerprint_inserts = Enum.map(fx.fingerprint_rows, &fingerprint_insert/1)
    entity_upserts = Enum.map(fx.entity_upserts, &entity_upsert/1)
    changelog_inserts = Enum.map(fx.changelog_rows, &changelog_insert/1)

    grants =
      Enum.map(fx.membership_grants, &membership_grant(&1, membership_id))

    gc_inserts = Enum.map(fx.evidence_gc_rows, &evidence_gc_insert(&1, org_id, now))

    revision_update =
      if fx.organization_authorization_revision > 0 do
        [organization_revision_update(fx.organization_authorization_revision, org_id)]
      else
        []
      end

    mutation_inserts ++
      fingerprint_inserts ++
      entity_upserts ++
      changelog_inserts ++
      grants ++
      gc_inserts ++
      revision_update
  end

  @doc ~S"""
  One `sync_mutations` insert. `received_at` is server-clocked
  (`server_default=func.now()`, `app/models.py:208`) and `id` is the identity
  column; the twelve application columns are bound explicitly.
  """
  @spec mutation_insert(map()) :: statement()
  def mutation_insert(row) do
    {"INSERT INTO sync_mutations
      (organization_id, client_mutation_id, submitted_by_user_id, membership_id,
       session_id, authorization_revision, device_id, entity_type, entity_id,
       base_server_revision, result_server_revision, result_status)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
     [
       encode(row.organization_id),
       encode(row.client_mutation_id),
       encode(row.submitted_by_user_id),
       encode(row.membership_id),
       encode(row.session_id),
       encode(row.authorization_revision),
       encode(row.device_id),
       encode(row.entity_type),
       encode(row.entity_id),
       encode(row.base_server_revision),
       encode(row.result_server_revision),
       encode(row.result_status)
     ]}
  end

  @doc "One `sync_mutation_fingerprints` insert (3 columns, composite PK)."
  @spec fingerprint_insert(map()) :: statement()
  def fingerprint_insert(row) do
    {"INSERT INTO sync_mutation_fingerprints (organization_id, client_mutation_id, request_fingerprint)
      VALUES ($1, $2, $3)",
     [
       encode(row.organization_id),
       encode(row.client_mutation_id),
       encode(row.request_fingerprint)
     ]}
  end

  @doc ~S"""
  One `sync_entities` upsert on its composite PK
  `(organization_id, entity_type, entity_id)`.

  `payload` binds as JSON bytes or `:null` — nil'd out on a tombstone by the
  walk (`batch.ex:380`). `updated_at`/`deleted_at` arrive as ISO-8601 strings
  from the wire record (`Record.t`) and are converted here: a raw string bound
  to `timestamptz` would reach `epgsql_idatetime:timestamp2i/1` as
  `function_clause` and KILL the connection, which is the failure class
  `VitrialSync.Db.Codec` exists to make impossible.
  """
  @spec entity_upsert(map()) :: statement()
  def entity_upsert(row) do
    {"INSERT INTO sync_entities
      (organization_id, entity_type, entity_id, server_revision, schema_version,
       payload_json, updated_at, deleted_at)
     VALUES ($1, $2, $3, $4, 1, $5, $6, $7)
     ON CONFLICT (organization_id, entity_type, entity_id)
     DO UPDATE SET server_revision = EXCLUDED.server_revision,
                   schema_version = EXCLUDED.schema_version,
                   payload_json = EXCLUDED.payload_json,
                   updated_at = EXCLUDED.updated_at,
                   deleted_at = EXCLUDED.deleted_at",
     [
       encode(row.organization_id),
       encode(row.entity_type),
       encode(row.entity_id),
       encode(row.server_revision),
       encode(row.payload),
       timestamp_param(row.updated_at),
       timestamp_param(row.deleted_at)
     ]}
  end

  @doc ~S"""
  One `sync_change_log` insert. `sequence` is the identity column the post-commit
  `max(sequence)` read keys on (`app/sync_service.py:458-462`), `created_at` is
  server-clocked.
  """
  @spec changelog_insert(map()) :: statement()
  def changelog_insert(row) do
    {"INSERT INTO sync_change_log
      (organization_id, entity_type, entity_id, server_revision, client_mutation_id, operation)
     VALUES ($1, $2, $3, $4, $5, $6)",
     [
       encode(row.organization_id),
       encode(row.entity_type),
       encode(row.entity_id),
       encode(row.server_revision),
       encode(row.client_mutation_id),
       encode(row.operation)
     ]}
  end

  @doc ~S"""
  One membership grant: server-side append of the delta onto the CURRENT lists.

  The grant delta carries only the new ids (`batch.ex:615-636`); the reference
  appends onto the row it re-fetched inside the transaction
  (`app/ownership.py:1000-1006`), so the SQL appends onto the stored value with
  the `||` operator (cast through `jsonb` for the append, back to `json` for the
  column) rather than rebinding a client-side copy that a concurrent push could
  have widened first.
  """
  @spec membership_grant(
          %{optional(:customer_ids) => [String.t()], optional(:project_ids) => [String.t()]},
          String.t()
        ) ::
          statement()
  def membership_grant(grant, membership_id) do
    {"UPDATE memberships
      SET customer_ids = (customer_ids::jsonb || $1::jsonb)::json,
          project_ids = (project_ids::jsonb || $2::jsonb)::json
      WHERE id = $3",
     [
       encode(Map.get(grant, :customer_ids, [])),
       encode(Map.get(grant, :project_ids, [])),
       encode(membership_id)
     ]}
  end

  @doc ~S"""
  One `evidence_object_gc` insert, with the two holes filled: `organization_id`
  from the caller context and `not_before = now + grace_seconds` computed here,
  inside the transaction (see the moduledoc).
  """
  @spec evidence_gc_insert(VitrialSync.EvidenceGC.request(), String.t(), DateTime.t()) ::
          statement()
  def evidence_gc_insert(request, org_id, now) do
    not_before = DateTime.add(now, max(0, request.grace_seconds), :second)

    {"INSERT INTO evidence_object_gc
      (organization_id, document_id, storage_provider, object_key, reason, not_before)
     VALUES ($1, $2, $3, $4, $5, $6)",
     [
       encode(org_id),
       encode(request.document_id),
       encode(request.storage_provider),
       encode(request.object_key),
       encode(request.reason),
       encode(not_before)
     ]}
  end

  @doc ~S"""
  The batch's one organization revision write. The FINAL threaded integer, not a
  per-record increment — `_grant_created_scope` bumps once per non-empty grant
  (`app/ownership.py:1002-1010`) and `Effects` carries the sum
  (`batch.ex:113-146`). Zero means no grant happened and nothing is written.
  """
  @spec organization_revision_update(non_neg_integer(), String.t()) :: statement()
  def organization_revision_update(revision, org_id) do
    {"UPDATE organizations SET authorization_revision = $1 WHERE id = $2",
     [encode(revision), encode(org_id)]}
  end

  defp encode(value), do: Codec.encode_param(value)

  # ISO-8601 (wire) | DateTime | nil → bind-safe parameter. nil → :null (a
  # nullable column), a string parsed to UTC, a DateTime passed through.
  defp timestamp_param(nil), do: :null

  defp timestamp_param(%DateTime{} = datetime), do: encode(datetime)

  defp timestamp_param(iso) when is_binary(iso) do
    case DateTime.from_iso8601(iso) do
      {:ok, datetime, _offset} ->
        encode(datetime)

      {:error, reason} ->
        raise ArgumentError, "not an ISO-8601 timestamp: #{inspect(iso)} (#{inspect(reason)})"
    end
  end

  defp timestamp_param(other),
    do: raise(ArgumentError, "cannot bind timestamp: #{inspect(other)}")
end
