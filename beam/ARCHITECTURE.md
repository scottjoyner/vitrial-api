# BEAM architecture — where this is, and what it needs

Status as of 2026-10-02. Written against the code, not the plan: every count
below was measured in this tree, and where a number comes from a document rather
than a measurement it says so.

The companion documents are `beam/README.md` (what the umbrella is and why),
`/mnt/ssd-relief/Projects/vitrial-phase0/BEHAVIOUR-SPEC.md` (the rule set both
implementations answer to), `DEPENDENCY-DOCTRINE.md` (every adopt/reject call
with its reasoning) and `VERSIONS-AND-BUDGETS.md` (measured envelopes).

---

## 1. The one-paragraph version

`vitrial_sync` is the only app with real content: 2,332 lines across 12 modules
and 230 tests, all of it **pure computation** over already-validated input.
The other seven apps are declared, documented scaffolds. There is **no database
layer, no HTTP layer, and no `mix.lock`** — nothing in this tree has ever talked
to PostgreSQL or opened a socket. The port is roughly a quarter built, and the
quarter that exists is the quarter that could be built without deciding anything
expensive. Everything still ahead is downstream of two decisions — how we reach
PostgreSQL, and how we reach HTTP — and both of those decisions are already
written down and argued in `DEPENDENCY-DOCTRINE.md`. The work is not "decide the
architecture." It is "execute decisions already made."

---

## 2. What exists

### 2.1 `vitrial_sync` — built, tested, and differentially proven

Twelve modules, and the split matters because roughly half of them have no
business being in a service at all:

| Module | Lines | Nature | Why it exists |
|---|---:|---|---|
| `json.ex` | 429 | pure | Strict RFC 8259 decoder. Hand-written because the doctrine forbids hex packages and OTP's `:json` cannot reject what it must reject here. |
| `page.ex` | 370 | pure | Pull page assembly with **exact wire sizing** — the page-fit decision is made on bytes, not on entries. |
| `admission.ex` | 255 | pure | Steps 1–4 of the push decision: missing id → prior mutation → revision guard → payload decode. |
| `canonical_json.ex` | 252 | pure | Canonical encoding for fingerprints, widened to the full JSON value space. |
| `record.ex` | 226 | pure | Schema validation, mirroring Pydantic's constraints exactly. |
| `retry.ex` | 149 | pure | SQLSTATE classification. Transient allow-list `40001`/`40P01`/`55P03`. |
| `cursor.ex` | 145 | pure | Cursor encode/decode with an explicit int64 ceiling. |
| `payload.ex` | 128 | pure | base64 → JSON-object decode, mirroring the reference's exact fallback semantics. |
| `entity_order.ex` | 124 | pure | The 13 entity types and their ordering. |
| `timestamp.ex` | 100 | pure | `isoformat()`-compatible rendering. |
| `fingerprint.ex` | 99 | pure | Request identity, byte-identical to `app/idempotency.py`. |
| `application.ex` | 29 | — | An intentionally empty supervisor, so later slices add a child spec instead of introducing supervision late. |

**230 passing — 227 tests plus 3 doctests, counted by `mix test` in the pinned
container, not by grepping the files.** More important than the count: the tests are
*differential against the real Python*, not written against a reading of it. The
harnesses AST-lift the reference functions and execute them rather than
retyping them, and a drift guard re-reads `app/sync_service.py` to catch a
transcription that has gone stale. Measured parity:

- admission decisions: **29/29 identical**
- pull page wire sizes: **100/100 identical**, checked against real
  `SyncBatch.model_dump_json()` output
- payload decode: **248/258**, the 10 differences being two deliberate, documented
  narrowings (unpaired surrogates, overflow-to-infinity) across five encodings

Eleven implementation defects were found *only by executing* the modules — a
string accumulator that was never reversed, a `characters_to_binary` return-shape
assumption that broke every UTF-16 document, an `Integer.digits/2` call that
returns a list rather than a count. None of them were visible from reading the
code. That is the argument for continuing in this mode rather than writing more
from first principles.

