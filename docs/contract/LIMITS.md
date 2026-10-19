# Request limits

Every ceiling the service enforces, what it is, and what a client gets for exceeding it.

This is a reference for client developers, not a change to the pinned contract
(`contracts/backend/v1/`). Both columns are machine-checked, so this page cannot drift:

- **Value** is compared against the source constant by `tests/test_contract_limits_doc.py`.
  Change a constant without updating this page and CI fails.
- **Exceeding it returns** is measured by `tests/test_contract_limits_postgres.py`, which
  actually exceeds each limit through the real app with a real session.

## Sync push

| Limit | Value | Source | Exceeding it returns |
|---|---|---|---|
| Records per batch (V1 and V2) | 200 | `app.schemas:MAX_SYNC_RECORDS` | 422, `too_long` on `records` |
| Aggregate payload per V1 batch | 2100000 | `app.schemas:MAX_SYNC_V1_BATCH_PAYLOAD_BYTES` | 422, `aggregate sync payload exceeds …` |
| Payload per V1 record (base64 wire text) | 2100000 | `app.schemas:MAX_SYNC_V1_WIRE_PAYLOAD_BYTES` | 422 |
| Canonical JSON payload per V2 record | 1500000 | `app.sync_v2:MAX_SYNC_V2_PAYLOAD_BYTES` | 422 |
| Identifier length (`id`, `entityID`, `deviceID`, `cursor`, `clientMutationID`) | 256 | `app.schemas:MAX_SYNC_IDENTIFIER_LENGTH` | 422 |
| Request body, V1 push | 4194304 | `app.request_size:DEFAULT_PATH_LIMITS[/api/v1/sync/push]` | 413, before the body is parsed |
| Request body, V2 push | 4194304 | `app.request_size:DEFAULT_PATH_LIMITS[/api/v2/sync/push]` | 413, before the body is parsed |

V1 payloads are base64 on the wire, and the per-record and aggregate ceilings count the
**base64 text**, not the decoded bytes. The iOS V1 client budgets 1.5 MB of raw payload per
batch; 2,100,000 leaves room for the base64 expansion.

## Sync pull — paging, not errors

These never produce an error. They end a page early, and the response cursor advances only
past changes actually consumed, so the next pull resumes at the first undelivered change.

| Limit | Value | Source | Effect |
|---|---|---|---|
| Changes scanned per pull | 500 | `app.sync_service:MAX_SYNC_PULL_SCAN_CHANGES` | page ends; cursor resumes after the last scanned change |
| Records per pull page | 200 | `app.schemas:MAX_SYNC_RECORDS` | page ends before the record that would exceed it |
| Payload per pull page | 2100000 | `app.schemas:MAX_SYNC_V1_BATCH_PAYLOAD_BYTES` | same |
| Serialized response per pull page | 2250000 | `app.sync_service:MAX_SYNC_PULL_RESPONSE_BYTES` | same |
| Cursor digits | 19 | `app.sync_service:MAX_CURSOR_DIGITS` | 400 above this or above 2⁶³−1 — not a 500 |

A page that ends because the next record does not fit is counted in
`vitrial_sync_pull_truncated_total`. Changes that are invisible to the caller, or whose
entity is gone, are consumed silently: they can never be delivered, so leaving them
unconsumed would stall the cursor.

## Evidence blobs

| Limit | Value | Source | Exceeding it returns |
|---|---|---|---|
| Blob size (streamed; no middleware ceiling by design) | 104857600 | `app.settings:Settings.evidence_max_bytes` | 413 |
| `X-Vitrial-Item-ID` header | 256 | `app.main:EVIDENCE_ITEM_ID_MAX_LENGTH` | 422 |
| `X-Vitrial-Filename` header | 512 | `app.main:EVIDENCE_FILENAME_MAX_LENGTH` | 422 |

The evidence PUT is the one route with no request-size middleware ceiling: blobs stream to
object storage in bounded chunks and are cut off at the configured maximum while streaming,
so a 100 MiB upload never sits in memory.

## Authentication surface

| Limit | Value | Source | Exceeding it returns |
|---|---|---|---|
| Pairing request body | 16384 | `app.request_size:DEFAULT_PATH_LIMITS[/api/v1/auth/pair]` | 413 |
| Admin request body | 65536 | `app.request_size:DEFAULT_PATH_LIMITS[/internal/admin/v1]` | 413 |
| Requests per client per window, per surface | 30 | `app.rate_limit:DEFAULT_LIMIT` | 429 with `Retry-After` |
| Rate-limit window, seconds | 60 | `app.rate_limit:DEFAULT_WINDOW_SECONDS` | — |

Rate limits apply to three surfaces, each with its own budget per client: pairing
(`/api/v1/auth/pair`), the admin plane (`/internal/admin/v1/*`), and `/metrics`. Refused
requests are answered before the handler runs and cost no database connection. Limits and
body ceilings can be overridden per deployment (`VITRIAL_AUTH_RATE_LIMIT`,
`VITRIAL_AUTH_RATE_WINDOW_SECONDS`, `VITRIAL_MAX_BODY_BYTES_<PATH>`); the values above are the
defaults.
