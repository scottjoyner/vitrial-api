defmodule VitrialSync.Fingerprint do
  @moduledoc """
  Request identity for a `clientMutationID`, byte-identical to the Python service.

  A `clientMutationID` is an organization's claim that "these bytes are one
  mutation". When the same id arrives again the server has to answer one of two
  very different questions, and the only thing that tells them apart is whether
  the bytes behind the id are the same:

    * **same bytes** -- a legitimate replay. Answer with the original result.
    * **different bytes** -- a collision, or a client that reused an id across two
      distinct intents. Refuse.

  Getting that wrong in the permissive direction applies a mutation twice; getting
  it wrong in the strict direction silently discards a real client change. So the
  comparison is a SHA-256 over a canonical encoding rather than a spot-check of
  whichever fields happen to be cheap.

  ## Parity is the whole requirement

  `app/idempotency.py` on the Python service already produces these bytes. This
  module has to produce the same digest for the same mutation, or the two estates
  disagree about whether a replay is legitimate -- and the disagreement is
  invisible until a client that pushed through both is rejected by one of them.

  The encoded fields are therefore fixed, and fixed *deliberately* rather than by
  reflection over the record struct:

    * `entityType`, `entityID` -- the mutation's target
    * `baseServerRevision` -- the revision the client believed it was building on;
      changing it is a different intent even when the payload is identical, which
      is exactly the case where a naive payload-only fingerprint would wrongly
      call a rebase a replay
    * `updatedAt`, `deletedAt` -- timestamps, normalised through
      `VitrialSync.Timestamp`
    * `payload` -- base64, because the payload is opaque bytes and a canonical
      encoding of arbitrary binary is not a thing JSON does

  `serverRevision` and `id` are deliberately absent. Both are assigned by the
  server, so including them would make the fingerprint of a mutation depend on
  what the server last said about it rather than on what the client asked for.
  """

  alias VitrialSync.{CanonicalJSON, Timestamp}

  @type mutation :: %{
          required(:entity_type) => String.t(),
          required(:entity_id) => String.t(),
          required(:base_server_revision) => non_neg_integer() | nil,
          required(:updated_at) => String.t(),
          required(:deleted_at) => String.t() | nil,
          required(:payload) => binary()
        }

  @doc """
  The canonical bytes this module hashes, exposed so a divergence from the Python
  implementation can be diagnosed by comparing bytes rather than digests.

  A mismatched digest says only *that* the two disagree; a mismatched encoding
  says exactly where.
  """
  @spec canonical_bytes(mutation()) :: binary()
  def canonical_bytes(%{
        entity_type: entity_type,
        entity_id: entity_id,
        base_server_revision: base_server_revision,
        updated_at: updated_at,
        deleted_at: deleted_at,
        payload: payload
      }) do
    CanonicalJSON.encode(%{
      "baseServerRevision" => base_server_revision,
      "deletedAt" => deleted_at && Timestamp.normalize(deleted_at),
      "entityID" => entity_id,
      "entityType" => entity_type,
      "payload" => Base.encode64(payload),
      "updatedAt" => Timestamp.normalize(updated_at)
    })
  end

  @doc """
  The lowercase hex SHA-256 of the canonical encoding of `mutation`.

  This is the value stored in `sync_mutation_fingerprints.request_fingerprint`.
  """
  @spec of(mutation()) :: String.t()
  def of(mutation), do: CanonicalJSON.sha256_hex(canonical_bytes(mutation))

  @doc """
  Whether two mutations are the same mutation.

  Kept as a named predicate rather than left to callers comparing digests with
  `==`, because "same bytes" and "same digest" are not the same claim -- this is
  where that confusion gets to live, in one place with a test, rather than at
  every call site.
  """
  @spec same_mutation?(mutation(), mutation()) :: boolean()
  def same_mutation?(left, right), do: of(left) == of(right)
end