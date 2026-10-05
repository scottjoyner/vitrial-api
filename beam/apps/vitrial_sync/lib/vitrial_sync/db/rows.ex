defmodule VitrialSync.Db.Rows do
  @moduledoc """
  The row codecs for the fourteen sync-path tables, in both directions.

  ## Decode: lossless by contract

  One function per table over `(fields, cells)` — a `RowDescription` field list
  plus one text-format cell list, exactly what `VitrialSync.Db.Pgconn.query/3`
  returns inside its result map — producing a PLAIN MAP keyed by column-name
  atoms that carries **every column**. The codec boundary never loses data: if
  a migration adds or renames a column under one of these SELECTs, the map
  changes and the tests fail, instead of a silently mis-read field. Each
  table's expected column list is pinned in `@table_columns` below,
  transcribed from `app/models.py` and the Alembic migrations
  (`migrations/versions/0001`–`0005`), and every decode test runs its probe
  rows against that list — so the list itself is checked, not just trusted.

  `decode_result/1` is the entry point for a whole result map: it walks the
  rows through the matching table decoder's machinery (the same
  `VitrialSync.Db.Codec.decode_row/2` call) and returns the list of row maps.

  Fourteen tables, stated with their sources so the next reader does not recount:

    | table | cols | source |
    |---|---|---|
    | `organizations` | 4 | `app/models.py:20-25` |
    | `memberships` | 10 | `app/models.py:35-47` |
    | `canonical_customers` | 3 | `app/models.py:62-70` |
    | `canonical_projects` | 4 | `app/models.py:73-88` |
    | `canonical_project_sectors` | 5 | `app/models.py:91-111` |
    | `canonical_items` | 5 | `app/models.py:114-140` |
    | `canonical_project_children` | 5 | `app/models.py:143-159` |
    | `canonical_item_children` | 5 | `app/models.py:162-178` |
    | `sync_entities` | 8 | `app/models.py:181-190` |
    | `sync_mutations` | 14 | `app/models.py:193-208` |
    | `sync_change_log` | 8 | `app/models.py:212-221` |
    | `sync_mutation_fingerprints` | 3 | `app/idempotency.py:14-28` |
    | `evidence_blobs` | 10 | `app/models.py:224-235` |
    | `evidence_object_gc` | 10 | `migrations/versions/0005` |

  `users`, `auth_sessions`, `pairing_grants` and `reference_publications` are
  NOT here: they belong to `vitrial_auth`/`vitrial_reference`, whose waves own
  them (`beam/ARCHITECTURE.md` §Wave 5). `evidence_object_gc` is ENCODE-only in
  this slice — the engine writes GC rows but never reads them back (the
  collector is a later vertical).

  ## Projection: lossy by design, every drop written down

  `to_canonical_*` and `to_prior/1` build the engine's deliberately lossy
  structures. Each projection documents exactly what it drops and why; the drops
  are decisions from the reference, not omissions.

  Nothing here talks SQL. The write direction — `%Batch.Effects{}` to literal
  statements — is `VitrialSync.Db.Writes`.
  """

  alias VitrialSync.Canonical
  alias VitrialSync.Db.Codec

  # One RowDescription field, as Pgconn decodes it. Only `name` and `type_oid`
  # participate in decoding; the rest is carried because the wire carries it.
  @type field :: %{
          optional(atom()) => term(),
          name: String.t(),
          type_oid: pos_integer()
        }

  @type fields :: [field()]
  @type cells :: [binary() | nil]
  @type row_map :: %{optional(atom()) => term()}

  # The result map Pgconn.query/3 returns (a plain map — see the findings note
  # in the wave record: the client's @spec claims a %Rows{} struct it does not
  # build). This module reads only the two keys that exist.
  @type result :: %{
          optional(atom()) => term(),
          rows: [cells()],
          row_description: fields() | nil
        }

  # -- the pinned column lists (see moduledoc) ---------------------------------

  @table_columns %{
    organizations: ~w(id name authorization_revision created_at)a,
    memberships:
      ~w(id organization_id user_id active all_customers all_projects customer_ids project_ids roles capabilities)a,
    canonical_customers: ~w(organization_id customer_id deleted_at)a,
    canonical_projects: ~w(organization_id project_id customer_id deleted_at)a,
    canonical_project_sectors:
      ~w(organization_id project_sector_id project_id sector_id deleted_at)a,
    canonical_items: ~w(organization_id item_id project_id project_sector_id deleted_at)a,
    canonical_project_children: ~w(organization_id entity_type entity_id project_id deleted_at)a,
    canonical_item_children: ~w(organization_id entity_type entity_id item_id deleted_at)a,
    sync_entities:
      ~w(organization_id entity_type entity_id server_revision schema_version payload_json updated_at deleted_at)a,
    sync_mutations:
      ~w(id organization_id client_mutation_id submitted_by_user_id membership_id session_id authorization_revision device_id entity_type entity_id base_server_revision result_server_revision result_status received_at)a,
    sync_change_log:
      ~w(sequence organization_id entity_type entity_id server_revision client_mutation_id operation created_at)a,
    sync_mutation_fingerprints: ~w(organization_id client_mutation_id request_fingerprint)a,
    evidence_blobs:
      ~w(organization_id document_id item_id filename mime_type sha256 size_bytes storage_provider object_key created_at)a,
    evidence_object_gc:
      ~w(id organization_id document_id storage_provider object_key reason not_before attempts last_error created_at)a
  }

  @doc "The pinned column list for a table, for the drift tests."
  @spec table_columns(atom()) :: [atom()] | nil
  def table_columns(table), do: Map.get(@table_columns, table)

  @doc "The tables this module covers."
  @spec tables() :: [atom()]
  def tables, do: Map.keys(@table_columns)

  # -- full-row decoders -------------------------------------------------------
  #
  # Each returns Codec.decode_row/2's map unchanged — the functions exist so a
  # reader can find "how is sync_mutations decoded" and land on one name, and so
  # the drift tests can pin each table's SELECT without a catch-all.

  @doc "All of `organizations`, lossless."
  def organizations(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `memberships`, lossless."
  def memberships(fields, cells), do: Codec.decode_row(fields, cells)

  @doc """
  All of `auth_sessions`, lossless.

  Present because `Reads.authenticate/2` needs it and the table was the one
  `information_schema` knew about that had no decoder: every other table in this
  module predates the authentication reads, and a decoder is the whole of what a
  new table requires — `Codec.decode_row/2` already handles the shapes.
  """
  def auth_sessions(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `canonical_customers`, lossless."
  def canonical_customers(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `canonical_projects`, lossless."
  def canonical_projects(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `canonical_project_sectors`, lossless."
  def canonical_project_sectors(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `canonical_items`, lossless."
  def canonical_items(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `canonical_project_children`, lossless."
  def canonical_project_children(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `canonical_item_children`, lossless."
  def canonical_item_children(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `sync_entities`, lossless."
  def sync_entities(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `sync_mutations`, lossless (14 columns, `app/models.py:193-208`)."
  def sync_mutations(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `sync_change_log`, lossless."
  def sync_change_log(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `sync_mutation_fingerprints`, lossless (3 columns, composite PK)."
  def sync_mutation_fingerprints(fields, cells), do: Codec.decode_row(fields, cells)

  @doc "All of `evidence_blobs`, lossless."
  def evidence_blobs(fields, cells), do: Codec.decode_row(fields, cells)

  # -- whole-result decode -------------------------------------------------------

  @doc """
  Decode every row of one `VitrialSync.Db.Pgconn.query/3` result map to plain
  maps, through `VitrialSync.Db.Codec.decode_row/2`.

  Takes the `{:ok, result}` tuple or the bare map, so a caller can pipe either.
  The write path's result carries no `row_description` at all (the client omits
  the key on the `CommandComplete`-without-`RowDescription` shape); there are no
  rows to decode there either, so the field list defaults to empty rather than
  being invented.
  """
  @spec decode_result({:ok, result()} | result()) :: [row_map()]
  def decode_result({:ok, result}), do: decode_result(result)

  def decode_result(%{rows: rows} = result) when is_list(rows) do
    fields = Map.get(result, :row_description) || []
    Enum.map(rows, &Codec.decode_row(fields, &1))
  end

  # -- projections: the engine's lossy structs ----------------------------------

  @doc """
  `organizations` → `Canonical.Organization{authorization_revision}`.

  **Drops `id`, `name`, `created_at`.** The push path locks the org row
  `for update` and reads ONLY the revision (`app/sync_service.py:231-233`); the
  `World` omits the organization entirely by design (`Canonical`'s moduledoc).
  """
  @spec to_canonical_organization(row_map()) :: Canonical.Organization.t()
  def to_canonical_organization(%{authorization_revision: revision}) do
    %Canonical.Organization{authorization_revision: revision}
  end

  @doc """
  `memberships` → `Canonical.Membership`'s seven fields.

  **Drops `id`, `roles`, `capabilities`.** The grant path fetches the row by
  `principal.membership_id` (`app/ownership.py:991`) and widens only the id
  lists (`app/ownership.py:1000-1006`, `batch.ex:615-636`); roles and
  capabilities never enter a sync-path decision.
  """
  @spec to_canonical_membership(row_map()) :: Canonical.Membership.t()
  def to_canonical_membership(row) do
    %Canonical.Membership{
      organization_id: row.organization_id,
      user_id: row.user_id,
      active: row.active,
      all_customers: row.all_customers,
      all_projects: row.all_projects,
      customer_ids: row.customer_ids || [],
      project_ids: row.project_ids || []
    }
  end

  @doc """
  `canonical_customers` → `Canonical.Customer`.

  **Drops `organization_id`** — one `World` is one organization's state
  (`Canonical`'s moduledoc); every canonical projection below drops the same
  column for the same reason, and it is not restated per-function.
  """
  @spec to_canonical_customer(row_map()) :: Canonical.Customer.t()
  def to_canonical_customer(row) do
    %Canonical.Customer{customer_id: row.customer_id, deleted_at: row.deleted_at}
  end

  @doc "`canonical_projects` → `Canonical.Project`. Drops `organization_id`."
  @spec to_canonical_project(row_map()) :: Canonical.Project.t()
  def to_canonical_project(row) do
    %Canonical.Project{
      project_id: row.project_id,
      customer_id: row.customer_id,
      deleted_at: row.deleted_at
    }
  end

  @doc "`canonical_project_sectors` → `Canonical.ProjectSector`. Drops `organization_id`."
  @spec to_canonical_project_sector(row_map()) :: Canonical.ProjectSector.t()
  def to_canonical_project_sector(row) do
    %Canonical.ProjectSector{
      project_sector_id: row.project_sector_id,
      project_id: row.project_id,
      sector_id: row.sector_id,
      deleted_at: row.deleted_at
    }
  end

  @doc "`canonical_items` → `Canonical.Item`. Drops `organization_id`."
  @spec to_canonical_item(row_map()) :: Canonical.Item.t()
  def to_canonical_item(row) do
    %Canonical.Item{
      item_id: row.item_id,
      project_id: row.project_id,
      project_sector_id: row.project_sector_id,
      deleted_at: row.deleted_at
    }
  end

  @doc "`canonical_project_children` → `Canonical.ProjectChild`. Drops `organization_id`."
  @spec to_canonical_project_child(row_map()) :: Canonical.ProjectChild.t()
  def to_canonical_project_child(row) do
    %Canonical.ProjectChild{
      entity_type: row.entity_type,
      entity_id: row.entity_id,
      project_id: row.project_id,
      deleted_at: row.deleted_at
    }
  end

  @doc "`canonical_item_children` → `Canonical.ItemChild`. Drops `organization_id`."
  @spec to_canonical_item_child(row_map()) :: Canonical.ItemChild.t()
  def to_canonical_item_child(row) do
    %Canonical.ItemChild{
      entity_type: row.entity_type,
      entity_id: row.entity_id,
      item_id: row.item_id,
      deleted_at: row.deleted_at
    }
  end

  @doc """
  `sync_entities` → `Canonical.Entity`'s five fields.

  **Drops `organization_id`, `schema_version`, `updated_at`**
  (`Canonical.Entity`'s field list, `canonical.ex:179-199`): the first by the
  one-organization rule, the other two because no admission decision reads them
  — visibility and staleness key on `server_revision` and `deleted_at`.
  `payload_json` decodes through `VitrialSync.JSON` into `payload`.
  """
  @spec to_canonical_entity(row_map()) :: Canonical.Entity.t()
  def to_canonical_entity(row) do
    %Canonical.Entity{
      entity_type: row.entity_type,
      entity_id: row.entity_id,
      payload: row.payload_json,
      deleted_at: row.deleted_at,
      server_revision: row.server_revision
    }
  end

  @doc """
  `evidence_blobs` → `Canonical.EvidenceBlob`.

  **Drops `organization_id`, `item_id`, `filename`, `mime_type`, `sha256`,
  `size_bytes`, `created_at`.** These three fields plus the caller's organization
  are the whole of `queue_blob_gc`'s input (`app/evidence_gc.py:56-70`); nothing
  else on the sync path reads a blob row.
  """
  @spec to_canonical_evidence_blob(row_map()) :: Canonical.EvidenceBlob.t()
  def to_canonical_evidence_blob(row) do
    %Canonical.EvidenceBlob{
      document_id: row.document_id,
      storage_provider: row.storage_provider,
      object_key: row.object_key
    }
  end

  @doc """
  `sync_mutations` → exactly `Batch.Store`'s five-field prior map
  (`batch.ex:474-493`): `entity_type`, `entity_id`, `base_server_revision`,
  `result_status`, `result_server_revision`.

  **Drops the other nine columns.** A prior is the replay/idempotency answer for
  one `clientMutationID`; the audit envelope (who, when, which session) lives in
  the row and never re-enters a decision. The map is keyed exactly as
  `Batch.put_priors/4` builds it in-memory, so a loaded prior and a threaded one
  are indistinguishable downstream.
  """
  @spec to_prior(row_map()) :: %{
          required(:entity_type) => String.t(),
          required(:entity_id) => String.t(),
          required(:base_server_revision) => non_neg_integer() | nil,
          required(:result_status) => String.t(),
          required(:result_server_revision) => non_neg_integer() | nil
        }
  def to_prior(row) do
    %{
      entity_type: row.entity_type,
      entity_id: row.entity_id,
      base_server_revision: row.base_server_revision,
      result_status: row.result_status,
      result_server_revision: row.result_server_revision
    }
  end

  @doc """
  The column-name atoms a table's full-row decode produces, as a map for the
  drift tests: `column_set(:sync_mutations)` → `MapSet` of its 14 names.
  """
  @spec column_set(atom()) :: MapSet.t(atom()) | nil
  def column_set(table) do
    case table_columns(table) do
      nil -> nil
      columns -> MapSet.new(columns)
    end
  end
end
