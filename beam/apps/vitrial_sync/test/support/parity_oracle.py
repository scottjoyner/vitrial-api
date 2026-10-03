#!/usr/bin/env python3
"""Differential oracle: run the Python reference and emit its answers as data.

The BEAM port is measured against this file's output. Everything the harness
compares comes out of the reference implementation itself, lifted out of the
reference tree by AST and executed, rather than retyped here. Nothing in this
file decides what the answer is.

    python3 beam/apps/vitrial_sync/test/support/parity_oracle.py --repo . \
        > /tmp/oracle.json

Writes ONE JSON object on stdout. `--only {all,admission,page,payload}` selects
which sections are present; the default is `all`, so the Elixir side runs this
exactly once per suite and reads three sections out of one document.

    {"limits": {...}, "admission": [...], "page": [...], "payload": [...]}

The output is `ensure_ascii=True`, so stdout is pure ASCII. That is not
cosmetic: several payload cases carry values Python accepts and the BEAM
deliberately rejects (non-finite numbers, unpaired surrogates), and one of them
would emit `NaN` into this document if the decoded value were placed in it
directly -- and `VitrialSync.JSON.decode/1` rejects `NaN` by design, so that
would poison the whole load. Every payload case therefore travels as base64
bytes and every canonical encoding travels as a STRING. Nothing that can poison
the document is ever placed in it.

## Why AST lifting, and why `app.sync_service` is not imported

`import app.sync_service` raises `ModuleNotFoundError: No module named
'sqlalchemy'`, and so do `app.idempotency` and `app.ownership`. Only
`app.schemas`, `app.rejection_codes` and `app.assembly_contract` import cleanly
in an environment that has pydantic but not SQLAlchemy.

So the functions are lifted: `ast.parse`, `ast.get_source_segment` for each
named top-level definition, `exec`. Each lifted chunk is the reference's own
source text, compiled with `from __future__ import annotations` prepended
because the reference modules rely on it and an isolated chunk does not carry
it -- without it `db: AsyncSession` is evaluated at def time and the chunk dies
with `NameError: name 'AsyncSession' is not defined`.

What is lifted, and from where:

  app/sync_service.py    InvalidMutation, decode_payload, pull_page_accepts,
                         _outcome_event, _mutation_row, _record_mutation,
                         `record = SyncRecord(...)` and `result = SyncBatch(...)`
                         from pull_since -- and the steps 1-3 slice of
                         _apply_push_once (below)
  app/idempotency.py     request_fingerprint
  app/ownership.py       EffectiveScope, mutation_sort_key, ENTITY_ORDER
  app/observability.py   correlation_ref
  app/schemas.py         SyncBatch, SyncRecord -- IMPORTED AND EXECUTED, not
                         lifted, because `model_dump_json()` is the thing the
                         page-wire measurement is about

What is faked, and why it is sound:

  * `select(...)`, `db.scalar`, `db.get`, `db.add` -- a FakeDB keyed on
    (model __name__, primary key). The four lookups inside steps 1-3 are the
    `Organization` row, the `SyncMutation` row, the `SyncMutationFingerprint`
    row and the `SyncEntity` row, and each is expressed in the reference as a
    `Model.column == value` comparison, so a `_Column.__eq__` that returns a
    clause object captures the key without anybody writing the key down.
  * `_record_mutation` writes go into the SAME store the reads come from, so a
    second record in a batch sees what the first one spent. That is the only
    way a multi-record batch is a sound case here; see `worlds` below.

## The steps 1-3 slice

`Reference._build_admission_slice()` rebuilds `_apply_push_once` as an
`ast.AsyncFunctionDef` whose `for`-body is the statements BEFORE the step-4
`try:` -- so steps 1, 2 and 3, and nothing else. The step-4 outcome is
`Payload.decode/1`'s, which the BEAM side runs itself; the oracle reports it
separately as `step4` on each row.

The slice returns `(outcomes, outcome_events)`, NOT `outcome_events` alone.
`outcomes` is the dict keyed on `original_index` and `outcome_events` carries no
index at all, so a return of events alone cannot map an outcome back to the
record that produced it. Returning both is the difference between a harness that
compares per-record decisions and a harness that compares two unordered lists.

`ast.unparse` rewrites some literals -- `sync_service.py`'s `"sync"` comes back
as `'sync'` -- so the executed text is semantically the reference, not
byte-for-byte. The drift guard in drift_test.exs is what checks the text; this
is what executes the behaviour.

## `worlds`, one per record, not one per batch

A multi-record batch is sequential: record 1 can write a `SyncMutation` row that
record 2 then reads. A harness that emitted a single pre-batch world and replayed
it against every record would disagree with the reference by construction, and
the disagreement would look like a parity bug. So the oracle emits one world per
input index, snapshotted immediately before that record runs. Elixir feeds
record N the world record N actually saw.

## The page driver loop

The fit PREDICATE (`pull_page_accepts`), the record CONSTRUCTION and the batch
CONSTRUCTION are all lifted, verbatim, from the reference. The eight-line loop
that feeds them is harness code, and it is eight lines rather than the
reference's loop because the reference's loop is inseparable from
`load_page_context` / `resolve_visible` (wave 4b, not built on the BEAM side
yet). Every change in the corpus is visible and has its entity, so those
branches never differ.

The payload_json values in the page corpus are deliberately float-free. Value
space parity -- including float rendering -- is the payload block's job, and
mixing the two would make a float-rendering difference look like a page-sizing
difference.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import hashlib
import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ORGANIZATION_ID = "org"

# ---------------------------------------------------------------------------
# lifting
# ---------------------------------------------------------------------------

FUTURE = "from __future__ import annotations\n"


def _definition_name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


def _segment(source: str, node: ast.stmt) -> str:
    """The node's own source text, decorators included and in order."""
    text = ast.get_source_segment(source, node)
    if text is None:
        raise SystemExit("parity_oracle: cannot read source for a definition")
    for decorator in reversed(getattr(node, "decorator_list", [])):
        head = ast.get_source_segment(source, decorator)
        if head is not None:
            # `get_source_segment` on a decorator node returns the expression
            # WITHOUT the `@`, so `EffectiveScope` would come back as a plain
            # class with no `__init__` and `from_principal` would blow up.
            text = f"@{head}\n{text}"
    return text


def collect(path: Path, names: list[str]) -> dict[str, str]:
    """The reference's own source text for each named top-level definition."""
    source = path.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for node in tree_for(path).body:
        name = _definition_name(node)
        if name in names and name not in found:
            found[name] = _segment(source, node)
    missing = sorted(name for name in names if name not in found)
    if missing:
        raise SystemExit(
            f"parity_oracle: {path} defines none of {', '.join(missing)}"
            " -- the reference has been renamed or restructured, and every case"
            " below would be a guess."
        )
    return found


def lift(path: Path, names: list[str], ns: dict) -> None:
    """Execute the named definitions from `path` into the namespace `ns`."""
    segments = collect(path, names)
    unit = FUTURE + "\n\n".join(segments[name] for name in names)
    exec(compile(unit, str(path), "exec"), ns)  # noqa: S102 -- that is the point


# Parsing `app/sync_service.py` once per statement rather than once per file is
# the difference between a page suite that runs in seconds and one that runs in
# a minute: the page harness lifts three statements per candidate record, and
# `ast.parse` over a 588-line module 5000 times is pure waste.
_TREES: dict[str, ast.Module] = {}


def tree_for(path: Path) -> ast.Module:
    key = str(path)
    tree = _TREES.get(key)
    if tree is None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=key)
        _TREES[key] = tree
    return tree


