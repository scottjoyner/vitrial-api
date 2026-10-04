# BEAM architecture — where this is, and what it needs

Status as of 2026-10-03. Written against the code, not the plan: every count
below was measured in this tree on this date, and where a number comes from a
document rather than a measurement it says so and names the document.

Every figure in §1, §2 and §3 came out of one of these, run in this tree:

```
./scripts/beam_gate.sh                      # format + compile + test, exit code
BEAM_BASE_IMAGE_ONLY=1 ./scripts/beam_gate.sh   # the bare-image shape, §2.1
python3 scripts/check_beam_orphans.py
python3 scripts/check_beam_scaffold.py
find beam/apps/vitrial_sync/lib -name '*.ex' | xargs wc -l
find beam/apps/vitrial_web/lib -name '*.ex' | xargs wc -l
podman run … parity_oracle.py --repo /w     # the parity corpus, as data
```

The companion documents are `beam/README.md` (what the umbrella is and why),
`../vitrial-phase0/BEHAVIOUR-SPEC.md` (the rule set both implementations answer
to), `../vitrial-phase0/DEPENDENCY-DOCTRINE.md` (every adopt/reject call with its
reasoning) and `../vitrial-phase0/VERSIONS-AND-BUDGETS.md` (measured envelopes,
the only source for §4).

---

## 1. The one-paragraph version

Two of the eight apps have real content. `vitrial_sync` is the engine:
**12,951 lines across 37 files** (`find beam/apps/vitrial_sync/lib -name '*.ex'
| xargs wc -l`; one `defmodule` per file. The scaffold guard's **"38 modules
beyond the scaffold"** is a file count over a wider net: the 35 lib files past
`mix new`'s baseline plus the three `.ex` helpers under `test/support/`) —
of which the wave-4 sync
engine proper is pure computation over validated input, and the wave-5 `Db/`
slice (9 files, 2,922 lines) is the first code here that is not. `vitrial_web`
is the HTTP boundary: **1,664 lines across 15 files**, backed by the umbrella's
first hex dependency (`beam/mix.lock`: cowboy 2.19.0 / cowlib 2.20.0 /
ranch 2.3.0, the doctrine's exact pin). Together: **723 passing tests (720
tests plus 3 doctests), 20 skipped** — every skip is the wave-5 live-PostgreSQL
suite declining to run without a server, and the skip line prints its reason
by name (§2.1). The other six apps are declared, documented scaffolds.
Wave 4 is closed: 4a (push steps 5–9), 4b (pull visibility resolution), 4c
(the push batch walk) and 4d (the differential harness) are built, and 4e was
the document-truth round this file's earlier revision performed (§3). Wave 5
landed the database floor (a hand-written PostgreSQL v3 client, a pool, row
codecs for 14 of the 18 tables — `3da3b87` plus the adaptation recorded in
§3), and wave 6 landed the HTTP boundary. One precision on "landed", because
the tree is shared: wave-4c's work reached `main` across two commits (`a2413ae`,
whose message says the work was in flight, and `9aa3582`), while the wave-5 and
wave-6 changes were uncommitted working-tree edits when every figure in this
revision was measured — the records pass committed them together with this
document, so no landing below is backdated to a commit it did not produce.
The two architecture decisions downstream of wave 4 — how we reach PostgreSQL,
and how we reach HTTP — were taken in `../vitrial-phase0/DEPENDENCY-DOCTRINE.md`
and are now executed code. The work is no longer "decide" or even "execute":
for the first time it is "cover the remaining tables, wire the engines behind
the 501s, and earn the live-database green the shared tree does not yet have
(§3, wave 5)."

---

## 2. What exists

### 2.1 `vitrial_sync` — built, tested, and differentially proven

Thirty-seven files, and the split matters because roughly half of them have no
business being in a service at all. Lines are `wc -l` per file, measured on the
landed tree:

