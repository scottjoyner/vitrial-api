"""Delivery authorization has exactly one authority gate, and it is not this one.

`authorize_record` (app/ownership.py) used to carry a `delivery_execution` branch.
It was unreachable: the push dispatches on `record.entityType` and calls
`authorize_delivery_execution` for `delivery_execution`
(app/sync_service.py:347-363), so `authorize_record` is only ever reached in the
else-arm. `tests/test_delivery_execution_ownership.py` drove the dead branch
directly with a stub session, so eleven green tests asserted against a gate no
request could reach -- and the copy had already drifted from the live one. It
skipped the `delivery.manage` capability check on tombstones (`_require_capability`
sat inside `if not deleting:`) and permitted tombstoning a known execution, which
`authorize_delivery_execution` refuses outright at app/delivery_execution.py:715.

Deleting the branch does not merely remove dead code; it makes the fall-through the
behaviour. `delivery_execution` now reaches the closing
`raise AuthorizationRejected(...)`, so if the dispatch in `sync_service` is ever
broken the record is denied rather than authorized by a stale second gate. That is
the property these tests pin, and it is checkable without a database -- which is the
point: the previous coverage was gated behind a stub session that could not tell
whether any request reached it.

The live gate's own coverage is `tests/test_delivery_execution_postgres.py`, which
drives it through the real push path.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.auth import Principal
from app.delivery_execution import DELIVERY_ENTITY_TYPE
from app.ownership import AuthorizationRejected, EffectiveScope, authorize_record

OWNERSHIP = pathlib.Path(__file__).resolve().parent.parent / "app" / "ownership.py"
SYNC_SERVICE = pathlib.Path(__file__).resolve().parent.parent / "app" / "sync_service.py"


def principal() -> Principal:
    return Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=7,
        capabilities=frozenset({"delivery.manage", "catalog.manage"}),
        customer_ids=frozenset({"customer-1"}),
        project_ids=frozenset({"project-1"}),
        all_customers=False,
        all_projects=False,
    )


class TrippedSession:
    """Fails if the authorization path touches the database at all.

    The delivery branch used to read the canonical project, project-child and
    entity tables, so a stub session was enough to make it look covered. A session
    that raises on any access makes the claim the other way round: reaching this
    gate for a delivery record must be refused before any state is consulted.
    """

    def __getattr__(self, name):
        raise AssertionError(f"the generic authorization gate touched the database ({name})")


@pytest.mark.asyncio
async def test_generic_gate_refuses_delivery_execution():
    actor = principal()
    with pytest.raises(AuthorizationRejected, match="not implemented for delivery_execution"):
        await authorize_record(
            TrippedSession(),
            actor,
            EffectiveScope.from_principal(actor),
            entity_type=DELIVERY_ENTITY_TYPE,
            entity_id="execution-1",
            payload={"id": "execution-1"},
            deleted_at=None,
            generic_entity_exists=False,
        )


def _function(path: pathlib.Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


def _string_constants(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def test_authorize_record_has_no_delivery_execution_branch():
    """No second authority gate for delivery, at all -- not even a disabled one."""
    body = _function(OWNERSHIP, "authorize_record")
    assert DELIVERY_ENTITY_TYPE not in _string_constants(body), (
        "authorize_record grants delivery_execution again. It is unreachable from the "
        "push, so any version of it is a second authority that can drift from "
        "authorize_delivery_execution; delivery must have exactly one gate."
    )


def _references_delivery(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == "DELIVERY_ENTITY_TYPE"
        for child in ast.walk(node)
    )


def test_push_routes_delivery_away_from_the_generic_gate():
    """The dispatch is what keeps delivery out of the generic gate; pin it.

    Every `authorize_record` call in `app/sync_service.py` must sit in the
    `else`-arm of an `if` on `DELIVERY_ENTITY_TYPE`. A refactor that hoists the call
    above the branch would silently promote every generic type's gate over
    delivery's -- and the tests above would still pass, because they call
    `authorize_record` directly, which is how this defect hid in the first place.
    """
    tree = ast.parse(SYNC_SERVICE.read_text())
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "authorize_record"
    ]
    assert calls, "no authorize_record call found in app/sync_service.py -- the dispatch moved; this test needs re-reading"

    for call in calls:
        child, parent = call, parents.get(call)
        guarded = False
        while parent is not None:
            if isinstance(parent, ast.If) and child in parent.orelse and _references_delivery(parent.test):
                guarded = True
                break
            child, parent = parent, parents.get(parent)
        assert guarded, (
            "app/sync_service.py calls authorize_record outside the else-arm of a "
            "DELIVERY_ENTITY_TYPE branch. Delivery records would then be authorized "
            "by the generic gate, which does not know delivery's capability, "
            "tombstone or quotation rules."
        )