def _root_callee(node) -> str | None:
    """The dotted name of the INNERMOST call a statement makes, or None.

    `payload = json.dumps(entity.payload_json or {}, ...).encode()` is an
    `Assign` whose value's outermost call is `(...).encode` -- an Attribute whose
    receiver is itself a Call. The name worth matching is `json.dumps`, two
    levels down. `SyncRecord` and `SyncBatch` are the shallow cases, and all
    four statements have to be told apart from same-named assignments elsewhere
    in the file (`payload` is assigned twice in `_apply_push_once`), so the
    match has to be exact rather than merely unique on the target name.
    """
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Call)
    ):
        node = node.func.value
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def lift_statement(path: Path, predicate, ns: dict) -> None:
    """Execute the single statement `predicate` selects, into `ns`.

    The search is a whole-tree walk, not module level: both statements the page
    harness lifts live inside `pull_since`, and the record construction lives
    two levels deeper still, inside its `for` body.

    The statement reads the names it needs (`change`, `entity`, `records`,
    `max_seq`, `json`, `base64`, `SyncRecord`, `SyncBatch`) out of `ns`, so the
    caller binds them first.
    """
    source = path.read_text(encoding="utf-8")
    matches = [node for node in ast.walk(tree_for(path)) if predicate(node)]
    if len(matches) != 1:
        raise SystemExit(
            f"parity_oracle: expected exactly one matching statement in {path},"
            f" found {len(matches)}"
        )
    unit = FUTURE + _segment(source, matches[0])
    exec(compile(unit, str(path), "exec"), ns)  # noqa: S102


def module_constant(path: Path, name: str):
    """A module-level constant, by `literal_eval` -- never by transcription."""
    for node in tree_for(path).body:
        if isinstance(node, ast.AnnAssign) and _definition_name(node) == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and _definition_name(node) == name:
            return ast.literal_eval(node.value)
    raise SystemExit(f"parity_oracle: {path} does not assign {name}")


# ---------------------------------------------------------------------------
# fakes for the four lookups inside steps 1-3
# ---------------------------------------------------------------------------


class _Clause:
    __slots__ = ("column", "value")

    def __init__(self, column: str, value) -> None:
        self.column = column
        self.value = value


class _Column:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other):
        return _Clause(self.name, other)

    def __hash__(self) -> int:
        return hash(("column", self.name))


class _ModelMeta(type):
    def __getattr__(cls, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        column = _Column(name)
        setattr(cls, name, column)
        return column


class _Model(metaclass=_ModelMeta):
    """An ORM row, reduced to the attributes the sliced code actually reads."""

    pk_columns: tuple[str, ...] = ()

    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)

    @classmethod
    def pk(cls, instance) -> tuple:
        return tuple(instance.__dict__[column] for column in cls.pk_columns)


def _model(name: str, pk_columns: tuple[str, ...]):
    return _ModelMeta(
        name,
        (_Model,),
        {"pk_columns": pk_columns, "__module__": "parity_oracle.fakes"},
    )


Organization = _model("Organization", ("id",))
SyncMutation = _model("SyncMutation", ("organization_id", "client_mutation_id"))
SyncMutationFingerprint = _model(
    "SyncMutationFingerprint", ("organization_id", "client_mutation_id")
)
SyncEntity = _model("SyncEntity", ("organization_id", "entity_type", "entity_id"))
SyncChangeLog = _model("SyncChangeLog", ("organization_id", "sequence"))


class _Query:
    __slots__ = ("model", "clauses")

    def __init__(self, model) -> None:
        self.model = model
        self.clauses: list[_Clause] = []

    def where(self, *clauses):
        for clause in clauses:
            if not isinstance(clause, _Clause):
                raise SystemExit(
                    f"parity_oracle: unexpected WHERE term {clause!r} -- the sliced"
                    " reference changed shape and the FakeDB key is no longer"
                    " derivable."
                )
            self.clauses.append(clause)
        return self

    def with_for_update(self):
        return self

    def key(self):
        values = [clause.value for clause in self.clauses]
        return values[0] if len(values) == 1 else tuple(values)


def select(*models, **kwargs):
    return _Query(models[0])


class FakeDB:
    """A store keyed on (model name, primary key), reads and writes alike.

    `_record_mutation` writes through `db.add` inside steps 1-3 (the
    stale-revision branch), so a store that did not read its own writes would
    give a second record in a batch a different verdict than the reference
    gives it.
    """

    def __init__(self, rows: dict) -> None:
        self.rows = dict(rows)
        self.added: list = []

    async def scalar(self, query: _Query, *args, **kwargs):
        return self.rows.get((query.model.__name__, query.key()))

    async def get(self, model, pk):
        return self.rows.get((model.__name__, tuple(pk)))

    def add(self, instance) -> None:
        self.rows[(type(instance).__name__, type(instance).pk(instance))] = instance
        self.added.append(instance)


class Principal:
    """Only the attributes `EffectiveScope.from_principal` and the slice read."""

    def __init__(self, organization_id: str) -> None:
        self.organization_id = organization_id
        self.user_id = "u1"
        self.membership_id = "mem1"
        self.session_id = "s1"
        self.authorization_revision = 3
        self.capabilities = {"sync"}
        self.all_customers = True
        self.all_projects = True
        self.customer_ids: set[str] = set()
        self.project_ids: set[str] = set()


# ---------------------------------------------------------------------------
# the reference, lifted
# ---------------------------------------------------------------------------