| Module | Lines | Nature | Why it exists |
|---|---:|---|---|
| `ownership.ex` | 1684 | pure | S-61, admission step 7b: generic record authorization and its ownership plan. The largest module in the tree — `authorize_record` plus the 13 entity types' own rules. |
| `delivery/handoff.ex` | 1436 | pure | The four delivery handoff validators, kept apart from the authority gate. |
| `delivery.ex` | 797 | pure | S-60, admission step 7a: delivery authority. |
| `db/pool.ex` | 785 | process | The hand-written connection pool: checkout/check-in with ownership tracking, an abandoned-checkout eviction on timeout, crash absorption via a per-connection monitor table, drain for shutdown. The supervisor's first real child. |
| `batch.ex` | 683 | pure | The push batch walk: `EntityOrder` sort, one `Admission.decide/6` per record against the working scope, outcomes keyed by original index, and every database effect returned as data for the caller's one transaction. |
| `lifecycle.ex` | 662 | pure | S-59, admission step 6: immutable server-side lifecycle validation. |
| `db/pgconn.ex` | 617 | process | The hand-written PostgreSQL v3 wire client — startup packet with GUCs, MD5 auth, extended query. Landed at `3da3b87`; its known wire defects and their status are recorded in §3 wave 5. |
| `json.ex` | 551 | pure | Strict RFC 8259 decoder. Hand-written because the doctrine forbids hex packages and OTP's `:json` cannot reject what it must reject here. Grown 429 → 551 by wave 5: map encoding, the lexer's `, ` trap, and timestamp-clause fixes, each found by executing against a live server. |
| `canonical.ex` | 449 | pure | The canonical server state the admission decisions read, as data. |
| `prefetch.ex` | 403 | pure | Batched pull visibility resolution: `load_page_context/3`, `resolve_visible/3`, `resolve_delivery_visible/2`. |
| `admission.ex` | 389 | pure | Steps 1–4 of the push decision: missing id → prior mutation → revision guard → payload decode; `decide/6` threads the working scope and returns the decoded payload on the accept effects. |
| `page.ex` | 376 | pure | Pull page assembly with **exact wire sizing** — the page-fit decision is made on bytes, not on entries. |
| `db/rows.ex` | 357 | pure | Row codecs for 14 of the 18 domain tables (the README names the four missing). |
| `db/codec.ex` | 332 | pure | The PostgreSQL wire type codec: text/binary params, timestamps, numerics, JSON. |
| `canonical_json.ex` | 330 | pure | Canonical encoding for fingerprints, widened to the full JSON value space. |
| `db/writes.ex` | 277 | pure | The write SQL for the covered tables — the upsert/mutation/changelog/membership rows wave 4's `%Batch.Effects{}` described. |
| `decimal.ex` | 237 | pure | Exact decimal arithmetic, for the two places the delivery authority compares money. |
| `authority.ex` | 230 | pure | Admission steps 5–8, as one ordered pipeline over the steps 1–4 verdict. |
| `record.ex` | 226 | pure | Schema validation, mirroring Pydantic's constraints exactly. |
| `db/config.ex` | 199 | pure | Database configuration from the environment: URL parsing, GUC budgets, pool sizing — pydantic-settings semantics without pydantic. |
| `value.ex` | 164 | pure | Python's `==` comparison rules for decoded JSON, where Elixir's differ. |
| `db/conn.ex` | 153 | process | The wire-level connection: the three-call surface (`connect`/`query`/`close`) everything above the `Db` boundary sees. |
| `retry.ex` | 149 | pure | SQLSTATE classification. Transient allow-list `40001`/`40P01`/`55P03`. |
| `cursor.ex` | 145 | pure | Cursor encode/decode with an explicit int64 ceiling. |
| `db/url.ex` | 139 | pure | `postgresql://` URL parsing, mirroring the reference's accepted forms. |
| `provenance.ex` | 133 | pure | The five-connected-provenance check and the field-comparison rule that makes it work. |
| `rejection_code.ex` | 131 | pure | The closed rejection vocabulary of the sync outcome stream. |
| `payload.ex` | 128 | pure | base64 → JSON-object decode, mirroring the reference's exact fallback semantics. |
| `entity_order.ex` | 124 | pure | The 13 entity types and their ordering. |
| `evidence_gc.ex` | 118 | pure | S-62, admission step 8: the evidence GC enqueue. |
| `timestamp.ex` | 100 | pure | `isoformat()`-compatible rendering. |
| `fingerprint.ex` | 99 | pure | Request identity, byte-identical to `app/idempotency.py`. |
| `mutation_event.ex` | 86 | pure | The `sync.mutation_result` event body, exactly `_outcome_event`'s eight wire fields, as data. |
| `item_delete_guard.ex` | 84 | pure | S-58, admission step 5: the immutable-audit-history item-delete guard. |
| `vitrial_sync.ex` | 76 | — | The app's own docs: the boundary of the sync engine. (Its moduledoc sentence "this app supervises no children" at `:7` predates wave 5 and is now false — `VitrialSync.Application` supervises the pool. Recorded, not fixed: it is a code file.) |
| `db.ex` | 63 | — | The single database boundary: `with_conn/2`, `health/0`, `drain/1` — the four names nothing above may bypass, which is what made the epgsql→native swap a one-module change. |
| `application.ex` | 39 | — | The supervision root. Empty through wave 4 on purpose; wave 5 gave it its first child, the pool, with an explicit statement-timeout-derived `shutdown`. |

