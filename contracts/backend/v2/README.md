# Connected Operations protocol V2

V2 is an explicit native-JSON wire protocol over the same canonical revisioned sync engine as V1.

## Compatibility rules

- V1 remains unchanged: `payload` is the existing base64-encoded byte envelope.
- V2 requires `protocolVersion: 2` on every batch.
- V2 records carry native JSON objects in `payload` and an explicit `entitySchemaVersion`.
- The current supported entity schema version is `1`; unknown versions fail closed.
- V1 bytes are never silently reinterpreted as V2 payloads.
- A `clientMutationID` belongs to the exact wire request that first used it. Reusing a V1 mutation ID through V2 (or vice versa) is not a protocol-migration shortcut.
- Both protocols use the same authorization, ownership, lifecycle, idempotency, tombstone, revision, and cursor authority.

The fixtures in this directory are independent from `contracts/backend/v1` and intentionally use native JSON payloads.