class Reference:
    """Every lifted name, in one namespace, resolved once per process."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.sync_service = repo / "app" / "sync_service.py"
        self.idempotency = repo / "app" / "idempotency.py"
        self.ownership = repo / "app" / "ownership.py"
        self.observability = repo / "app" / "observability.py"
        self.schemas = repo / "app" / "schemas.py"
        self.rejection_codes = repo / "app" / "rejection_codes.py"

        for path in (self.sync_service, self.schemas, self.rejection_codes):
            if not path.is_file():
                raise SystemExit(f"parity_oracle: {path} is not a file")

        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))

        # app.schemas is the page-wire oracle itself, so it is imported and run
        # rather than lifted. Same for app.rejection_codes.
        from app.rejection_codes import rejection_code
        from app.schemas import (
            MAX_SYNC_IDENTIFIER_LENGTH,
            MAX_SYNC_RECORDS,
            MAX_SYNC_V1_BATCH_PAYLOAD_BYTES,
            SyncBatch,
            SyncRecord,
        )

        self.MAX_SYNC_RECORDS = MAX_SYNC_RECORDS
        self.MAX_SYNC_IDENTIFIER_LENGTH = MAX_SYNC_IDENTIFIER_LENGTH
        self.MAX_SYNC_BATCH_PAYLOAD_BYTES = MAX_SYNC_V1_BATCH_PAYLOAD_BYTES
        self.SyncBatch = SyncBatch
        self.SyncRecord = SyncRecord
        self.rejection_code = rejection_code

        common: dict = {
            "SyncBatch": SyncBatch,
            "SyncRecord": SyncRecord,
            "SyncMutationFingerprint": SyncMutationFingerprint,
            "dataclass": dataclass,
            "field": field,
            "set": set,
            "hashlib": hashlib,
            "json": json,
            "base64": base64,
            # `_mutation_row` CONSTRUCTS a SyncMutation; it does not compare a
            # column, so the fake model has to be visible to the lifted chunk,
            # not only to the slice.
            "Organization": Organization,
            "SyncMutation": SyncMutation,
            "SyncEntity": SyncEntity,
            "SyncChangeLog": SyncChangeLog,
            "MAX_SYNC_RECORDS": MAX_SYNC_RECORDS,
            "MAX_SYNC_V1_BATCH_PAYLOAD_BYTES": MAX_SYNC_V1_BATCH_PAYLOAD_BYTES,
            # `pull_page_accepts` reads the response ceiling out of its own
            # module, and the module is not importable, so it comes from the
            # source by `literal_eval` rather than by transcription.
            "MAX_SYNC_PULL_RESPONSE_BYTES": module_constant(
                self.sync_service, "MAX_SYNC_PULL_RESPONSE_BYTES"
            ),
        }
        lift(self.sync_service, ["InvalidMutation"], common)
        lift(self.sync_service, ["decode_payload", "pull_page_accepts"], common)
        lift(
            self.sync_service,
            ["_outcome_event", "_mutation_row", "_record_mutation"],
            common,
        )
        lift(self.idempotency, ["request_fingerprint"], common)
        lift(self.observability, ["correlation_ref"], common)
        lift(self.ownership, ["ENTITY_ORDER", "mutation_sort_key"], common)
        lift(self.ownership, ["EffectiveScope"], common)

        self.common = common
        self.InvalidMutation = common["InvalidMutation"]
        self.decode_payload = common["decode_payload"]
        self.pull_page_accepts = common["pull_page_accepts"]
        self._outcome_event = common["_outcome_event"]
        self._mutation_row = common["_mutation_row"]
        self._record_mutation = common["_record_mutation"]
        self.request_fingerprint = common["request_fingerprint"]
        self.correlation_ref = common["correlation_ref"]
        self.ENTITY_ORDER = dict(common["ENTITY_ORDER"])
        self.mutation_sort_key = common["mutation_sort_key"]
        self.EffectiveScope = common["EffectiveScope"]

        self.slice = self._build_admission_slice()

    # -- the steps 1-3 slice -------------------------------------------------

    def _build_admission_slice(self):
        tree = tree_for(self.sync_service)
        function = None
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_apply_push_once":
                function = node
                break
        if function is None:
            raise SystemExit(
                f"parity_oracle: _apply_push_once is gone from {self.sync_service}"
            )

        loop_index = next(
            (i for i, node in enumerate(function.body) if isinstance(node, ast.For)),
            None,
        )
        if loop_index is None:
            raise SystemExit("parity_oracle: _apply_push_once has no for-loop")
        loop = function.body[loop_index]

        cut = next(
            (i for i, node in enumerate(loop.body) if isinstance(node, ast.Try)),
            None,
        )
        if cut is None:
            raise SystemExit(
                "parity_oracle: the steps 1-3 slice boundary -- the step-4 `try:` --"
                " is no longer present in the loop body. The harness would"
                " silently start measuring a different function."
            )

        new_loop = ast.For(
            target=loop.target,
            iter=loop.iter,
            body=loop.body[:cut],
            orelse=[],
            type_comment=None,
        )
        ast.copy_location(new_loop, loop)

        # Both, not just the events: see the module docstring.
        result = ast.Return(
            value=ast.Tuple(
                elts=[ast.Name(id="outcomes"), ast.Name(id="outcome_events")],
                ctx=ast.Load(),
            )
        )
        new_body = list(function.body[:loop_index]) + [new_loop, result]
        for node in new_body:
            ast.fix_missing_locations(node)

        sliced = ast.AsyncFunctionDef(
            name="_apply_push_steps_1_to_3",
            args=function.args,
            body=new_body,
            decorator_list=[],
            returns=None,
            type_comment=None,
        )
        ast.fix_missing_locations(sliced)

        ns = {
            "select": select,
            "SyncBatch": self.SyncBatch,
            "SyncRecord": self.SyncRecord,
            "Organization": Organization,
            "SyncMutation": SyncMutation,
            "SyncMutationFingerprint": SyncMutationFingerprint,
            "SyncEntity": SyncEntity,
            "EffectiveScope": self.EffectiveScope,
            "mutation_sort_key": self.mutation_sort_key,
            "correlation_ref": self.correlation_ref,
            "decode_payload": self.decode_payload,
            "request_fingerprint": self.request_fingerprint,
            "_outcome_event": self._outcome_event,
            "_mutation_row": self._mutation_row,
            "_record_mutation": self._record_mutation,
            "InvalidMutation": self.InvalidMutation,
        }
        unit = FUTURE + ast.unparse(ast.Module(body=[sliced], type_ignores=[]))
        exec(compile(unit, str(self.sync_service), "exec"), ns)  # noqa: S102
        return ns["_apply_push_steps_1_to_3"]

    # -- running the slice ---------------------------------------------------

    def run_steps_1_to_3(self, records, rows):
        """Return `(worlds, outcomes)` for one batch.

        `worlds[i]` is the store as record i saw it. That is the world the BEAM
        side has to feed `Admission.decide/2`, and it is NOT the pre-batch
        store: step 3's rejection path writes a `SyncMutation` row through
        `_record_mutation`, so record 2 of a batch sees what record 1 spent. A
        harness that handed both records the same world would disagree with the
        reference by construction, and the disagreement would look like a parity
        bug rather than a bug in the harness.
        """
        db = FakeDB(rows)
        batch = self.SyncBatch(deviceID="dev1", cursor=None, records=records)
        principal = Principal(ORGANIZATION_ID)

        outcomes, events = asyncio.run(self.slice(db, principal, batch))

        # Events are appended in mutation_sort_key order, not batch order, AND a
        # record that passed steps 1-3 appends none -- it fell off the end of the
        # sliced body into step 4. So `events` cannot be zipped against the
        # sorted records positionally: doing that attributes record 2's
        # `stale_revision` to record 1 whenever record 1 also passes. (It did,
        # once, and it read as a parity bug.)
        #
        # `outcomes` is the authoritative index -> decision map: every path that
        # appends an event also assigns `outcomes[original_index]`, and nothing
        # else does. So its keys, in mutation_sort_key order, are exactly the
        # records the events belong to.
        ordered = sorted(
            enumerate(records),
            key=lambda pair: self.mutation_sort_key(pair[1].entityType, pair[0]),
        )
        emitters = [
            original_index for original_index, _ in ordered if original_index in outcomes
        ]
        if len(emitters) != len(events):
            raise SystemExit(
                f"parity_oracle: {len(events)} events but {len(emitters)} records"
                " recorded an outcome. The steps 1-3 branches no longer set"
                " `outcomes[original_index]` on every path that appends an"
                " event, so the event-to-record mapping is no longer derivable."
            )

        by_index: list[dict | None] = [None] * len(records)
        for original_index, event in zip(emitters, events):
            if outcomes[original_index] != (event["resultStatus"] == "accepted"):
                raise SystemExit(
                    f"parity_oracle: record {original_index} has outcomes="
                    f"{outcomes[original_index]} but an event claiming"
                    f" {event['resultStatus']}"
                )
            by_index[original_index] = {
                "status": event["resultStatus"],
                "reason": event["reason"],
                "result_revision": event["resultServerRevision"],
            }

        # The per-record stores. Inside the slice, `_record_mutation` is the only
        # writer and only the stale-revision branch calls it -- steps 1 and 2
        # both `continue` before it, and the accepted path is past the slice
        # boundary. So a stale_revision rejection spends exactly the two rows
        # `_record_mutation` adds, in event order, and replaying `db.added` two
        # rows at a time over those events reconstructs what each record had to
        # see. The row count is checked rather than trusted, so a reference
        # change that makes an earlier branch write fails loudly here instead of
        # quietly shifting one record's world onto its neighbour's.
        writers = [index for index in emitters if by_index[index]["reason"] == "stale_revision"]
        if len(db.added) != 2 * len(writers):
            raise SystemExit(
                f"parity_oracle: the slice added {len(db.added)} rows for"
                f" {len(writers)} stale_revision rejections. Either another"
                " steps 1-3 branch writes now, or `_record_mutation` no longer"
                " adds two rows; the per-record worlds below would be wrong."
            )

        replayed = dict(rows)
        worlds: list[dict] = []
        seen = 0
        for index, record in enumerate(records):
            worlds.append(self._world_from_rows(replayed, record))
            if index in writers:
                for added in db.added[2 * seen : 2 * seen + 2]:
                    replayed[(type(added).__name__, type(added).pk(added))] = added
                seen += 1

        if replayed != db.rows:
            raise SystemExit(
                "parity_oracle: replaying `db.added` does not reproduce the"
                " final store, so the per-record worlds below would be wrong."
            )

        return worlds, by_index

    def _world_from_rows(self, rows: dict, record) -> dict:
        prior = rows.get(("SyncMutation", (ORGANIZATION_ID, record.clientMutationID)))
        fingerprint = rows.get(
            ("SyncMutationFingerprint", (ORGANIZATION_ID, record.clientMutationID))
        )
        current = rows.get(
            (
                "SyncEntity",
                (ORGANIZATION_ID, record.entityType, record.entityID),
            )
        )
        return {
            "prior": None
            if prior is None
            else {
                "entity_type": prior.entity_type,
                "entity_id": prior.entity_id,
                "base_server_revision": prior.base_server_revision,
                "result_status": prior.result_status,
                "result_server_revision": prior.result_server_revision,
            },
            "prior_fingerprint": None
            if fingerprint is None
            else fingerprint.request_fingerprint,
            "entity_exists": current is not None,
            "current_server_revision": None if current is None else current.server_revision,
        }


# ---------------------------------------------------------------------------
# corpus construction
# ---------------------------------------------------------------------------

COLLIDING_FINGERPRINT = "f" * 64

ENTITY_TYPES = [
    "customer",
    "project",
    "project_sector",
    "item",
    "item_audit_event",
    "measurement",
    "evidence",
    "customer_requirement",
    "configuration",
    "configuration_version",
    "blocker",
    "quotation",
    "delivery_execution",
]


def record(**overrides) -> dict:
    """One record, in `SyncRecord`'s own field names, ready for pydantic."""
    base = {
        "id": "r1",
        "entityType": "customer",
        "entityID": "c1",
        "updatedAt": "2026-01-01T00:00:00Z",
        "payload": b"{}",
        "baseServerRevision": None,
        "serverRevision": None,
        "clientMutationID": "m1",
        "deletedAt": None,
    }
    base.update(overrides)
    return base