**723 passing — 720 tests plus 3 doctests — plus 20 skipped, counted by
`./scripts/beam_gate.sh` on the landed tree (`Result: 66 passed` for
`vitrial_web`; `Result: 657 passed (3 doctests, 654 tests), 20 skipped` for
`vitrial_sync`; PASS, exit 0), whose gate image (`beam/Dockerfile.gate`: the
pinned image plus `python3`, `pydantic` and the resolved hex cache) mounts the
repository root precisely so the harness below can execute the reference.**
All 20 skips are the wave-5 live-PostgreSQL suites declining to run without a
server, and the skip line is printed by name into the gate output:
`[db] live-postgresql suite NOT RUN (no server reachable at
$VITRIAL_TEST_DATABASE_URL): ... "transport raised exit: :badarg"` — the
`:badarg` is the shared tree's unfixed wire transport, §3 wave 5. The bare
pinned image has no Python: driven through the gate's documented
`BEAM_BASE_IMAGE_ONLY=1` switch, the same tree reports **715 passed (3
doctests, 712 tests), 28 skipped** — the eight `VitrialSync.ParityTest` blocks
skip with `[parity] differential suite SKIPPED: no python3 on PATH` (the drift
guard still runs: it needs only `app/`'s source), the same 20 live-db skips on
top. Both figures were measured on this tree on 2026-10-03 by running each
shape; 723 − 715 is exactly the parity eight.
`ci.yml`'s BEAM job is no longer a third shape: `e279fb2` pointed it at
`beam/Dockerfile.gate` with a repository-root mount (`-v "$PWD:/work"`,
`-w /work/beam`) and made it **fail the job on any `SKIPPED` line** — so CI now
runs the oracle where the old
text of this paragraph said it could not (`.github/workflows/ci.yml:242-281`:
the job builds the gate image at `:242-244`, mounts the root at `:252`, tests
at `:264`, fails on `SKIPPED` at `:277`). The old third figure this paragraph
carried (556 passed / 31 skipped, the bare image over a `beam/`-only mount)
describes a lane that no longer exists and was not re-run.

> The `BEAM_BASE_IMAGE_ONLY=1` switch used above is the fixed form of a defect
> this document once recorded here: the variable used to be read only inside the
> build-on-demand block, after `IMAGE` had already defaulted to the gate image,
> so on any host with the gate image it silently did nothing and a bare-image
> run had to be produced through the `BEAM_IMAGE` override instead. It was fixed
> at `414b7a0` — the selection now reads `BEAM_BASE_IMAGE_ONLY` first
> (`scripts/beam_gate.sh:58-64`, line numbers as of HEAD `77c83c2`; later gate
> commits shifted the block from the `:51-63` an earlier revision of this
> document cited) — and the bare-image figure above is that fixed path run for
> real.

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
for real (that is what its image adds); since `e279fb2`, CI's BEAM job runs it
too and fails the job if either half of the harness ever has to skip.

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

### 2.2 The other six apps — declared, and declared on purpose

Each is a `mix new` scaffold with a `@moduledoc` describing its eventual job, and
each is listed in `beam/apps/PLANNED_APPS.txt` with that description.

This is sanctioned, not debt. `scripts/check_beam_scaffold.py` fails CI if an
app is a scaffold nobody declared, and equally fails if a declared app is built
but its line left behind. The umbrella is a map; declaring the destinations is
what makes the map a plan rather than a directory of empty promises. Measured:
`beam apps: 8  built: 2  declared scaffolds: 6` (wave 6 built `vitrial_web`),
with `vitrial_sync` at 38 file(s) beyond its scaffold and `vitrial_web` at 13.

### 2.3 Guards that keep it honest

- `check_beam_scaffold.py` — the set of unbuilt apps is a decision, and a
  decision `mix new` can change silently is not a decision.
- `check_beam_orphans.py` — a `lib/` module that nothing calls and no test names
  is dead code. `mix compile --warnings-as-errors` catches unused *variables*;
  it structurally cannot catch unused *modules*, because an unreferenced module
  compiles perfectly clean.

Both run in this tree on every push and pass now. The exact verdicts, measured
2026-10-03 after wave 6 landed (pre-wave-6 the same commands reported 100
sources, 66 modules, built 1, declared scaffolds 7):

```
check_beam_orphans.py  →  elixir sources: 136 (lib 77, test 50)  modules: 88
                          reachable: 87  specified-by-test: 0  exempt: 0
                          no dead modules
check_beam_scaffold.py  →  beam apps: 8  built: 2  declared scaffolds: 6
                          built    vitrial_sync  (38 module(s) beyond the scaffold)
                          built    vitrial_web   (13 module(s) beyond the scaffold)
                          scaffold registry is consistent
```

(The counts above were measured by running the two commands on the landed
wave-6 tree; run them to reproduce. The orphans total exceeds the lib+test sum
by design: `check_beam_orphans.py` counts every hand-written `.ex`/`.exs` under
`beam/` — including the nine `mix.exs` and the `config/*.exs` files — while its
two buckets exclude mix files.)

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
| 4e | Document truth | **Built** — the document-truth slice, performed twice on 2026-10-03 because the tree kept landing under it. Its first revision re-measured every count against the wave-4 tree (587 passing); waves 5 and 6 then landed on top of it, so this revision re-measured again against the landed tree (723 passing, §2.1) and corrected what the landing falsified — §1's "no database layer, no HTTP layer, no mix.lock" above all. Both revisions measured by running the gate shapes, both guards and `wc -l` per file; nothing is backdated to a commit |

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

> **Superseded in part, 2026-10-03:** the `epgsql` ADOPT below was overtaken by
> commit `3da3b87`, which landed a hand-written zero-dependency client
> (`VitrialSync.Db.Pgconn`). `DEPENDENCY-DOCTRINE.md` §4.2/§5.3 carry the dated
> amendment with the measurements and the flip-back condition; the pool,
> startup-GUC and no-hex-dependency decisions below all stand.

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
2026-08-16). That is a real risk to accept knowingly, not hide. *(Historical:
superseded with the ADOPT itself — the risk was retired by not taking the
dependency, at the price recorded in the amendment above.)*

