# VitrialSync

The sync engine.

  Push and pull, and the rules that decide what a mutation means.

  Built so far, all pure computation with no database:

    * `VitrialSync.Cursor` -- pull cursors, bounded to the `BIGINT` range that
      `SyncChangeLog.sequence` occupies
    * `VitrialSync.Fingerprint` -- request identity for a `clientMutationID`
    * `VitrialSync.CanonicalJSON` -- the byte-exact encoding the fingerprint hashes
    * `VitrialSync.Timestamp` -- ISO-8601 rendering identical to Python's

  `VitrialSync.Fingerprint` reproduces `app/idempotency.py` byte for byte. That
  parity is the entire point of the module and is pinned to digests produced by
  running the Python implementation, not to a re-reading of it. See
  `test/vitrial_sync/fingerprint_test.exs` for the regeneration recipe.