def wire_form(reference: Reference, attrs: dict) -> dict:
    """The record as Pydantic will serialize it.

    `updatedAt` and `deletedAt` are the only fields the wire form can change:
    they are parsed into `datetime` on the way in and rendered back out on the
    way through. Emitting the ORIGINAL string would make the Elixir side size a
    record the reference never serialized.
    """
    model = reference.SyncRecord(**attrs)
    dumped = json.loads(model.model_dump_json())
    emitted = dict(attrs)
    emitted["updatedAt"] = dumped["updatedAt"]
    emitted["deletedAt"] = dumped["deletedAt"]
    return emitted


def emit_record(attrs: dict) -> dict:
    """A record as DATA. `payload` travels base64: `json.dumps` raises
    TypeError on bytes, and text transport would let `b'\\xff\\xfe'` round-trip
    differently on each estate -- a false pass, which is worse than a false
    failure."""
    return {
        "id": attrs["id"],
        "entity_type": attrs["entityType"],
        "entity_id": attrs["entityID"],
        "updated_at": attrs["updatedAt"],
        "payload_b64": base64.b64encode(attrs["payload"]).decode("ascii"),
        "base_server_revision": attrs["baseServerRevision"],
        "server_revision": attrs["serverRevision"],
        "client_mutation_id": attrs["clientMutationID"],
        "deleted_at": attrs["deletedAt"],
    }


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------


def empty_world() -> dict:
    return {
        "prior": None,
        "prior_fingerprint": None,
        "entity_exists": False,
        "current_server_revision": None,
        "entity_type": None,
        "entity_id": None,
        "prior_client_mutation_id": None,
        "prior_fingerprint_client_mutation_id": None,
    }


def rows_for(reference: Reference, world: dict, attrs_list: list[dict]) -> dict:
    rows: dict = {
        ("Organization", ORGANIZATION_ID): Organization(id=ORGANIZATION_ID),
    }
    for attrs in attrs_list:
        cmid = attrs["clientMutationID"]
        model = reference.SyncRecord(**attrs)
        if world["prior"] is not None and world["prior_client_mutation_id"] == cmid:
            rows[("SyncMutation", (ORGANIZATION_ID, cmid))] = SyncMutation(
                organization_id=ORGANIZATION_ID,
                client_mutation_id=cmid,
                entity_type=world["prior"]["entity_type"],
                entity_id=world["prior"]["entity_id"],
                base_server_revision=world["prior"]["base_server_revision"],
                result_status=world["prior"]["result_status"],
                result_server_revision=world["prior"]["result_server_revision"],
            )
        if world["prior_fingerprint"] is not None and world["prior_fingerprint_client_mutation_id"] == cmid:
            value = world["prior_fingerprint"]
            if value == "SELF":
                value = reference.request_fingerprint(model)
            rows[("SyncMutationFingerprint", (ORGANIZATION_ID, cmid))] = (
                SyncMutationFingerprint(
                    organization_id=ORGANIZATION_ID,
                    client_mutation_id=cmid,
                    request_fingerprint=value,
                )
            )
        if world["entity_exists"] and (
            attrs["entityType"],
            attrs["entityID"],
        ) == (world["entity_type"], world["entity_id"]):
            rows[
                (
                    "SyncEntity",
                    (ORGANIZATION_ID, attrs["entityType"], attrs["entityID"]),
                )
            ] = SyncEntity(
                organization_id=ORGANIZATION_ID,
                entity_type=attrs["entityType"],
                entity_id=attrs["entityID"],
                server_revision=world["current_server_revision"],
            )
    return rows


