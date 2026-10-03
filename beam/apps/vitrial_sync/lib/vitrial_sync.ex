defmodule VitrialSync do
  @moduledoc """
  The sync engine: the only component allowed to decide what a mutation means.

  Everything here is pure computation over validated input. There is no process,
  no GenServer, and no state, because none of it needs any -- which is also why
  this app supervises no children (see `VitrialSync.Application`).

  ## What is implemented

  The transport-adjacent primitives:

    * `VitrialSync.Cursor` -- pull cursors, bounded to the `BIGINT` range that
      `SyncChangeLog.sequence` actually occupies
    * `VitrialSync.Fingerprint` and `VitrialSync.CanonicalJSON` -- request identity
      for a `clientMutationID`, byte-identical to `app/idempotency.py` on the
      Python service
    * `VitrialSync.Timestamp` -- the ISO-8601 rendering both sides agree on
    * `VitrialSync.Record` and `VitrialSync.Payload` -- the inbound record and
      its base64-or-raw JSON decode
    * `VitrialSync.Page` and `VitrialSync.EntityOrder` -- page assembly and the
      deterministic order rows leave a batch in
    * `VitrialSync.Prefetch` -- the two pull-side visibility resolvers, which are
      batched twins of `record_is_visible` and `delivery_execution_is_visible`
      and must not drift from them
    * `VitrialSync.Retry` -- which failures are worth another attempt

  The admission pipeline, in the order `app/sync_service.py:318-383` makes its
  decisions:

    * `VitrialSync.Admission` -- steps 1-4: the idempotency key, the prior
      mutation, optimistic concurrency, and the payload decode
    * `VitrialSync.ItemDeleteGuard` -- step 5, the immutable-audit-history
      item-delete guard (S-58)
    * `VitrialSync.Lifecycle` -- step 6, blocker and quotation state machines
    * `VitrialSync.Delivery` and `VitrialSync.Delivery.Handoff` -- step 7a,
      delivery authority (S-60)
    * `VitrialSync.Ownership` -- step 7b, generic record authorization and its
      ownership plan (S-61)
    * `VitrialSync.EvidenceGC` -- step 8, the evidence GC enqueue (S-62)

  `VitrialSync.Authority` is steps 5-8 as one pipeline, because the order is the
  contract: three of the four can refuse and the first refusal is the one the
  client sees. `VitrialSync.RejectionCode` is the closed vocabulary every refusal
  answers with, and `VitrialSync.Canonical`, `VitrialSync.Value`,
  `VitrialSync.Provenance` and `VitrialSync.Decimal` are the shared vocabulary
  those decisions are written in.

  ## What is not, and why that is visible

  Every decision returns the work it would do -- upsert tuples, a scope grant, a
  GC row -- as data, because a pure function cannot write it. The transaction
  that applies them, the storage it reads them from, and the HTTP surface are
  the next slices. They are absent rather than stubbed: there is no
  `apply_push/1` that returns `:not_implemented`, because a function that looks
  like the sync engine and cannot sync is worse than no function -- the next
  caller has no way to tell the difference.
  """
end
