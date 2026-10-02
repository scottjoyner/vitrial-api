defmodule VitrialSync do
  @moduledoc """
  The sync engine: the only component allowed to decide what a mutation means.

  Everything implemented so far is pure computation over validated input. There
  is no process, no GenServer, and no state, because none of it needs any -- which
  is also why this app supervises no children (see `VitrialSync.Application`).

  ## What is implemented

    * `VitrialSync.Cursor` -- pull cursors, bounded to the `BIGINT` range that
      `SyncChangeLog.sequence` actually occupies
    * `VitrialSync.Fingerprint` and `VitrialSync.CanonicalJSON` -- request identity
      for a `clientMutationID`, byte-identical to `app/idempotency.py` on the
      Python service
    * `VitrialSync.Timestamp` -- the ISO-8601 rendering both sides agree on

  ## What is not, and why that is visible

  Page assembly, revision guards, visibility filtering and transient retry all
  need a database connection and are the next slices. They are absent rather than
  stubbed: there is no `apply_push/1` that returns `:not_implemented`, because a
  function that looks like the sync engine and cannot sync is worse than no
  function -- the next caller has no way to tell the difference.
  """
end