def admission_specs() -> list[dict]:
    specs: list[dict] = []

    def add(name, attrs_list, *mutators):
        world = empty_world()
        for mutate in mutators:
            mutate(world)
        specs.append({"name": name, "records": list(attrs_list), "world": world})

    def prior(status, revision, entity_type="customer", entity_id="c1", base_revision=None):
        def apply(world):
            world["prior"] = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "base_server_revision": base_revision,
                "result_status": status,
                "result_server_revision": revision,
            }
            world["prior_client_mutation_id"] = "m1"

        return apply

    def fingerprint(value):
        def apply(world):
            world["prior_fingerprint"] = value
            world["prior_fingerprint_client_mutation_id"] = "m1"

        return apply

    def entity(revision, entity_type="customer", entity_id="c1"):
        def apply(world):
            world["entity_exists"] = True
            world["current_server_revision"] = revision
            world["entity_type"] = entity_type
            world["entity_id"] = entity_id

        return apply

    # -- step 1 --------------------------------------------------------------
    add("step1_client_mutation_id_absent", [record(clientMutationID=None)])
    add("step1_client_mutation_id_empty", [record(clientMutationID="")])

    def prior_on_empty(world):
        prior("accepted", 9)(world)
        world["prior_client_mutation_id"] = ""

    add("step1_beats_step2", [record(clientMutationID="")], prior_on_empty)

    # -- step 3, with no prior ------------------------------------------------
    add("step3_create_without_base_revision", [record()])
    add("step3_create_with_base_revision", [record(baseServerRevision=0)])
    add("step3_update_matching_revision", [record(baseServerRevision=5)], entity(5))
    add("step3_update_matching_revision_zero", [record(baseServerRevision=0)], entity(0))
    add("step3_update_stale_revision", [record(baseServerRevision=4)], entity(5))
    add("step3_update_stale_revision_ahead", [record(baseServerRevision=9)], entity(5))
    add(
        "step3_update_null_base_against_existing",
        [record(baseServerRevision=None)],
        entity(5),
    )
    add(
        "step3_stale_revision_alongside_missing_id",
        [record(clientMutationID=None, baseServerRevision=4)],
        entity(5),
    )

    # -- step 2, with a fingerprint row --------------------------------------
    add(
        "step2_replay_accepted_with_fingerprint",
        [record()],
        prior("accepted", 7),
        fingerprint("SELF"),
    )
    add(
        "step2_replay_rejected_with_fingerprint",
        [record()],
        prior("rejected", 7),
        fingerprint("SELF"),
    )
    add(
        "step2_collision_fingerprint_differs",
        [record()],
        prior("accepted", 7),
        fingerprint(COLLIDING_FINGERPRINT),
    )
    add(
        "step2_collision_fingerprint_differs_after_rejection",
        [record()],
        prior("rejected", None),
        fingerprint(COLLIDING_FINGERPRINT),
    )

    # -- step 2 with no fingerprint row: the weak identity -------------------
    add("step2_legacy_replay_accepted_no_fingerprint", [record()], prior("accepted", 7))
    add("step2_legacy_replay_rejected_no_fingerprint", [record()], prior("rejected", 7))
    add(
        "step2_legacy_collision_on_entity_type",
        [record()],
        prior("accepted", 7, entity_type="project"),
    )
    add(
        "step2_legacy_collision_on_entity_id",
        [record()],
        prior("accepted", 7, entity_id="c2"),
    )
    add(
        "step2_legacy_collision_on_base_revision",
        [record(baseServerRevision=3)],
        prior("accepted", 7, base_revision=4),
        entity(4),
    )
    add(
        "step2_legacy_replay_ignores_payload_and_timestamps",
        [
            record(
                payload=b'{"different":true}',
                updatedAt="2026-06-30T23:59:59.123456Z",
            )
        ],
        prior("accepted", 7),
    )

    # -- step 2 beats step 3 --------------------------------------------------
    add(
        "step2_beats_step3_stale",
        [record(baseServerRevision=4)],
        prior("accepted", 7),
        entity(5),
    )

    # -- step 4: the slice stops here, and says so ----------------------------
    for name, payload in [
        ("step4_object", b'{"a":1}'),
        ("step4_object_base64", base64.b64encode(b'{"a":1}')),
        ("step4_empty_object", b"{}"),
        ("step4_not_json", b"not json"),
        ("step4_array_base64", base64.b64encode(b"[1,2]")),
        ("step4_number_base64", base64.b64encode(b"42")),
        ("step4_empty_bytes", b""),
        ("step4_non_finite_number", b'{"a": NaN}'),
        ("step4_unpaired_surrogate_high", b'{"a": "\\ud800"}'),
        ("step4_unpaired_surrogate_low", b'{"a": "\\udc00\\ud800"}'),
        ("step4_overflow_to_infinity", b'{"a": 1e400}'),
    ]:
        add(name, [record(payload=payload, clientMutationID=f"m-{name}")])

    # -- identifier and timestamp edges ---------------------------------------
    long_id = "x" * 256
    add(
        "identifier_at_cap",
        [record(id=long_id, entityID=long_id, clientMutationID=long_id)],
    )
    add("identifier_non_ascii", [record(entityID="客户-é☃-\U0001f600")])
    add("identifier_control_characters", [record(entityID='a\nb\tc\x01d\\e"f')])
    add("tombstone_record", [record(deletedAt="2026-01-01T00:00:00Z")])
    add("timestamp_with_offset", [record(updatedAt="2026-01-01T12:34:56+02:00")])
    add("timestamp_with_microseconds", [record(updatedAt="2026-01-01T00:00:00.500000Z")])

    # -- multi-record batches -------------------------------------------------
    add(
        "batch_two_distinct_creates",
        [
            record(id="r1", clientMutationID="m1", entityID="c1"),
            record(id="r2", clientMutationID="m2", entityID="c2"),
        ],
    )
    add(
        "batch_two_reversed_entity_order",
        [
            record(id="r1", entityType="item", entityID="i1", clientMutationID="m1"),
            record(id="r2", entityType="customer", entityID="c1", clientMutationID="m2"),
        ],
    )
    add(
        "batch_same_id_twice_first_create",
        [
            record(id="r1", clientMutationID="dup", entityID="c1"),
            record(id="r2", clientMutationID="dup", entityID="c1"),
        ],
    )
    add(
        "batch_same_id_twice_after_stale_rejection",
        [
            record(id="r1", clientMutationID="dup", entityID="c1", baseServerRevision=4),
            record(id="r2", clientMutationID="dup", entityID="c1", baseServerRevision=4),
        ],
        entity(5),
    )
    add(
        "batch_three_with_a_missing_id",
        [
            record(id="r1", clientMutationID=None, entityID="c1"),
            record(id="r2", clientMutationID="m2", entityID="c2"),
            record(
                id="r3",
                entityType="item",
                entityID="i3",
                clientMutationID="m3",
                baseServerRevision=2,
            ),
        ],
        entity(1, entity_type="item", entity_id="i3"),
    )
    return specs


def admission_cases(reference: Reference) -> list[dict]:
    resolved = []
    for spec in admission_specs():
        attrs_list = [wire_form(reference, attrs) for attrs in spec["records"]]
        records = [reference.SyncRecord(**attrs) for attrs in attrs_list]
        worlds, outcomes = reference.run_steps_1_to_3(
            records, rows_for(reference, spec["world"], attrs_list)
        )
        step4 = []
        for model in records:
            try:
                value = reference.decode_payload(model)
            except reference.InvalidMutation:
                step4.append({"accepted": False, "narrowing": None, "canonical": None})
            else:
                step4.append(
                    {
                        "accepted": True,
                        "narrowing": narrowing_class(value),
                        "canonical": canonical(value),
                    }
                )
        resolved.append(
            {
                "name": spec["name"],
                "records": [emit_record(attrs) for attrs in attrs_list],
                "worlds": worlds,
                "outcomes": outcomes,
                "step4": step4,
            }
        )
    return resolved


def canonical(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def narrowing_class(value):
    """Which of the two documented BEAM narrowings a decoded value falls under.

    Derived from the VALUE the reference produced, by the reference's own
    semantics -- `json.loads` turns 1e400 into `inf`, and `"\\ud800"` into a
    lone surrogate -- never from what the BEAM side did. That is what lets the
    Elixir test assert an enumerated expected-divergence set instead of guessing
    it, and what stops a divergence from being "fixed" by relaxing the
    assertion.
    """
    nonfinite = False
    surrogate = False
    stack = [value]
    while stack:
        item = stack.pop()
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                nonfinite = True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str):
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in item):
                surrogate = True
    if nonfinite and surrogate:
        return "nonfinite+surrogate"
    if nonfinite:
        return "nonfinite"
    if surrogate:
        return "surrogate"
    return None


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


