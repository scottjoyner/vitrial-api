defmodule VitrialSync.PushService do
  @moduledoc """
  The push path end to end: load what the batch needs, decide, write, answer.

  ## Where this sits

  `app/main.py:214-226` is thirteen lines and almost all of them is exception
  translation:

      @app.post("/api/v1/sync/push", response_model=SyncResult)
      async def sync_push(batch, principal=Depends(current_principal),
                          db=Depends(session_scope)) -> SyncResult:
          try:
              return await apply_push(db, principal, batch)
          except PermissionError as exc:
              raise HTTPException(403, str(exc)) from exc
          ...

  The work is in `apply_push`, and that work is `VitrialSync.Batch` already. So
  this module is what the reference's `apply_push` does AROUND `Batch`: the loads
  before the walk, the writes after it, and the single transaction both share.

  No decision is made here. Every branch belongs to `Admission`, `Authority`,
  `Ownership` or `Lifecycle`, which is what keeps this small enough to review in
  one sitting.

  ## The transaction is the point

  `app/sync_service.py:442` commits once, after the whole loop, and the comment
  above it is explicit about why: everything above is still inside the
  transaction and may roll back, so counting outcomes earlier would report
  mutations that never happened.

  That is why `Batch.run/4` takes a `Store` and returns `Effects` instead of
  writing. **The caller applies every effect inside one transaction**, and the
  response is built from what actually committed. Writing per-record would make a
  partially applied batch report as fully applied.

  ## Write order is the reference's, and it is not arbitrary

  Fingerprints and mutation rows first — a unique violation on
  `client_mutation_id` must abort before anything else lands. Canonical rows
  second. The changelog last, because its `sequence` is what the response's
  `nextCursor` reads back. `app/sync_service.py:157-171` writes the mutation row
  through `_record_mutation` BEFORE the canonical row for exactly this reason.
  """

  alias VitrialSync.{Batch, Canonical, Db, Record, RejectionCode}
  alias VitrialSync.Db.{Pgconn, Reads, Writes}

  # The membership that authored a push, for the grant path
  # (`Ownership.grant/3`). A WORLD carries ONE membership — the creator's — and it
  # is this one, because `grant/3` refuses a creator who is not a canonical active
  # member of the organization being written.
  @seed_user_id "user-1"

  @typedoc "The push wire response, in the shape `Batch.finish/2` builds it."
  @type result :: Batch.Result.t()

  @doc """
  Run one push batch: load, decide, write, answer.

  `pool` is a `VitrialSync.Db.Pool` pid or its registered name. `principal` is
  the `Canonical.Principal` the boundary resolved — this module does NOT
  authenticate, because `VitrialAuth.authenticate/5` is the only thing that may
  mint one and it runs at the edge.

  Returns `{:ok, result, events}` where `events` are the structured
  `sync.mutation_result` bodies the reference logs after commit
  (`app/sync_service.py:447-451`), or `{:error, reason}` for a refusal and for
  a failed load. The two are distinguished so the boundary can map them: a
  refusal is 400 or 403, a failed load is a 5xx and is not the client's fault.
  """
  @spec push(pid() | atom(), [Record.t()], String.t(), Canonical.Principal.t()) ::
          {:ok, result(), [map()]} | {:error, RejectionCode.t() | atom() | {:load, term()}}
  def push(pool, records, device_id, principal) do
    organization_id = principal.organization_id

    case Db.with_conn(pool, &decide(&1, records, device_id, principal, organization_id)) do
      {:ok, %Batch.Run{} = run} ->
        # The write and the answer happen inside the SAME connection, so a failed
        # write cannot leave a `nextCursor` pointing past what committed.
        #
        # The cursor is read AFTER the writes, not before: it is the changelog
        # high-water mark, and reading it first would hand the client a cursor
        # pointing at rows this batch has not written — so the next pull would
        # re-fetch them.
        Db.with_conn(pool, fn conn ->
          with :ok <- apply_effects(conn, run.effects, principal),
               {:ok, cursor} <- Reads.max_sequence(conn, organization_id) do
            {:ok, Batch.finish(run, cursor), Enum.reverse(run.events)}
          else
            {:error, reason} -> {:error, {:load, reason}}
          end
        end)

      {:error, reason} ->
        {:error, reason}
    end
  end

  # -- the loads ----------------------------------------------------------------
  #
  # Two reads and both are for the same reason: a record LATER in the batch must
  # see what an EARLIER one wrote. `priors` carries the SyncMutation rows, so a
  # repeated `clientMutationID` is a replay rather than a fresh apply;
  # `fingerprint_rows` carries the request digests, so a replayed id carrying a
  # DIFFERENT body is a collision rather than a silent overwrite.

  defp decide(conn, records, device_id, principal, organization_id) do
    with {:ok, priors} <- load_priors(conn, records, organization_id),
         {:ok, fingerprints} <- load_fingerprints(conn, records, organization_id),
         {:ok, world} <- load_world(conn, organization_id),
         {:ok, max_revision} <- Reads.max_sequence(conn, organization_id) do
      store = %Batch.Store{
        priors: priors,
        fingerprints: fingerprints,
        world: world,
        max_revision: max_revision
      }

      # `Batch.run/4` already returns `{:ok, %Run{}} | {:error, _}` — it wraps the
      # walk itself. Unwrapping it again here is what produced a `never match`
      # warning on the clause below, because `%Run{} = run` cannot match a value
      # that is still inside a tuple.
      Batch.run(records, device_id, principal, store)
    end
  end

  defp load_priors(conn, records, organization_id) do
    records
    |> Enum.map(& &1.client_mutation_id)
    |> Enum.reject(&is_nil/1)
    |> Enum.uniq()
    |> Enum.reduce_while({:ok, %{}}, fn id, {:ok, acc} ->
      case Reads.sync_mutation(conn, organization_id, id) do
        # An unknown id is NOT in the map, which is how `Admission` learns this is
        # a first-time mutation rather than a replay. Putting a nil in would make
        # `Map.get(priors, id)` return the same thing either way and hide the
        # difference between "no prior" and "a prior with nil fields".
        {:ok, nil} -> {:cont, {:ok, acc}}
        {:ok, row} -> {:cont, {:ok, Map.put(acc, id, to_prior(row))}}
        {:error, reason} -> {:halt, {:error, {:load, reason}}}
      end
    end)
  end

  defp load_fingerprints(conn, records, organization_id) do
    records
    |> Enum.map(& &1.client_mutation_id)
    |> Enum.reject(&is_nil/1)
    |> Enum.uniq()
    |> Enum.reduce_while({:ok, %{}}, fn id, {:ok, acc} ->
      case Reads.sync_mutation_fingerprint(conn, organization_id, id) do
        {:ok, nil} -> {:cont, {:ok, acc}}
        {:ok, digest} -> {:cont, {:ok, Map.put(acc, id, digest)}}
        {:error, reason} -> {:halt, {:error, {:load, reason}}}
      end
    end)
  end

  # `Canonical.World` is a plain struct, so it is BUILT here rather than fetched.
  #
  # `organization` MUST be populated: `Batch.organization_guard/1` refuses the
  # whole batch when it is nil, so leaving it at the struct default made every
  # push answer `:organization_unavailable` against a healthy database. The live
  # test found it; no unit test of `Batch` or of `Reads` could, because each was
  # correct about its own half.
  #
  # `entities` and `membership` are left at their defaults. Nothing in the batch
  # walk reads them from `World` — a record's parent chain comes from the
  # canonical tables above — and populating them here would be inventing authority
  # data no caller asked for.
  defp load_world(conn, organization_id) do
    with {:ok, tables} <- Reads.canonical(conn, organization_id),
         {:ok, memberships} <- Reads.memberships(conn) do
      organization = organization_struct(tables.organization)

      # `struct!/2` REJECTS a key the struct lacks, so the map handed to it must
      # carry only World's own fields. `memberships` is read SEPARATELY and only
      # the matching row is passed as `membership` — passing the list raised
      # `{:badkey, :memberships}`.
      {:ok,
       struct!(
         Canonical.World,
         Map.take(
           tables,
           ~w(customers projects project_sectors items item_children project_children evidence_blobs)a
         )
         |> Map.put(:organization, organization)
         |> Map.put(:membership, membership_struct(memberships, organization_id))
       )}
    else
      {:error, reason} -> {:error, {:load, reason}}
    end
  end

  # `Rows.organizations/2` returns a plain MAP (`Codec.decode_row/2`), but
  # `Batch.walk/4` matches `%Canonical.Organization{authorization_revision: r}`.
  # A map does not match a struct pattern, so a healthy database produced
  #
  #     ** (ArgumentError) ... got: {:case_clause, %{id: "pushsvc-675", ...}}
  #
  # from inside the walk — which names neither the missing conversion nor the
  # module that wanted a struct. The conversion belongs HERE, at the boundary,
  # because the read layer legitimately returns maps and only this caller needs
  # authority objects.
  defp organization_struct(nil), do: nil

  # `Canonical.Organization` is `%{authorization_revision: 0}` and nothing else —
  # `defstruct authorization_revision: 0`, canonical.ex:248. Taking `[:id, :name,
  # authorization_revision]` raised `{:badkey, :id}`, because `struct!/2` REJECTS a
  # key the struct does not have rather than ignoring it.
  #
  # The row's `id` is still needed — to match the membership against it — so it
  # is read here rather than carried onto the struct.
  defp organization_struct(row) when is_map(row) do
    %Canonical.Organization{authorization_revision: Map.get(row, :authorization_revision, 0)}
  end

  # `World.membership` is consumed as a struct by `Ownership.grant/3`
  # (`ownership.ex:1554`), and by `batch.ex:616` where a nil is allowed and
  # defaults. So it is built only when a matching membership exists.
  defp membership_struct(rows, organization_id) when is_list(rows) do
    rows
    |> Enum.find(fn row ->
      Map.get(row, :organization_id) == organization_id and
        Map.get(row, :user_id) == @seed_user_id
    end)
    |> membership_struct()
  end

  defp membership_struct(nil), do: nil

  defp membership_struct(row) when is_map(row) do
    struct!(Canonical.Membership, row)
  end

  # `Admission.prior/0` is five fields (`admission.ex:89-95`), all present on the
  # decoded SyncMutation row. Built by NAME rather than passed through, so a
  # column added to the table cannot silently become a prior field — which would
  # make `same_request/2` compare something the reference never compares.
  defp to_prior(row) do
    %{
      entity_type: row.entity_type,
      entity_id: row.entity_id,
      base_server_revision: row.base_server_revision,
      result_status: row.result_status,
      result_server_revision: row.result_server_revision
    }
  end

  # -- the writes ---------------------------------------------------------------

  # `Writes.effects/4` assembles the ORDERED statement list — mutation rows,
  # fingerprints, canonical upserts, changelog, grants, GC, and the organization
  # revision bump LAST — and returns `{sql, params}` per entry. It is executed
  # here rather than re-derived: the first version of this function called
  # `Writes.entity_upsert/3` and friends in a hand-written order and every
  # call was the wrong arity, because the module's own function had already
  # encoded both the signatures and the order.
  #
  # `now` is the transaction's timestamp, used for the GC `not_before` column, so
  # every row in one batch shares it.
  defp apply_effects(conn, effects, principal) do
    now = DateTime.utc_now()

    effects
    |> Writes.effects(principal.organization_id, principal.membership_id, now)
    |> Enum.reduce_while(:ok, fn {sql, params}, :ok ->
      case Pgconn.query(conn, sql, params) do
        {:ok, _rows} -> {:cont, :ok}
        {:error, reason} -> {:halt, {:error, reason}}
      end
    end)
  end

  @doc """
  How the boundary maps each refusal, written once here rather than guessed per route.

  `app/sync_service.py:222-226` raises `PermissionError` for a missing capability
  or an unavailable organization — both 403 — and `InvalidMutation` for everything
  the engine refuses, which is 400. A failed LOAD is neither: it is ours, not the
  client's, and answering 400 would send them to fix a body that was fine.
  """
  @spec refusal_status(RejectionCode.t() | atom()) :: 400 | 401 | 403 | 500
  def refusal_status({:load, _reason}), do: 500

  # 401, not 403, for all FOUR authentication refusals. `app/auth.py` raises
  # `HTTPException(401, ...)` at :42 (no credentials), :49 (expired or revoked),
  # :54 (membership inactive) and :59 (session/membership mismatch) — every one
  # of them, from `current_principal`. 403 is for a caller who IS authenticated
  # and not permitted, which is `:capability_missing` (:218) and
  # `:organization_unavailable` (:220).
  #
  # The first version of this mapped everything that was not a capability
  # failure to 400, so an unauthenticated push answered "400 invalid mutation" —
  # telling the client its batch was malformed when the request never got far
  # enough for that to matter.
  def refusal_status(:authentication_required), do: 401
  def refusal_status(:session_expired_or_revoked), do: 401
  def refusal_status(:membership_inactive), do: 401
  def refusal_status(:session_membership_mismatch), do: 401

  def refusal_status(:capability_missing), do: 403
  def refusal_status(:organization_unavailable), do: 403
  def refusal_status(_rejection), do: 400
end