### 2.2 The other seven apps — declared, and declared on purpose

Each is a `mix new` scaffold with a `@moduledoc` describing its eventual job, and
each is listed in `beam/apps/PLANNED_APPS.txt` with that description.

This is sanctioned, not debt. `scripts/check_beam_scaffold.py` fails CI if an
app is a scaffold nobody declared, and equally fails if a declared app is built
but its line left behind. The umbrella is a map; declaring the destinations is
what makes the map a plan rather than a directory of empty promises.

### 2.3 Guards that keep it honest

- `check_beam_scaffold.py` — the set of unbuilt apps is a decision, and a
  decision `mix new` can change silently is not a decision.
- `check_beam_orphans.py` — a `lib/` module that nothing calls and no test names
  is dead code. `mix compile --warnings-as-errors` catches unused *variables*;
  it structurally cannot catch unused *modules*, because an unreferenced module
  compiles perfectly clean.

---

## 3. What is missing, in dependency order

This ordering is the whole argument of this document. Each wave is blocked by
something in the wave above it, and the blocking is not stylistic — it is that
the layer below has no interface to test against until it exists.

### Wave 4 — finish the sync engine (no new dependencies)

Everything here is pure logic against types that already exist. **This wave can
be built today with zero hex packages.**

**4a. Push steps 5–9.** `Admission` currently stops at payload decode. From
`app/sync_service.py:223-463`, the remaining steps are: the item-delete guard
(`_item_has_immutable_audit_history`), lifecycle validation
(`validate_lifecycle_mutation`), delivery authority
(`authorize_delivery_execution`), generic ownership (`authorize_record` +
`apply_ownership_plan`), and evidence GC enqueue (`queue_blob_gc`). Each is a
decision function over plain maps today — that is the point of doing them here
before the database arrives, because a decision function with a database behind
it cannot be tested without one.

**4b. Pull visibility resolution.** `load_page_context` / `resolve_visible` /
`resolve_delivery_visible`, from `sync_service.py:463-557`. The constraint that
matters: `resolve_visible` and `resolve_delivery_visible` must stay line-for-line
twins of `record_is_visible` and `delivery_execution_is_visible`, because they
are a batched reimplementation of them. They are pure functions over a loaded
page, so they port cleanly — but only if the page context struct is ported first.

**4c. Wire assembly.** The last thing that can be built without a database: turn
`Page` plus visibility results into the exact wire response.

After wave 4, `vitrial_sync` is a complete, fully-tested engine that transforms
validated input into decisions and pages. It still cannot serve a request, and
that is the correct stopping point.

### Wave 5 — the first real dependency: PostgreSQL

**This is the decision, and it is already made.** `DEPENDENCY-DOCTRINE.md` §4.2
argues it in full; the short version:

- `epgsql` 4.8.0 — pure Erlang, **zero transitive dependencies**, gives the
  PostgreSQL v3 wire protocol. **ADOPT.**
- A **hand-written connection pool.** `db_connection` is not needed for what is
  actually required, and eight independently auditable dependency trees are
  exactly the CVE surface the single-lockfile layout exists to collapse.
- **REJECT `postgrex`** — best-in-class, but it drags in `db_connection`,
  `decimal` and `telemetry`. For a service whose measured bottleneck is *not*
  ORM convenience, that is a poor trade.
- **REJECT `ecto`/`ecto_sql`** — and this one is genuinely counter-intuitive,
  because the service has 16 tables and 115 mapped columns and only 4 raw SQL
  call sites, which makes it look ORM-shaped. The doctrine rejects it anyway:
  the value of an ORM is change tracking and schema-drift detection, and this
  service's writes are hand-rolled state machines (`ownership.py` 1,073 lines,
  `lifecycle.py` 350 lines) that Ecto changesets would fight rather than help.

