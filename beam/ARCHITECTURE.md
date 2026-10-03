# BEAM architecture — where this is, and what it needs

Status as of 2026-10-03. Written against the code, not the plan: every count
below was measured in this tree on this date, and where a number comes from a
document rather than a measurement it says so and names the document.

Every figure in §1, §2 and §3 came out of one of these, run in this tree:

```
./scripts/beam_gate.sh                      # format + compile + test, exit code
python3 scripts/check_beam_orphans.py
python3 scripts/check_beam_scaffold.py
find beam/apps/vitrial_sync/lib -name '*.ex' | xargs wc -l
podman run … parity_oracle.py --repo /w     # the parity corpus, as data
```

The companion documents are `beam/README.md` (what the umbrella is and why),
`../vitrial-phase0/BEHAVIOUR-SPEC.md` (the rule set both implementations answer
to), `../vitrial-phase0/DEPENDENCY-DOCTRINE.md` (every adopt/reject call with its
reasoning) and `../vitrial-phase0/VERSIONS-AND-BUDGETS.md` (measured envelopes,
the only source for §4).

---

## 1. The one-paragraph version

`vitrial_sync` is the only app with real content: **9,897 lines across 28 modules**
and **587 passing tests** (584 tests plus 3 doctests), all of it **pure
computation** over already-validated input. The other seven apps are declared,
documented scaffolds. There is **no database layer, no HTTP layer, and no
`mix.lock`** — `beam/mix.lock` and `beam/deps` do not exist, and nothing in
`lib/` opens a socket, touches a file or starts a process.
The port is **one app of eight built**, and inside that app wave 4 — the part
that needs no decisions — is built through all four of its code slices: 4a (push
steps 5–9), 4b (pull visibility resolution), 4c (the push batch walk) and 4d
(the differential harness) all stand in this tree, and 4e closes the wave as
the document-truth round this revision of this file performs (§3). One
precision on "landed", because the tree is shared: part of 4c is committed at
`a2413ae` (whose message says the work was in flight) and the remainder of the
4c changes were uncommitted working-tree edits when every figure here was
measured — so no landing below is backdated to a commit. Everything after
wave 4 is downstream of two decisions — how we reach PostgreSQL, and how we
reach HTTP — and both of those decisions are already written down and argued in
`../vitrial-phase0/DEPENDENCY-DOCTRINE.md`. The work is not "decide the
architecture." It is "execute decisions already made."

---

## 2. What exists

### 2.1 `vitrial_sync` — built, tested, and differentially proven

Twenty-eight modules, and the split matters because roughly half of them have no
business being in a service at all. Lines are `wc -l` per file, measured:

| Module | Lines | Nature | Why it exists |
|---|---:|---|---|
| `ownership.ex` | 1684 | pure | S-61, admission step 7b: generic record authorization and its ownership plan. The largest module in the tree — `authorize_record` plus the 13 entity types' own rules. |
| `delivery/handoff.ex` | 1436 | pure | The four delivery handoff validators, kept apart from the authority gate. |
| `delivery.ex` | 797 | pure | S-60, admission step 7a: delivery authority. |
| `batch.ex` | 683 | pure | The push batch walk: `EntityOrder` sort, one `Admission.decide/6` per record against the working scope, outcomes keyed by original index, and every database effect returned as data for the caller's one transaction. |
| `lifecycle.ex` | 662 | pure | S-59, admission step 6: immutable server-side lifecycle validation. |
| `canonical.ex` | 449 | pure | The canonical server state the admission decisions read, as data. |
| `json.ex` | 429 | pure | Strict RFC 8259 decoder. Hand-written because the doctrine forbids hex packages and OTP's `:json` cannot reject what it must reject here. |
| `prefetch.ex` | 403 | pure | Batched pull visibility resolution: `load_page_context/3`, `resolve_visible/3`, `resolve_delivery_visible/2`. |
| `admission.ex` | 389 | pure | Steps 1–4 of the push decision: missing id → prior mutation → revision guard → payload decode; `decide/6` threads the working scope and returns the decoded payload on the accept effects. |
| `page.ex` | 376 | pure | Pull page assembly with **exact wire sizing** — the page-fit decision is made on bytes, not on entries. |
| `canonical_json.ex` | 330 | pure | Canonical encoding for fingerprints, widened to the full JSON value space. |
| `decimal.ex` | 237 | pure | Exact decimal arithmetic, for the two places the delivery authority compares money. |
| `record.ex` | 226 | pure | Schema validation, mirroring Pydantic's constraints exactly. |
| `authority.ex` | 230 | pure | Admission steps 5–8, as one ordered pipeline over the steps 1–4 verdict. |
| `mutation_event.ex` | 86 | pure | The `sync.mutation_result` event body, exactly `_outcome_event`'s eight wire fields, as data. |
| `value.ex` | 164 | pure | Python's `==` comparison rules for decoded JSON, where Elixir's differ. |
| `retry.ex` | 149 | pure | SQLSTATE classification. Transient allow-list `40001`/`40P01`/`55P03`. |
| `cursor.ex` | 145 | pure | Cursor encode/decode with an explicit int64 ceiling. |
| `provenance.ex` | 133 | pure | The five-connected-provenance check and the field-comparison rule that makes it work. |
| `rejection_code.ex` | 131 | pure | The closed rejection vocabulary of the sync outcome stream. |
| `payload.ex` | 128 | pure | base64 → JSON-object decode, mirroring the reference's exact fallback semantics. |
| `entity_order.ex` | 124 | pure | The 13 entity types and their ordering. |
| `evidence_gc.ex` | 118 | pure | S-62, admission step 8: the evidence GC enqueue. |
| `timestamp.ex` | 100 | pure | `isoformat()`-compatible rendering. |
| `fingerprint.ex` | 99 | pure | Request identity, byte-identical to `app/idempotency.py`. |
| `item_delete_guard.ex` | 84 | pure | S-58, admission step 5: the immutable-audit-history item-delete guard. |
| `vitrial_sync.ex` | 76 | — | The app's own docs: the boundary, and why nothing here is a process. |
| `application.ex` | 29 | — | An intentionally empty supervisor, so later slices add a child spec instead of introducing supervision late. |