**Wave 5 state, 2026-10-03: the floor is landed; the live-database green is
not.** Measured on this tree:

- **What stands committed:** `3da3b87` (a parallel agent) replaced `epgsql`
  with the hand-written client — `db/pgconn.ex` (617 lines) behind
  `db/conn.ex`'s three-call surface and the `Db` boundary, zero hex
  dependencies.
- **What the wave added on top:** the adaptation of Codec/Rows/Writes/Pool to
  that client plus the configuration and URL modules (§2.1's `db/` rows —
  2,922 lines across the nine `db` files), the live-database support and five
  test files, two live-only pool defects fixed by executing (a `Map.pop`
  remainder-as-state crash on the first check-in, and a phantom waiter a timed-out
  checkout left for the next capacity event — now evicted with
  `{:checkout_abandoned, owner}`), and the codec fixes the live server forced
  (map-parameter encoding, the `, ` lexer trap, timestamp-clause coverage).
  These were uncommitted working-tree edits when measured; the records pass
  committed them.
- **Coverage boundary:** row codecs and write SQL cover **14 of the 18** domain
  tables — missing `auth_sessions`, `pairing_grants`,
  `reference_publications`, `users` (`beam/README.md:58-59`).
- **The verification line that matters:** the shared tree is
  **serverless-green — 657 passed / 20 skipped**, both shapes of the gate
  measured this session, with every skip printing
  `[db] live-postgresql suite NOT RUN (no server reachable … "transport raised
  exit: :badarg")`. The **677-passed live run against PostgreSQL 16.14 was
  measured on a working copy whose transport carried ~12 wire fixes that are
  deliberately NOT in this tree** — `db/pgconn.ex` and `db/conn.ex` are
  byte-identical to `3da3b87` (`git diff HEAD` on both is empty, measured).
  Consequence, stated rather than buried: this tree has never passed a
  live-database test; the doctrine amendment's flip-back trigger is met as
  written, and the transport fixes exist only in an ephemeral working copy.