def payload_inputs() -> list[tuple[str, bytes]]:
    """The corpus. Curated for branch coverage, then a seeded random probe.

    Returns (name, raw payload bytes) pairs. Nothing here is a verdict; the
    reference decides.
    """
    cases: list[tuple[str, bytes]] = [
        ("raw_object", b'{"a":1}'),
        ("raw_object_spaced", b'{"a": 1, "b": [1, 2]}'),
        ("raw_object_empty", b"{}"),
        ("raw_object_unicode", '{"city":"Köln","emoji":"\U0001f600"}'.encode()),
        ("raw_object_bom", b"\xef\xbb\xbf" + b'{"a":1}'),
        ("raw_object_duplicate_keys", b'{"a":1,"a":2}'),
        ("raw_object_escapes", b'{"a":"\\u00e9\\n\\t\\"\\\\"}'),
        ("raw_object_deep_nesting", b'{"a":' * 40 + b"1" + b"}" * 40),
        ("raw_object_leading_zero", b'{"a":01}'),
        ("raw_object_trailing_comma", b'{"a":1,}'),
        ("raw_object_single_quotes", b"{'a':1}"),
        ("raw_object_big_integer", b'{"a":123456789012345678901234567890}'),
        ("raw_object_negative_exponent", b'{"a":1e-7}'),
        ("raw_object_null_and_bool", b'{"a":null,"b":true,"c":false}'),
        ("raw_object_nested_object", b'{"a":{"b":{"c":[1,{"d":2}]}}}'),
        ("raw_object_float", b'{"a":0.1}'),
        ("raw_object_float_integral", b'{"a":2.0}'),
        ("raw_object_float_exponent", b'{"a":1.5e10}'),
        ("raw_object_float_small_exponent", b'{"a":1e-7}'),
        ("raw_object_non_finite_nan", b'{"a": NaN}'),
        ("raw_object_non_finite_infinity", b'{"a": Infinity}'),
        ("raw_object_non_finite_negative_infinity", b'{"a": -Infinity}'),
        ("raw_object_overflow_to_infinity", b'{"a": 1e400}'),
        ("raw_object_overflow_to_negative_infinity", b'{"a": -1e400}'),
        ("raw_object_nested_nan", b'{"a":[1,{"b":NaN}]}'),
        ("raw_object_unpaired_surrogate_high", b'{"a": "\\ud800"}'),
        ("raw_object_unpaired_surrogate_low", b'{"a": "\\udc00\\ud800"}'),
        ("raw_object_unpaired_surrogate_after_text", b'{"a":"ok\\udfff"}'),
        ("raw_object_surrogate_pair", b'{"a": "\\ud83d\\ude00"}'),
        ("raw_object_surrogate_pair_and_lone", b'{"a": ["\\ud83d\\ude00", "\\ud800"]}'),
        ("raw_object_surrogate_key", b'{"\\ud800": 1}'),
        ("raw_array", b"[1, 2]"),
        ("raw_string", b'"a"'),
        ("raw_number", b"42"),
        ("raw_true", b"true"),
        ("raw_null", b"null"),
        ("raw_empty", b""),
        ("whitespace_only", b"   \n\t"),
        ("not_json", b"not json at all"),
        ("truncated_object", b'{"a":'),
        ("bare_brace", b"{"),
        ("object_then_garbage", b'{"a": 1}!'),
        ("two_documents", b'{"a":1}{"b":2}'),
        ("b64_object", base64.b64encode(b'{"a":1}')),
        ("b64_object_spaced", base64.b64encode(b'{"a": 1}')),
        ("b64_array", base64.b64encode(b"[1, 2]")),
        ("b64_string", base64.b64encode(b'"a"')),
        ("b64_number", base64.b64encode(b"42")),
        ("b64_truncated_object", base64.b64encode(b'{"a":')),
        ("b64_empty_document", base64.b64encode(b"")),
        ("b64_not_json", base64.b64encode(b"still not json")),
        ("b64_non_finite", base64.b64encode(b'{"a": NaN}')),
        ("b64_unpaired_surrogate", base64.b64encode(b'{"a": "\\ud800"}')),
        ("b64_unicode", base64.b64encode('{"city":"Köln"}'.encode())),
        ("b64_illegal_character", b"eyJhIjogMX0=!"),
        ("b64_illegal_character_middle", b"ey!JhIjogMX0="),
        ("b64_bad_padding", b"eyJhIjogMX0"),
        ("b64_padding_in_middle", b"ey=hIjogMX0="),
        ("b64_url_safe_alphabet", b"-_-_"),
        ("b64_empty", b""),
        ("b64_of_b64", base64.b64encode(base64.b64encode(b'{"a":1}'))),
        ("raw_base64_looking_object", b'{"a":"eyJhIjogMX0="}'),
        ("raw_base64_looking_array", b'["eyJhIjogMX0="]'),
        ("control_characters_in_string", b'{"a":"\\u0000\\u001f\\u007f"}'),
        ("raw_control_character", b'{"a":"\x01"}'),
        ("latin1_bytes", b'{"a":"\xe9"}'),
        ("utf8_two_byte_and_three_byte", '{"a":"é中"}'.encode()),
        ("four_byte_utf8", '{"a":"\U0001f600"}'.encode()),
        ("solidus_not_escaped", b'{"a":"a/b"}'),
    ]

    rng = random.Random(20261003)
    alphabet = "abcXYZ019 _-/.\t\né中\U0001f600"
    for index in range(100):
        value = _random_value(rng, alphabet, rng.randint(0, 3))
        raw = json.dumps(
            value, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode()
        if index % 3 == 1:
            raw = base64.b64encode(raw)
        cases.append((f"random_{index:03d}", raw))
    return cases


def _random_value(rng, alphabet, depth):
    roll = rng.random()
    if depth <= 0 or roll < 0.45:
        kind = rng.randint(0, 4)
        if kind == 0:
            return None
        if kind == 1:
            return rng.choice([True, False])
        if kind == 2:
            return rng.randint(-(10 ** rng.randint(1, 12)), 10 ** rng.randint(1, 12))
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
    if roll < 0.72:
        return [_random_value(rng, alphabet, depth - 1) for _ in range(rng.randint(0, 4))]
    return {
        "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6))): _random_value(
            rng, alphabet, depth - 1
        )
        for _ in range(rng.randint(0, 4))
    }