**587 passing — 584 tests plus 3 doctests — counted by `./scripts/beam_gate.sh`
(this tree, with the wave-4c work of `a2413ae` plus its uncommitted remainder in
the working tree: `Result: 587 passed (3 doctests, 584 tests)`, exit 0),
whose gate image (`beam/Dockerfile.gate`: the pinned image plus `python3` and
`pydantic`) mounts the repository root precisely so the harness below can
execute the reference.** The bare pinned image has no Python: driven through
the gate's documented `BEAM_BASE_IMAGE_ONLY=1` switch — the bare image is on
this host, so this figure is a fresh run, not arithmetic — the same suite
reports `579 passed (3 doctests, 576 tests), 8 skipped`: exactly the eight test
blocks of `VitrialSync.ParityTest` skip, printing
`[parity] differential suite SKIPPED: no python3 on PATH` into the gate output,
while the drift guard still runs, because it needs only `app/`'s source.
`ci.yml`'s BEAM job is the shape where neither can run — `-v "$PWD/beam:/work"`,
no reference tree — and there the suite reports `556 passed (3 doctests, 553
tests), 31 skipped`, printing both skip reasons (the parity eight and the drift
guard's twenty-three). All three figures were re-measured on this tree, by
running each shape.

> The `BEAM_BASE_IMAGE_ONLY=1` switch used above is the fixed form of a defect
> this document once recorded here: the variable used to be read only inside the
> build-on-demand block, after `IMAGE` had already defaulted to the gate image,
> so on any host with the gate image it silently did nothing and a bare-image
> run had to be produced through the `BEAM_IMAGE` override instead. It was fixed
> at `414b7a0` — the variable is now read before `IMAGE` is chosen
> (`scripts/beam_gate.sh:51-63`) — and the bare-image figure above is that fixed
> path run for real.

More important than the count: the tests are *differential against the real
Python*, not written against a reading of it, and the machinery is committed and
runnable, not a description of a run someone once did:

- `beam/apps/vitrial_sync/test/support/parity_oracle.py` (1,988 lines) — the
  Python side, stdlib + pydantic only. It cannot `import app.sync_service` (that
  pulls in SQLAlchemy), so it AST-lifts what it measures: `InvalidMutation`,
  `decode_payload`, `pull_page_accepts`, the steps 1–3 of `_apply_push_once`
  (sliced at the step-4 `try:`), `request_fingerprint`, `EffectiveScope`,
  `mutation_sort_key`, `correlation_ref` — executed against the real
  `app.schemas.SyncBatch`/`SyncRecord`, whose `model_dump_json()` is the
  page-wire oracle itself. Every input the Elixir side tests is emitted by the
  oracle as data; the suite never retypes a case.
- `beam/apps/vitrial_sync/test/support/python_reference.ex` (167 lines) — locates
  the interpreter, runs the oracle once per suite, keeps its stderr out of the
  JSON stream and raises with that stderr attached if it fails.
  `VitrialSync.ParityTest` skips, printing why, when there is no interpreter or
  no reachable reference tree.
- `beam/apps/vitrial_sync/test/vitrial_sync/drift_test.exs` (343 lines, 23 tests) —
  the drift guard. It needs only the reference *source*, so it is deliberately a
  separate module that does not inherit the differential suite's skip: it runs
  wherever `app/` is readable, interpreter or not, and asserts the constructs the
  port encodes decisions about — the three replay reasons in source order, both
  arms of the revision guard, the falsy `clientMutationID` test, the serialized-batch
  measurement, the ceiling order, the rejection vocabulary, and (since 4c) the
  batch assembly lines: the replay answer's revision, the original-order result
  lists, the `seq:` cursor, `_next_revision`'s coalesce, the per-accept flush,
  and the decode → `validate_assembly_contract_mutation` order — are still in
  `app/sync_service.py` and `app/rejection_codes.py`.

Measured 2026-10-03, this tree, by running the oracle and reading its document:

- admission decisions: **44 cases / 50 records, every `{status, reason,
  result_revision}` identical**, including the step-4 boundary — a record that
  passes steps 1–3 must produce exactly `Payload.decode/1`'s verdict
- the push batch walk: **7 batch cases, every per-index
  `{status, reason, result_revision}` identical and every `_outcome_event` map
  key-for-key identical**, including the one intra-batch case the slice can
  express — record A stale under `m1`, record B reusing `m1` and reading the
  rejection row A wrote mid-batch (`rejected_mutation_id_reuse` at A's current
  revision). The accept-path half of commit visibility — a duplicate of an
  *accepted* id, staleness against a same-batch create's *new* revision — is
  past the slice boundary and pinned unit-only in `batch_test.exs`, which says
  why in its own moduledoc
- pull page wire sizes: **117 cases (17 named + 100 seeded-random),
  `Page.batch_byte_size/2` byte-identical to real `SyncBatch.model_dump_json()`
  output on every case**, with each of the three ceilings reached by a named case
  — `count_ceiling` at 200 records (42,020 B), `payload_ceiling` at
  1,050,246 B, `response_ceiling` at 169 records / 2,247,982 B, which is the
  page the next record would push past `MAX_SYNC_PULL_RESPONSE_BYTES`
- payload decode: **169 cases: 114 rejected by both estates, 42 accepted with
  byte-identical canonical encodings, and 13 divergences that are exactly the
  two documented narrowings** — 7 non-finite numbers (`NaN`, `±Infinity`,
  `±1e400`, one nested, one base64-wrapped) and 6 unpaired surrogates, which
  CPython's `json.loads` accepts and `VitrialSync.JSON` rejects by design

These figures replace the ones previously quoted here (29/29, 100/100, 248/258).
They could not be reproduced: no corpus existed in the tree that would produce
them — the 258-case payload corpus never existed anywhere, only in the commit
messages of `3d87468` and `36f32b1` — so they were the unrepeatable record of an
earlier, uncommitted harness. The counts above are what the committed oracle
emits, and `ParityTest` asserts them (`@documented` in
`test/vitrial_sync/parity_test.exs`) against each fresh oracle run, so this
document and the corpus cannot now drift apart silently. The gate runs the suite
for real (that is what its image adds); `ci.yml`'s BEAM job is the one lane where
neither half can run, and it says so in its output rather than pretending.

**On the wire format, measured rather than assumed.** The reference's own
docstring at `app/sync_service.py:71-73` says the page-fit predicate exists
"once base64 and per-record framing are included". There is no base64 layer: a
`SyncRecord.payload` of 1,000 raw bytes serializes inside a 6,239-byte batch
because Pydantic emits `\u0000` escapes, and one carrying invalid UTF-8 raises
`PydanticSerializationError` rather than being base64'd. What replaces base64 is
JSON string escaping, which expands — 1.12× for non-ASCII text, 6.24× for
control bytes — and it is joined by ~243 bytes of per-record framing for a
typical JSON-object payload. The predicate is still necessary and still measured
on real bytes; the reason it is necessary is framing plus escaping, not base64.

Twelve implementation defects have now been found *only by executing* the
modules: eleven before 4c — three at a time in `2a14dcc` (3), `3d87468` (5) and
`36f32b1` (3): a string accumulator that was never reversed, a
`characters_to_binary` return-shape assumption that broke every UTF-16
document, an `Integer.digits/2` call that returned a list rather than a count —
and the twelfth during 4c, when the evidence-GC batch case refused with
`authorization_rejected`: `ownership.ex`'s item-child arm fired
`absent_canonical` on `generic_exists` alone, where the reference guards with
`canonical is None AND generic_entity_exists` (`app/ownership.py:597-598`), so
the port refused every legitimate update of an existing item child. None of the
twelve were visible from reading the code. The harness has also caught one on
the *reference* side the same way: `SyncRecord.payload` is typed `bytes` with no
UTF-8 check (`app/schemas.py:76`), so a record carrying non-UTF-8 payload
validates, and `pull_page_accepts`'s `model_dump_json()` then raises
`PydanticSerializationError` — an unhandled 500 out of the pull loop. The pin
is the `non_utf8_payload` case of the parity corpus, so a reference fix that
changes the shape of the failure is visible here. That is the argument for
continuing in this mode rather than writing more from first principles.

### 2.2 The other seven apps — declared, and declared on purpose

Each is a `mix new` scaffold with a `@moduledoc` describing its eventual job, and
each is listed in `beam/apps/PLANNED_APPS.txt` with that description.

This is sanctioned, not debt. `scripts/check_beam_scaffold.py` fails CI if an
app is a scaffold nobody declared, and equally fails if a declared app is built
but its line left behind. The umbrella is a map; declaring the destinations is
what makes the map a plan rather than a directory of empty promises. Measured:
`beam apps: 8  built: 1  declared scaffolds: 7`, and `vitrial_sync` carries 28
modules beyond its scaffold.

### 2.3 Guards that keep it honest

- `check_beam_scaffold.py` — the set of unbuilt apps is a decision, and a
  decision `mix new` can change silently is not a decision.
- `check_beam_orphans.py` — a `lib/` module that nothing calls and no test names
  is dead code. `mix compile --warnings-as-errors` catches unused *variables*;
  it structurally cannot catch unused *modules*, because an unreferenced module
  compiles perfectly clean.

Both run in this tree on every push and pass now. The exact verdicts, measured
2026-10-03:

```
check_beam_orphans.py  →  elixir sources: 100 (lib 55, test 36)  modules: 66
                          reachable: 65  specified-by-test: 0  exempt: 0
                          no dead modules
check_beam_scaffold.py  →  beam apps: 8  built: 1  declared scaffolds: 7
                          scaffold registry is consistent
```

---

## 3. What is missing, in dependency order

This ordering is the whole argument of this document. Each wave is blocked by
something in the wave above it, and the blocking is not stylistic — it is that
the layer below has no interface to test against until it exists.

### Wave 4 — finish the sync engine (no new dependencies)

Everything here is pure logic against types that already exist. **This wave was
built with zero hex packages** — that was the point of doing it before the
decisions below. State measured 2026-10-03:

| Slice | What | State |
|---|---|---|
| 4a | Push steps 5–9 | **Built.** `item_delete_guard.ex`, `lifecycle.ex`, `delivery.ex` + `delivery/handoff.ex`, `ownership.ex`, `evidence_gc.ex`, wired as one ordered pipeline through `authority.ex` behind `Admission.decide/2` |
| 4b | Pull visibility resolution | **Built.** `prefetch.ex` — `load_page_context/3`, `resolve_visible/3`, `resolve_delivery_visible/2` |
| 4c | The push batch walk | **Built.** `batch.ex` — `EntityOrder` sort, one `Admission.decide/6` per record against the WORKING scope (entry scope widened after every accepted create, `app/ownership.py:865`/`:884`), outcomes keyed by original index, `MutationEvent`s in processing order, the `SyncResult` wire response built by `finish/2` from the caller's post-commit sequence read — and every database effect returned as data for the caller's one transaction. One reference step is deliberately absent: `validate_assembly_contract_mutation` (`app/sync_service.py:319-323`) is its own vertical, tracked as VITR-V015 and named in `batch.ex`'s step table. |
| 4d | Differential harness | **Built** (`342f9be`) — see §2.1 |
| 4e | Document truth | **Built** — this revision of this file is the slice. Its work: every count in §1–§3 re-measured against this tree on 2026-10-03 by running the three gate shapes, both guards and `wc -l` per module; stale figures corrected; the remainder restated below. Uncommitted at measurement time, like the 4c remainder above — nothing here is backdated |

**4c was the last thing in wave 4 that can be built without a database, and it
is built.** What wave 4 does NOT contain, on purpose: the database effects are
decided but never applied — revision assignment, the canonical row write, the
mutation row and the changelog write come back in `%Batch.Effects{}` for the
caller — and the metric increment, the log line and the post-commit
`max(SyncChangeLog.sequence)` cursor read are caller work by the same argument:
a pure function cannot commit, cannot observe by counting, and cannot read what
does not exist until the transaction lands.

**With 4e, wave 4 closes.** Its four code slices are built, the caller carve-out
above stands, and the counts in this document now reproduce from the commands in
the header. What remains is exactly two things, neither of them a wave-4 slice:
`validate_assembly_contract_mutation`, standing outside the wave as its own
vertical — tracked as VITR-V015 in `trackers/vitrial-api-TRACKER.md`, fenced on
the BEAM side in `batch.ex`'s step table and on the reference side by the drift
guard — and the dependency-ordered waves below.

**4a** was specified against `app/sync_service.py:227-477` (`_apply_push_once`,
measured: the next `def` is at `:479`). Each step is a decision function over
plain maps today — that is the point of doing them here before the database
arrives, because a decision function with a database behind it cannot be tested
without one. **4c** was specified against the same function's loop
(`:240-443`): the sort, the per-record decision with commit visibility threaded
through the store — a same-batch duplicate must see the row the first record
wrote (`tests/test_push_preread_deferred.py:240-279`) — and the working-scope
widening that is the reference's flagship behaviour, one batch creating a
Customer and then a Project under it in arbitrary client order. The flagship
unit test pins it under a RESTRICTED principal, because under
organization-wide fixtures the wrong semantics (a scope frozen at entry) still
pass.

**4b** was specified against `app/pull_prefetch.py:116-314`. The constraint that
matters: `resolve_visible` and `resolve_delivery_visible` must stay line-for-line
twins of `record_is_visible` and `delivery_execution_is_visible`, because they
are a batched reimplementation of them. They are pure functions over a loaded
page, so they port cleanly — but only if the page context struct is ported first.

With wave 4 closed, `vitrial_sync` is a complete, fully-tested engine that
transforms validated input into decisions and pages. It still cannot serve a
request, and that is the correct stopping point.

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
  because the service has 18 tables and 125 mapped columns and only 4 raw SQL
  call sites, which makes it look ORM-shaped. The doctrine rejects it anyway:
  the value of an ORM is change tracking and schema-drift detection, and this
  service's writes are hand-rolled state machines (`ownership.py` 1,098 lines,
  `lifecycle.py` 350 lines) that Ecto changesets would fight rather than help.
  (18 `__tablename__` declarations across `app/`; 125 `Column(`/`mapped_column(`
  assignments. Both measured in this tree.)