The honest caveat, already recorded: `epgsql`'s last **hex release** is
2025-06-06, ~16 months stale, though the repository itself is active (last push
2026-08-16). That is a real risk to accept knowingly, not hide.

Wave 5 delivers: the pool, the row codecs for the 16 tables, and the migration of
`vitrial_sync`'s decision functions from pure maps onto real rows — at which
point the differential harness gains a database-backed mode.

### Wave 6 — HTTP: `vitrial_web`

**Also already decided.** `cowboy` 2.19.0 (+ mandatory `cowlib` 2.20.0,
`ranch` 2.3.0) for HTTP/1.1. `beam/config/*.exs` already configures
`VitrialWeb.Endpoint`, so the app is not inert — it is the only scaffold with
live configuration.

Rejected, with reasons in the doctrine: `bandit` (8 transitive dependencies to
serve 19 routes with no WebSocket — byte-for-byte the mistake this repository's
own `pyproject.toml` documents and removed), and `plug` (a framework replacing
19 case clauses with a framework, whose one useful export `secure_compare/2` is
already native as `crypto:hash_equals/2`).

Also worth flagging: **no native OpenAPI generator exists.** This is the one place
where the rewrite-native doctrine costs something real, because the Python side
generates its OpenAPI document from type hints and the contract validation
(`scripts/validate_backend_contract.py`) checks against it.

Wave 6 delivers the boundary only — no business logic, per the README.

### Waves 7–9 — the remaining apps, in ascending order of risk

`vitrial_ownership` (1,073 Python lines — the largest single module in the
service, 13 entity types and the canonical parent chain), `vitrial_delivery`
(900 + 630 lines, an 8-state machine with plan freezing and append-only
history), `vitrial_evidence` (streamed SHA-256, multipart staging, GC), then
`vitrial_lifecycle`, `vitrial_auth`, `vitrial_reference`.

`vitrial_ownership` first despite being a scaffold today, because the push
pipeline's authority steps call into it — waves 4a and 7 are the same work seen
from two ends.

---

## 4. The measured constraints this design answers to

From `VERSIONS-AND-BUDGETS.md`, and they are worth restating because they are why
the BEAM port is not a lateral move:

- **Message send** p50 1.55 µs / p99 3.19 µs — a per-record fan-out is nearly
  free.
- **`term_to_binary`** on a 1 MiB payload: ~12 ms round trip at p50; 10 MiB is
  ~67 ms. Relevant because a sync page is passed between processes.
- **SHA-256** at ~1.5–1.7 GiB/s, so a 100 MiB evidence blob is ~60 ms to hash —
  and the transfer itself is comparable, which means hashing is *not* the
  bottleneck and should not be optimised first.
- **PostgreSQL** read path p50 70–120 µs, p99 0.3–0.5 ms on loopback with no
  load. The **1,000-row fetch at 838 µs p50** is the number that matters for a
  sync service, because that is the shape of a pull page.

The last one is the honest headline: the wire protocol, the JSON, and the OTP
schedulers all got faster, and the database round trip did not. This is why the
architecture's centre of gravity is *page assembly and batched visibility
resolution* — the places where per-row overhead becomes per-page overhead — and
not the request path.

---

## 5. What is deliberately not happening

- **The Python service is not being retired.** It is the reference
  implementation and the parity oracle, and the differential harnesses execute it.
  Deleting it would delete the only thing that can prove the BEAM code is right.
- **No hex package without a written verdict.** Every adoption needs a row in
  `DEPENDENCY-DOCTRINE.md` explaining what it replaces and why the native option
  is worse. `beam/mix.lock` does not exist yet, and that is correct: it appears
  when the first package is admitted, not before.
- **No abstraction layers, no framework-shaped dependencies.** `bandit` and
  `plug` are rejected on exactly this ground, and the estate-wide lesson is that
  abstraction layers are where dependency chains go to become unmanageable.
- **`vitrial_web` holds no business logic.** It is a boundary. The moment it
  grows a decision, the split has failed and the umbrella is eight monoliths.