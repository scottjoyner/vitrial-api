defmodule VitrialSync.Admission do
  @moduledoc """
  The per-record push decision, in the order the server makes it.

  `_apply_push_once` (`app/sync_service.py:227-476`) runs nine steps per record
  and the first match wins. This module owns the first four, which need nothing
  but the record and three facts the storage layer can supply:

  | # | step | contract |
  |---|---|---|
  | 1 | missing idempotency key | `missing_client_mutation_id` |
  | 2 | prior mutation with this `clientMutationID` | S-55 |
  | 3 | optimistic concurrency | S-56 |
  | 4 | payload decode | S-57 |

  Steps 5-8 (item-delete guard, lifecycle, authority, evidence GC) all need the
  canonical world, and they live in `VitrialSync.Authority` and the modules it
  dispatches to. `decide/3` runs the whole sequence, so a caller with a world can
  ask for the per-record decision in one call and get the same rejection code the
  reference would have emitted.

  ## Why the decisions are pulled out of the loop

  Three reasons, in order of weight:

    * **The order is the contract.** Nine ordered steps in one 250-line function
      is a function nobody can reorder safely. As a pure function of a record and
      a context map, the ordering is the code.
    * **Step 2 writes nothing; step 3 spends the id.** Which steps write a
      `SyncMutation` row is not incidental -- it determines whether the client's
      `clientMutationID` is still usable. `writes_mutation_row?/1` makes that
      queryable instead of something you have to read the loop to learn.
    * **It is testable without PostgreSQL.** Every branch here is reachable from
      a plain map, so the branch matrix is a unit test rather than a fixture.

  ## The three-way replay reason

  Step 2 distinguishes three outcomes, not two, and the third one exists because
  reporting `idempotent_replay` for a rejected mutation is a false statement:

    * `mutation_id_collision` -- the id was reused for different bytes. Refuse.
    * `idempotent_replay` -- the same mutation, previously **accepted**. The
      response is the original answer.
    * `rejected_mutation_id_reuse` -- the same mutation, previously **rejected**.
      Nothing replayed, and the id is spent (S-55a).

  An operator reading `idempotent_replay` in a `sync.mutation_result` event when
  a client insists it never got a rejection has been told something false. That
  is the entire reason the third reason exists.

  ## What a rejection at step 5 or later still does

  It still **spends** the `clientMutationID`, exactly as a stale revision does.
  The reference writes a `SyncMutation` row for every rejection from step 3
  onwards (`app/sync_service.py:390-393`), and `Outcome.writes_mutation_row?`
  reports that for the later steps too. That is S-55a: a client must mint a new
  id to retry anything that was rejected, not just a stale revision.
  """

  alias VitrialSync.{Authority, Canonical, Fingerprint, Payload, Record}

  defmodule Outcome do
    @moduledoc """
    What the push engine should do with one record, and what to tell the world.

    `status` and `reason` are the wire values. They are strings rather than atoms
    because they are compared against the Python estate's output during
    dual-run, and an atom would have to be rendered somewhere -- better here,
    once, than in a log formatter.
    """

    @type t :: %__MODULE__{
            status: :accepted | :rejected,
            reason: String.t(),
            result_revision: non_neg_integer() | nil,
            writes_mutation_row?: boolean(),
            payload: VitrialSync.Payload.t() | nil
          }

    defstruct [:status, :reason, :result_revision, :writes_mutation_row?, :payload]
  end

  @typedoc """
  The stored `SyncMutation` row for this `clientMutationID`, if one exists.

  `result_status` is a string because it is read from the database, where it was
  written by whichever estate handled the original attempt.
  """
  @type prior :: %{
          required(:entity_type) => String.t(),
          required(:entity_id) => String.t(),
          required(:base_server_revision) => non_neg_integer() | nil,
          required(:result_status) => String.t(),
          required(:result_server_revision) => non_neg_integer() | nil
        }

  @typedoc """
  The facts about the record that the storage layer looked up on this record's
  behalf.

  `entity_exists` is separate from `current_server_revision` rather than encoded
  as `nil` meaning "absent", because conflating them is how a real row with a
  `NULL` revision would be silently treated as a create.
  """
  @type context :: %{
          required(:prior) => prior() | nil,
          required(:prior_fingerprint) => String.t() | nil,
          required(:entity_exists) => boolean(),
          required(:current_server_revision) => non_neg_integer() | nil
        }

  @empty_context %{
    prior: nil,
    prior_fingerprint: nil,
    entity_exists: false,
    current_server_revision: nil
  }

  @doc "A context for a record with no prior mutation and no stored entity."
  @spec empty_context() :: context()
  def empty_context, do: @empty_context

  @doc """
  Decide one record.

      iex> alias VitrialSync.{Admission, Record}
      iex> record = %Record{id: "r1", entity_type: "customer", entity_id: "c1",
      ...>   updated_at: "2026-10-02T00:00:00Z", payload: ~s({}), client_mutation_id: "m1",
      ...>   base_server_revision: 0}
      iex> Admission.decide(record, Admission.empty_context()).reason
      "stale_revision"
  """
  @spec decide(Record.t(), context()) :: Outcome.t()
  def decide(%Record{} = record, context) do
    case step_missing_id(record) do
      nil -> step_prior_mutation(record, context)
      outcome -> outcome
    end
  end

  # Step 1. Falsy is nil or "", because that is what Pydantic produces for both an
  # omitted field and an empty string, and the reference implementation branches
  # on `not record.clientMutationID` rather than on `is None`.
  #
  # `writes_mutation_row?: false`, and deliberately NOT via the shared `reject/2`
  # helper: the reference `continue`s here (sync_service.py:243-249) BEFORE
  # `_record_mutation` is ever reached, so no SyncMutation row is written for a
  # record with no clientMutationID. Reporting `true` here would hand the caller
  # a row carrying nil/"" into `SyncMutation.client_mutation_id`, which is
  # `Mapped[str]` NOT NULL under a unique constraint (app/models.py:197,209).
  defp step_missing_id(%Record{client_mutation_id: id}) when id in [nil, ""],
    do: %Outcome{
      status: :rejected,
      reason: "missing_client_mutation_id",
      result_revision: nil,
      writes_mutation_row?: false,
      payload: nil
    }

  defp step_missing_id(%Record{}), do: nil

  # Step 2. A prior row for this id: replay, collision, or spent id.
  defp step_prior_mutation(%Record{} = record, %{prior: nil} = context),
    do: step_revision_guard(record, context)

  defp step_prior_mutation(%Record{} = record, %{prior: prior} = context) when is_map(prior) do
    same_request = same_request?(record, prior, context)
    accepted_replay = same_request and prior.result_status == "accepted"

    %Outcome{
      status: if(accepted_replay, do: :accepted, else: :rejected),
      reason: replay_reason(same_request, accepted_replay),
      result_revision: prior.result_server_revision,
      # Step 2 never writes a second row. The existing row IS the record of what
      # happened, and overwriting it would erase the original outcome.
      writes_mutation_row?: false,
      payload: nil
    }
  end

  # Step 3. Optimistic concurrency.
  #
  # Two cases, and both are rejections. A create must omit `baseServerRevision`
  # entirely -- sending one for a row that does not exist is a client that
  # believes it is updating something the server has never heard of. An update
  # must match the stored revision exactly, so a null base against an existing
  # row is stale too.
  defp step_revision_guard(%Record{} = record, context) do
    if stale_revision?(record, context) do
      reject("stale_revision", context.current_server_revision)
    else
      step_payload_decode(record, context)
    end
  end

  # Step 4. Payload decode.
  defp step_payload_decode(%Record{payload: raw}, context) do
    case Payload.decode(raw) do
      {:ok, payload} ->
        %Outcome{
          status: :accepted,
          # "committed" rather than something like "admitted": the reason is the
          # token the `sync.mutation_result` event carries, and S-71 pins that
          # token for the accepted case. Steps 5-8 can still reject after this
          # point, and the engine is what decides whether the commit survives.
          reason: "committed",
          result_revision: nil,
          writes_mutation_row?: true,
          payload: payload
        }

      {:error, _reason} ->
        # S-71: the reason is a token from the shared rejection vocabulary in
        # `app/rejection_codes.py`. It used to be the Python exception class name,
        # which meant a refactor that renamed a class silently renamed a token
        # that clients and metrics key on. `InvalidMutation` maps to
        # `invalid_mutation`, not to the class name.
        reject("invalid_mutation", context.current_server_revision)
    end
  end

  @doc """
  Whether the record's `baseServerRevision` disagrees with stored state.
  """
  @spec stale_revision?(Record.t(), context()) :: boolean()
  def stale_revision?(%Record{base_server_revision: base}, %{
        entity_exists: true,
        current_server_revision: current
      }),
      do: base !== current

  def stale_revision?(%Record{base_server_revision: base}, %{entity_exists: false}),
    do: base !== nil

  @doc """
  Whether this `clientMutationID` names the same request as the prior row.

  Prefers the stored fingerprint, and falls back to a weaker identity when no
  fingerprint row exists -- legacy rows, or one lost to a partial migration. The
  fallback ignores `updatedAt`, `deletedAt` and payload bytes, so it calls two
  genuinely different mutations the same. That is the reference behaviour and it
  is preserved deliberately: a row with no fingerprint is a row from before
  fingerprinting existed, and treating it as a collision would reject legitimate
  replays of every mutation ever recorded.
  """
  @spec same_request?(Record.t(), prior(), context()) :: boolean()
  def same_request?(%Record{} = record, _prior, %{prior_fingerprint: fingerprint})
      when is_binary(fingerprint),
      do: Fingerprint.of(Record.mutation(record)) == fingerprint

  def same_request?(%Record{} = record, prior, %{prior_fingerprint: nil}) do
    record.entity_type == prior.entity_type and record.entity_id == prior.entity_id and
      record.base_server_revision == prior.base_server_revision
  end

  @doc """
  The `reason` token for a record that hit a prior `SyncMutation` row.

  Split out because the three-way branch is the part of this module most likely
  to be "simplified" back into two arms by a later reader who finds the third
  one redundant.
  """
  @spec replay_reason(boolean(), boolean()) :: String.t()
  def replay_reason(same_request, accepted_replay) do
    cond do
      not same_request -> "mutation_id_collision"
      accepted_replay -> "idempotent_replay"
      true -> "rejected_mutation_id_reuse"
    end
  end

  defp reject(reason, result_revision) do
    %Outcome{
      status: :rejected,
      reason: reason,
      result_revision: result_revision,
      writes_mutation_row?: true,
      payload: nil
    }
  end

  @doc """
  Steps 1-8: the whole per-record decision, with a canonical world in hand.

  Steps 1-4 run first and unchanged, so their rejection reasons and their
  `writes_mutation_row?` are the same as `decide/2` produces. If they admit the
  record, steps 5-8 run in `VitrialSync.Authority` and either admit it or refuse
  with one of the three authority/lifecycle codes.

  `effects` carries what the push engine still has to do inside the transaction:
  the canonical row upserts, the scope grant a self-created parent produces, and
  the evidence GC row a tombstone enqueues. It is `nil` on a rejection, because
  the reference's `except` clause rolls the whole `try` block back. On an accept
  it also carries `:payload` -- the decoded object, set HERE and not in
  `VitrialSync.Authority`, so `VitrialSync.Batch` can build the canonical
  upsert effect without a second decode.

  `scope` (decide/6) is the batch's WORKING scope: `VitrialSync.Batch` derives
  it from the principal once at entry, widens it after every accepted
  customer/project create (`app/ownership.py:865`/`:884`), and feeds it here for
  every subsequent record -- that threading is what lets one batch create a
  parent and then a child under it. decide/5 derives it from the principal
  (`Canonical.Scope.from_principal/1`), which is the entry scope: the right
  shape for a single-record caller, and what a batch would wrongly freeze every
  record at.

      iex> alias VitrialSync.{Admission, Canonical, Record}
      iex> record = %Record{id: "r1", entity_type: "customer", entity_id: "c1",
      ...>   updated_at: "2026-10-02T00:00:00Z", payload: ~s({"id": "c1"}), client_mutation_id: "m1"}
      iex> principal = %Canonical.Principal{organization_id: "org", user_id: "u1",
      ...>   membership_id: "m1", session_id: "s1", authorization_revision: 1,
      ...>   capabilities: MapSet.new(["customer.create"])}
      iex> world = %Canonical.World{
      ...>   membership: %Canonical.Membership{active: true, organization_id: "org", user_id: "u1"},
      ...>   organization: %Canonical.Organization{authorization_revision: 1}}
      iex> {:ok, effects} = Admission.decide(record, Admission.empty_context(), nil, principal, world); :ok
      :ok
      iex> effects.authority
      :generic
      iex> effects.payload
      %{"id" => "c1"}
      iex> effects.ownership.scope_grant
      %{customer_ids: ["c1"], project_ids: []}
      iex> effects.ownership.upserts
      [{:customer, "c1", nil}]

      iex> alias VitrialSync.{Admission, Canonical, Record}
      iex> record = %Record{id: "r2", entity_type: "customer", entity_id: "c1",
      ...>   updated_at: "2026-10-02T00:00:00Z", payload: ~s({}), client_mutation_id: "m2",
      ...>   deleted_at: "2026-10-02T00:00:00Z"}
      iex> principal = %Canonical.Principal{organization_id: "org",
      ...>   capabilities: MapSet.new(["customer.edit"])}
      iex> Admission.decide(record, Admission.empty_context(), nil, principal, Canonical.empty_world())
      {:rejected, "authorization_rejected"}
  """
  @spec decide(
          Record.t(),
          context(),
          VitrialSync.Canonical.Entity.t() | nil,
          VitrialSync.Canonical.Principal.t(),
          VitrialSync.Canonical.World.t()
        ) ::
          {:ok, VitrialSync.Authority.effects()}
          | {:rejected, String.t()}
          | %Outcome{}
  def decide(%Record{} = record, context, current, principal, world) do
    decide(record, context, current, principal, Canonical.Scope.from_principal(principal), world)
  end

  @spec decide(
          Record.t(),
          context(),
          VitrialSync.Canonical.Entity.t() | nil,
          VitrialSync.Canonical.Principal.t(),
          VitrialSync.Canonical.Scope.t(),
          VitrialSync.Canonical.World.t()
        ) ::
          {:ok, VitrialSync.Authority.effects()}
          | {:rejected, String.t()}
          | %Outcome{}
  def decide(%Record{} = record, context, current, principal, scope, world) do
    case decide(record, context) do
      %Outcome{status: :rejected} = outcome ->
        # Steps 1-4 keep their own return shape: the outcome carries
        # `result_revision` and `writes_mutation_row?`, and a caller already
        # written against `decide/2` reads those.
        outcome

      # Step 2's accepted replay also stops here, and the distinguishing fact is
      # `payload: nil`. The reference `continue`s for all three step-2 reasons
      # (sync_service.py:292) before it ever reaches the authority steps, so an
      # accepted replay must NOT be handed to Authority with a nil payload --
      # doing so turns `idempotent_replay` into `authorization_rejected`, which
      # is a false statement to the client: the mutation WAS applied, and this
      # response is the original answer, not a fresh rejection.
      #
      # Matched on the step, not on `status`, because an accepted outcome from
      # step 4 carries a real decoded payload and must go on to Authority.
      %Outcome{status: :accepted, payload: nil} = outcome ->
        outcome

      %Outcome{status: :accepted, payload: payload} ->
        case Authority.decide(record, payload, current, principal, scope, world) do
          {:ok, effects} -> {:ok, Map.put(effects, :payload, payload)}
          {:rejected, _code} = rejected -> rejected
        end
    end
  end
end