The honest caveat, already recorded: `epgsql`'s last **hex release** is
2025-06-06, ~16 months stale, though the repository itself is active (last push
2026-08-16). That is a real risk to accept knowingly, not hide.

Wave 5 delivers: the pool, the row codecs for the 18 tables, and the migration of
`vitrial_sync`'s decision functions from pure maps onto real rows — at which
point the differential harness gains a database-backed mode.

### Wave 6 — HTTP: `vitrial_web`

**Also already decided.** `cowboy` 2.19.0 (+ mandatory `cowlib` 2.20.0,
`ranch` 2.3.0) for HTTP/1.1. `beam/config/*.exs` already configures
`VitrialWeb.Endpoint`, so the app is not inert — it is the only scaffold with
live configuration.

Rejected, with reasons in the doctrine: `bandit` (8 transitive dependencies to
serve 23 routes with no WebSocket — byte-for-byte the mistake this repository's
own `pyproject.toml` documents and removed), and `plug` (a framework replacing
23 case clauses with a framework, whose one useful export `secure_compare/2` is
already native as `crypto:hash_equals/2`). The route count is measured:
`grep -rE '^@(app|router)\.(get|post|put|delete|patch|head|options)\(' --include='*.py' app/`
→ 23, across `main.py`, `auth_session_routes.py`, `reference_routes.py` and
`sync_v2_routes.py`.