def payload_cases(reference: Reference) -> list[dict]:
    rows = []
    for name, raw in payload_inputs():
        model = reference.SyncRecord(**{**record(), "payload": raw})
        try:
            value = reference.decode_payload(model)
        except reference.InvalidMutation:
            rows.append(
                {
                    "name": name,
                    "input_b64": base64.b64encode(raw).decode("ascii"),
                    "accepted": False,
                    "narrowing": None,
                    "canonical": None,
                }
            )
        else:
            rows.append(
                {
                    "name": name,
                    "input_b64": base64.b64encode(raw).decode("ascii"),
                    "accepted": True,
                    "narrowing": narrowing_class(value),
                    # Always a STRING. json.dumps(NaN) is `{"a": NaN}` and
                    # VitrialSync.JSON.decode/1 rejects that by design, so a
                    # decoded value in this document would poison the whole
                    # load rather than one case.
                    "canonical": canonical(value),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


class _Change:
    __slots__ = ("sequence", "entity_type", "entity_id", "client_mutation_id")

    def __init__(self, change: dict) -> None:
        self.sequence = change["sequence"]
        self.entity_type = change["entity_type"]
        self.entity_id = change["entity_id"]
        self.client_mutation_id = change["client_mutation_id"]


class _Entity:
    __slots__ = ("payload_json", "updated_at", "server_revision", "deleted_at")

    def __init__(self, change: dict) -> None:
        self.payload_json = change["payload_json"]
        self.updated_at = datetime.fromisoformat(change["updated_at"].replace("Z", "+00:00"))
        self.server_revision = change["server_revision"]
        self.deleted_at = (
            None
            if change["deleted_at"] is None
            else datetime.fromisoformat(change["deleted_at"].replace("Z", "+00:00"))
        )


def build_page_record(reference: Reference, change: dict):
    """`record = SyncRecord(...)` lifted verbatim out of `pull_since`."""
    ns = {
        "change": _Change(change),
        "entity": _Entity(change),
        "json": json,
        "base64": base64,
        "SyncRecord": reference.SyncRecord,
    }
    for target, callee in (
        ("payload", "json.dumps"),
        ("encoded_payload", "base64.b64encode"),
        ("record", "SyncRecord"),
    ):
        lift_statement(
            reference.sync_service,
            lambda node, target=target, callee=callee: isinstance(node, ast.Assign)
            and _definition_name(node) == target
            and _root_callee(node.value) == callee,
            ns,
        )
    record_model = ns["record"]
    if change.get("wide_id"):
        # `pull_since` never does this; `SyncRecord.id` allows 256 characters,
        # so `pull_page_accepts` accepts it. See `response_ceiling` above.
        record_model.id = "w" * 256
    if "raw_payload_hex" in change:
        # app/schemas.py:76 types `payload` as `bytes` with no UTF-8 check, so
        # a non-UTF-8 payload VALIDATES and only blows up at serialization
        # time. There is no way to reach that through `_Entity.payload_json`,
        # because the construction path base64-encodes JSON, which is always
        # ASCII. Assigned after construction, which is what "the record
        # validated" means.
        record_model.payload = bytes.fromhex(change["raw_payload_hex"])
    return record_model


def build_page_batch(reference: Reference, records, max_seq):
    """`result = SyncBatch(...)` lifted verbatim out of `pull_since`."""
    ns = {
        "records": list(records),
        "max_seq": max_seq,
        "SyncBatch": reference.SyncBatch,
    }
    lift_statement(
        reference.sync_service,
        lambda node: isinstance(node, ast.Assign)
        and _definition_name(node) == "result"
        and _root_callee(node.value) == "SyncBatch",
        ns,
    )
    return ns["result"]


def emit_page_record(model) -> dict:
    """One accepted `SyncRecord`, on the same shape `emit_record/1` produces.

    Only the cases whose records `Page.assemble/2` cannot rebuild -- the
    `wide_id` ones -- carry these, because for every other case the BEAM side
    regenerates the records itself through `Page.to_record/1` and comparing the
    regenerated ones is the point.
    """
    wire = json.loads(model.model_dump_json())
    return {
        "id": wire["id"],
        "entity_type": wire["entityType"],
        "entity_id": wire["entityID"],
        "updated_at": wire["updatedAt"],
        "payload_b64": base64.b64encode(model.payload).decode("ascii"),
        "base_server_revision": wire["baseServerRevision"],
        "server_revision": wire["serverRevision"],
        "client_mutation_id": wire["clientMutationID"],
        "deleted_at": wire["deletedAt"],
    }


def page_measure(reference: Reference, cursor_sequence: int, changes: list[dict]) -> dict:
    """Drive the lifted predicate over `changes`, the way `pull_since` does.

    Eight lines of harness code around two lifted functions and two lifted
    statements. The reference's own loop is not lifted because it is inseparable
    from `load_page_context` / `resolve_visible` (wave 4b).
    """
    accepted = []
    max_seq = cursor_sequence
    truncated = False
    page_error: str | None = None
    for change in changes:
        candidate = build_page_record(reference, change)
        try:
            fits = reference.pull_page_accepts(accepted, candidate, change["sequence"])
        except Exception as exc:
            # Not a fit decision -- a crash. `pull_page_accepts` calls
            # `model_dump_json()` at sync_service.py:84, and `SyncRecord.payload`
            # is typed `bytes` with no UTF-8 check, so a schema-valid record
            # carrying non-UTF-8 payload bytes raises here and escapes the pull
            # loop as an unhandled 500. The BEAM side cannot express the
            # acceptance half of this at all, which is why it is reported
            # separately from `batch_error`.
            page_error = f"{type(exc).__name__}: {exc}"
            break
        if not fits:
            truncated = True
            break
        accepted.append(candidate)
        max_seq = max(max_seq, change["sequence"])
    batch_error: str | None = None
    batch_bytes: int | None = None
    if page_error is None:
        batch = build_page_batch(reference, accepted, max_seq)
        try:
            batch_bytes = len(batch.model_dump_json().encode("utf-8"))
        except Exception as exc:
            batch_error = f"{type(exc).__name__}: {exc}"
    return {
        "included": len(accepted),
        "max_sequence": max_seq,
        "truncated": truncated,
        "batch_bytes": batch_bytes,
        "batch_error": batch_error,
        "page_error": page_error,
        "accepted_records": (
            [emit_page_record(model) for model in accepted]
            if any(item.get("wide_id") for item in changes)
            else None
        ),
    }


def change(sequence, **overrides) -> dict:
    base = {
        "sequence": sequence,
        "entity_type": "customer",
        "entity_id": f"c{sequence}",
        "payload_json": {"n": sequence},
        "server_revision": sequence,
        "client_mutation_id": f"m{sequence}",
        "updated_at": "2026-01-01T00:00:00Z",
        "deleted_at": None,
    }
    base.update(overrides)
    return base


_WIRE_TIMESTAMPS: dict[tuple[str, str | None], tuple[str, str | None]] = {}


def wire_timestamps(reference: Reference, updated_at: str, deleted_at: str | None):
    """The form Pydantic will serialize a pair of timestamps into.

    Probed on a SMALL record rather than on the record under test: datetime
    serialization does not depend on the other fields, and probing the real
    record means serializing three megabytes of payload for every candidate in
    a page that is deliberately near the ceiling -- which is how a seven-second
    corpus turned into a half-minute one.

    `Page.Candidate.updated_at` is carried as a string and used verbatim by
    `record_byte_size/1`, so it has to be the wire form, not the form that went
    into pydantic.
    """
    key = (updated_at, deleted_at)
    cached = _WIRE_TIMESTAMPS.get(key)
    if cached is None:
        probe = reference.SyncRecord(
            **{**record(), "updatedAt": updated_at, "deletedAt": deleted_at}
        )
        wire = json.loads(probe.model_dump_json())
        cached = (wire["updatedAt"], wire["deletedAt"])
        _WIRE_TIMESTAMPS[key] = cached
    return cached


def normalize_timestamps(reference: Reference, changes: list[dict]) -> list[dict]:
    normalized = []
    for item in changes:
        copy = dict(item)
        copy["updated_at"], copy["deleted_at"] = wire_timestamps(
            reference, item["updated_at"], item["deleted_at"]
        )
        normalized.append(copy)
    return normalized


def page_specs(reference: Reference) -> list[dict]:
    specs: list[dict] = []

    def add(name, cursor_sequence, changes):
        specs.append(
            {
                "name": name,
                "cursor_sequence": cursor_sequence,
                "changes": normalize_timestamps(reference, changes),
            }
        )

    add("empty_page", 0, [])
    add("one_record", 0, [change(1)])
    add("two_records", 0, [change(1), change(2)])
    add("ten_records", 0, [change(n) for n in range(1, 11)])
    add("cursor_int64_max", 2**63 - 1, [change(1)])
    add("cursor_at_int64_max_one_change", 2**63 - 1, [change(2**63 - 1)])
    add(
        "non_ascii_identifiers",
        0,
        [
            change(1, entity_id="Köln"),
            change(2, entity_id="中文字段"),
            change(3, entity_id="\U0001f600\U0001f601"),
            change(4, entity_id="mixed-\U0001f600-漢字-ok"),
        ],
    )
    add(
        "control_characters_in_identifiers",
        0,
        [
            change(1, entity_id="a\nb"),
            change(2, entity_id="a\tb"),
            change(3, entity_id="a\x01b"),
            change(4, entity_id='quote"and\\slash'),
            change(5, entity_id="del\x7fchar"),
            change(6, entity_id="solidus/is/not/escaped"),
        ],
    )
    add(
        "null_and_absent_fields",
        0,
        [
            change(1, client_mutation_id=None, deleted_at=None),
            change(2, client_mutation_id=None, deleted_at="2026-01-02T03:04:05Z"),
        ],
    )
    add(
        "tombstones",
        0,
        [
            change(1, deleted_at="2026-01-02T03:04:05Z"),
            change(2, deleted_at="2026-01-02T03:04:05.678901Z"),
        ],
    )
    add("revision_at_int64_max", 0, [change(1, server_revision=2**63 - 1)])
    add(
        "identifier_at_cap",
        0,
        [change(1, entity_id="y" * reference.MAX_SYNC_IDENTIFIER_LENGTH)],
    )
    add(
        "nested_payload_shapes",
        0,
        [
            change(1, payload_json={}),
            change(2, payload_json={"a": [1, 2, {"b": None}]}),
            change(3, payload_json={"z": 1, "a": 2, "m": "x"}),
            change(4, payload_json={"unicode": "héllo\U0001f600漢字"}),
            change(5, payload_json={"control": "a\nb\tc\"d\\e"}),
        ],
    )

    # -- the three ceilings --------------------------------------------------
    # `payload_json` is canonical JSON of the given length; base64 of it is the
    # record's `payload`, and `len(record.payload)` -- the base64 length -- is
    # what the aggregate ceiling counts. `{"blob":"..."}` is 11 characters of
    # envelope, so the repeat count is chosen to land the base64 length exactly
    # on a multiple of four.
    add(
        "payload_ceiling",
        0,
        [change(n, payload_json={"blob": "z" * 787_492}) for n in (1, 2)],
    )
    add(
        "count_ceiling",
        0,
        [change(n) for n in range(1, reference.MAX_SYNC_RECORDS + 2)],
    )

    # The response ceiling, and the only way to reach it.
    #
    # MAX_SYNC_PULL_RESPONSE_BYTES (2_250_000) sits 150_000 bytes above
    # MAX_SYNC_V1_BATCH_PAYLOAD_BYTES (2_100_000), and the only thing between
    # them is per-record framing. `pull_since` builds `id` as
    # `f"server:{sequence}"`, so its framing is ~670 bytes a record and 200 of
    # them reach only ~2_235_000 -- the ceiling is unreachable through the pull
    # loop. It IS reachable for a record whose `id` is at the 256-character cap,
    # which `SyncRecord` allows and `pull_page_accepts` therefore accepts: three
    # capped identifiers take the framing to ~911 bytes and 170 records cross
    # 2_250_000 with the aggregate check still 60 KB clear. That is what makes
    # the third ceiling load-bearing rather than decorative -- for any other
    # producer of `SyncRecord`s, which is precisely what a second implementation
    # is.
    wide = "w" * reference.MAX_SYNC_IDENTIFIER_LENGTH
    add(
        "response_ceiling",
        0,
        [
            change(
                n,
                entity_id=wide,
                client_mutation_id=wide,
                payload_json={"blob": "p" * 9_253},
                wide_id=True,
            )
            for n in range(1, 171)
        ],
    )

    # -- the unhandled-500 defect --------------------------------------------
    # app/schemas.py:76 types `payload` as `bytes` with no UTF-8 check, so this
    # record VALIDATES, and pull_page_accepts() calls model_dump_json() on it at
    # sync_service.py:84. Pydantic raises PydanticSerializationError, which is
    # not one of the four the pull loop catches -- it escapes the pull as a 500.
    add(
        "non_utf8_payload",
        0,
        [
            {
                **change(1),
                "raw_payload_hex": "fffe",
            }
        ],
    )

    rng = random.Random(20261004)
    for index in range(100):
        changes = [
            change(
                position + 1,
                entity_type=rng.choice(ENTITY_TYPES),
                entity_id=_random_page_id(rng),
                payload_json=_random_payload_json(rng, rng.randint(0, 3)),
                server_revision=rng.randint(0, 10**6),
                client_mutation_id=None if rng.random() < 0.2 else f"m{position}",
                deleted_at=None if rng.random() < 0.7 else "2026-01-02T03:04:05Z",
            )
            for position in range(rng.randint(1, 12))
        ]
        add(f"random_{index:03d}", rng.randint(0, 10**6), changes)

    return specs


def _random_page_id(rng) -> str:
    pool = "abcdefgh-_\u00e9\u4e2d\U0001f600\n\t\"\\/\x00\x1f\x7f"
    return "".join(rng.choice(pool) for _ in range(rng.randint(1, 24)))


def _random_payload_json(rng, depth):
    """A `SyncEntity.payload_json` value -- always a JSON OBJECT at the top.

    `decode_payload` raises unless a payload decodes to an object
    (`app/sync_service.py:95-96`), and that decoded object is what gets stored
    in `payload_json`. So a bare array or scalar is not a value this column can
    hold, and generating one would measure a state the reference cannot reach --
    it would fail on `Page.to_record/1`, which assumes the same shape, and the
    failure would read as a page-sizing bug.
    """
    return {"value": _random_payload_value(rng, depth)}


def _random_payload_value(rng, depth):
    if depth == 0 or rng.random() < 0.5:
        return rng.choice(
            [None, True, False, rng.randint(-(10**9), 10**9), _random_page_id(rng)]
        )
    if rng.random() < 0.75:
        return [_random_payload_value(rng, depth - 1) for _ in range(rng.randint(0, 4))]
    return {
        f"k{rng.randint(0, 6)}": _random_payload_value(rng, depth - 1)
        for _ in range(rng.randint(0, 4))
    }


def page_cases(reference: Reference) -> list[dict]:
    return [
        {**spec, **page_measure(reference, spec["cursor_sequence"], spec["changes"])}
        for spec in page_specs(reference)
    ]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def limits(reference: Reference) -> dict:
    """The page ceilings and the rejection vocabulary, read out of the
    reference by `literal_eval` -- never transcribed."""
    return {
        "max_records": module_constant(reference.schemas, "MAX_SYNC_RECORDS"),
        "max_batch_payload_bytes": module_constant(
            reference.schemas, "MAX_SYNC_V1_BATCH_PAYLOAD_BYTES"
        ),
        "max_response_bytes": module_constant(
            reference.sync_service, "MAX_SYNC_PULL_RESPONSE_BYTES"
        ),
        "max_scan_changes": module_constant(
            reference.sync_service, "MAX_SYNC_PULL_SCAN_CHANGES"
        ),
        "max_identifier_length": module_constant(
            reference.schemas, "MAX_SYNC_IDENTIFIER_LENGTH"
        ),
        "invalid_mutation_code": module_constant(
            reference.rejection_codes, "_DEFAULT_BY_CLASS"
        )["InvalidMutation"],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Python-side parity oracle.")
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parents[5]),
        help="the reference repository root (the directory holding app/)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="write the document here instead of stdout; used so that the "
        "caller can keep interpreter diagnostics on stderr without them "
        "landing in the stream it is decoding",
    )
    parser.add_argument(
        "--only",
        default="all",
        choices=["all", "admission", "page", "payload"],
        help="which section(s) to emit; `all` is one object with every section",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / "app" / "sync_service.py").is_file():
        print(f"parity_oracle: no Python reference tree at {repo}", file=sys.stderr)
        return 3

    try:
        reference = Reference(repo)
    except ModuleNotFoundError as exc:
        print(f"parity_oracle: {exc}", file=sys.stderr)
        return 4

    document: dict = {"limits": limits(reference)}
    wanted = ["admission", "page", "payload"] if args.only == "all" else [args.only]
    for section in wanted:
        if section == "admission":
            document["admission"] = admission_cases(reference)
        elif section == "page":
            document["page"] = page_cases(reference)
        else:
            document["payload"] = payload_cases(reference)

    text = json.dumps(document, ensure_ascii=True, sort_keys=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))