- **Named as not-live-verified:** the SCRAM-SHA-256 auth arm (test server was
  MD5-only), extended-query pipelining or statement reuse, error-field parsing
  beyond S/C/M/D/H/P, and an actual `statement_timeout` deadline cancellation
  (`57014`) — the GUCs are sent and `idle_in_transaction_session_timeout` was
  SHOW-verified live, but no test demonstrates the cancellation.

What wave 5 still owes: the four uncovered tables, the transport fixes landed
or consciously rejected, and the migration of `vitrial_sync`'s decision
functions from pure maps onto real rows — at which point the differential
harness gains a database-backed mode.

### Wave 6 — HTTP: `vitrial_web`

**Also already decided.** `cowboy` 2.19.0 (+ mandatory `cowlib` 2.20.0,
`ranch` 2.3.0) for HTTP/1.1. `beam/config/*.exs` already configures
`VitrialWeb.Endpoint`, so the app was not inert even as a scaffold — it was the
only one with live configuration, and it is built now (below).

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

**Wave 6 state, 2026-10-03: landed.** `vitrial_web` is built: the cowboy
listener (the umbrella's first hex dependency — `beam/mix.lock` now exists,
pinned to the doctrine's exact shape), the hand-written 23-clause route table
and dispatch (the doctrine's §4.4 shape; the middleware mechanism is decided on
the record in `VitrialWeb.Handler`'s moduledoc, since cowboy's middleware runs
in-only and cannot observe statuses), the byte guard, the fail-open rate
limiter, correlation + NDJSON event wire shape, and two servable routes
(`/health`, `/api/v1/version`). Every other clause answers one uniform 501
until its engine lands. The contract check (`scripts/validate_backend_contract.py`)
still cannot be satisfied from this side: no native OpenAPI generator exists,
so the BEAM port carries no generated document — parity stays behavioural.
One standing fact the pin buys: `cowlib` 2.20.0 — forced transitively by the
doctrine's exact cowboy 2.19.0 pin, which leaves no version room — carries two
open advisories, EEF-CVE-2026-43966 (MEDIUM) and EEF-CVE-2026-43969 (LOW),
flagged by `mix hex.audit` when the dependency was vetted and visible in this
session's gate output (the hex resolution prints both under `VULNERABLE!`).
Advisory output only; recorded because the pin, not a choice, holds it open.

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
  is worse. `beam/mix.lock` appeared exactly the way this rule predicted — with
  the first admission (cowboy 2.19.0, verdict at doctrine §4.4) and not before;
  it is one lockfile covering the umbrella's eight apps, which is the collapse
  of CVE surface the layout exists for. The PostgreSQL wire client that could
  have been the first dependency was instead hand-written to zero of them
  (§3 wave 5).
- **No abstraction layers, no framework-shaped dependencies.** `bandit` and
  `plug` are rejected on exactly this ground, and the estate-wide lesson is that
  abstraction layers are where dependency chains go to become unmanageable.
- **`vitrial_web` holds no business logic.** It is a boundary. The moment it
  grows a decision, the split has failed and the umbrella is eight monoliths.