Also worth flagging: **no native OpenAPI generator exists.** This is the one place
where the rewrite-native doctrine costs something real, because the Python side
generates its OpenAPI document from type hints and the contract validation
(`scripts/validate_backend_contract.py`) checks against it.

Wave 6 delivers the boundary only — no business logic, per the README.

### Waves 7–9 — the remaining apps, in ascending order of risk

`vitrial_ownership` (1,098 Python lines — the largest single module in the
service, 13 entity types and the canonical parent chain), `vitrial_delivery`
(900 + 630 lines, an 8-state machine with plan freezing and append-only
history), `vitrial_evidence` (streamed SHA-256, multipart staging, GC), then
`vitrial_lifecycle`, `vitrial_auth`, `vitrial_reference`.

`vitrial_ownership` first despite being a scaffold today, because the push
pipeline's authority steps call into it — waves 4a and 7 are the same work seen
from two ends.

---

## 4. The measured constraints this design answers to

**Every number in this section is quoted from `../vitrial-phase0/VERSIONS-AND-BUDGETS.md`,
which measured it on that document's own host. None of it was re-measured here,
and it is the only section of this file that is not a measurement of this tree.**

- **Message send** (`gen_server:call`, one VM) p50 1.55 µs / p99 3.19 µs on an
  idle VM with one caller — p99 2.96 µs with 8 dirty CPU schedulers saturated —
  so a per-record fan-out is nearly free.
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
  implementation, and the committed differential harness
  (`beam/apps/vitrial_sync/test/support/parity_oracle.py`, executed by
  `VitrialSync.ParityTest` through `python_reference.ex`) runs it and compares
  against it. Deleting it would delete the only thing that can prove the BEAM
  code is right.
- **No hex package without a written verdict.** Every adoption needs a row in
  `DEPENDENCY-DOCTRINE.md` explaining what it replaces and why the native option
  is worse. `beam/mix.lock` does not exist yet, and that is correct: it appears
  when the first package is admitted, not before.
- **No abstraction layers, no framework-shaped dependencies.** `bandit` and
  `plug` are rejected on exactly this ground, and the estate-wide lesson is that
  abstraction layers are where dependency chains go to become unmanageable.
- **`vitrial_web` holds no business logic.** It is a boundary. The moment it
  grows a decision, the split has failed and the umbrella is eight monoliths.