from __future__ import annotations

import pytest

from app.auth import Principal
from app.lifecycle import (
    LifecycleRejected,
    validate_blocker_lifecycle,
    validate_quotation_lifecycle,
)


def principal(*, user_id: str = "user-1", session_id: str = "session-1") -> Principal:
    return Principal(
        user_id=user_id,
        organization_id="org-1",
        membership_id="membership-1",
        session_id=session_id,
        authorization_revision=7,
        capabilities=frozenset({"sync", "item.blockers.manage", "quotation.create", "quotation.send", "quotation.approve"}),
        customer_ids=frozenset({"customer-1"}),
        project_ids=frozenset({"project-1"}),
        all_customers=False,
        all_projects=False,
    )


def provenance(actor: Principal) -> dict:
    return {
        "actorID": actor.user_id,
        "organizationID": actor.organization_id,
        "membershipID": actor.membership_id,
        "sessionID": actor.session_id,
        "authorizationRevision": actor.authorization_revision,
    }


def blocker_event(actor: Principal, event_id: str, kind: str, *, description: str = "Verify mullion") -> dict:
    return {
        "id": event_id,
        "blockerID": "blocker-1",
        "itemID": "item-1",
        "kind": kind,
        "blockerCategory": "technical",
        "blockerKind": "engineering",
        "description": description,
        "critical": True,
        "responsiblePerson": "Engineer",
        "vendorSupplier": "",
        **provenance(actor),
    }


def blocker_payload(actor: Principal, *, status: str = "open", resolved_at=None, events=None) -> dict:
    return {
        "id": "blocker-1",
        "itemID": "item-1",
        "category": "technical",
        "kind": "engineering",
        "description": "Verify mullion",
        "status": status,
        "critical": True,
        "resolvedAt": resolved_at,
        "responsiblePerson": "Engineer",
        "vendorSupplier": "",
        "events": events if events is not None else [blocker_event(actor, "event-created", "created")],
    }


def quote_event(actor: Principal, event_id: str, kind: str) -> dict:
    return {"id": event_id, "kind": kind, **provenance(actor)}


def quote_payload(*, status: str, events=None, number: str = "Q-100", revision: int = 1) -> dict:
    return {
        "id": "quotation-1",
        "projectID": "project-1",
        "customerID": "customer-1",
        "number": number,
        "status": status,
        "currencyCode": "COP",
        "lines": [{"id": "line-1", "itemID": "item-1", "quantity": 1}],
        "paymentTerms": [],
        "notes": "",
        "revision": revision,
        "supersedesQuotationID": None,
        "events": events or [],
    }


def test_blocker_history_replays_resolve_reopen_and_update():
    actor = principal()
    events = [
        blocker_event(actor, "event-created", "created"),
        blocker_event(actor, "event-updated", "updated", description="Verify reinforced mullion"),
        blocker_event(actor, "event-resolved", "resolved", description="Verify reinforced mullion"),
        blocker_event(actor, "event-reopened", "reopened", description="Verify reinforced mullion"),
    ]
    payload = blocker_payload(actor, events=events)
    payload["description"] = "Verify reinforced mullion"
    validate_blocker_lifecycle(payload, None, actor, entity_id="blocker-1")


def test_blocker_status_and_history_cannot_disagree():
    actor = principal()
    payload = blocker_payload(
        actor,
        status="resolved",
        resolved_at="2026-09-07T12:00:00Z",
        events=[blocker_event(actor, "event-created", "created")],
    )
    with pytest.raises(LifecycleRejected, match="status does not match"):
        validate_blocker_lifecycle(payload, None, actor, entity_id="blocker-1")


def test_blocker_history_prefix_is_immutable_and_new_tail_is_current_actor():
    old_actor = principal(user_id="user-old", session_id="session-old")
    actor = principal()
    created = blocker_event(old_actor, "event-created", "created")
    current = blocker_payload(old_actor, events=[created])

    rewritten = blocker_payload(actor, events=[dict(created) | {"description": "rewritten"}])
    with pytest.raises(LifecycleRejected, match="history is immutable"):
        validate_blocker_lifecycle(rewritten, current, actor, entity_id="blocker-1")

    appended = blocker_payload(
        actor,
        status="resolved",
        resolved_at="2026-09-07T12:00:00Z",
        events=[created, blocker_event(actor, "event-resolved", "resolved")],
    )
    validate_blocker_lifecycle(appended, current, actor, entity_id="blocker-1")


@pytest.mark.asyncio
async def test_quotation_history_allows_exact_client_transition_graph():
    actor = principal()
    sent = quote_event(actor, "event-sent", "sent")
    rejected = quote_event(actor, "event-rejected", "rejected")

    # draft -> ready (no event) -> sent -> rejected -> draft (no event) -> ready (no event)
    payload = quote_payload(status="ready", events=[sent, rejected])
    await validate_quotation_lifecycle(None, payload, None, actor, entity_id="quotation-1")

    approved = quote_payload(
        status="approved",
        events=[sent, quote_event(actor, "event-approved", "approved")],
    )
    await validate_quotation_lifecycle(None, approved, None, actor, entity_id="quotation-1")


@pytest.mark.asyncio
async def test_quotation_invalid_transition_and_terminal_escape_are_rejected():
    actor = principal()
    direct_approval = quote_payload(
        status="approved",
        events=[quote_event(actor, "event-approved", "approved")],
    )
    with pytest.raises(LifecycleRejected, match="event sequence"):
        await validate_quotation_lifecycle(None, direct_approval, None, actor, entity_id="quotation-1")

    current = quote_payload(
        status="approved",
        events=[
            quote_event(actor, "event-sent", "sent"),
            quote_event(actor, "event-approved", "approved"),
        ],
    )
    proposed = dict(current) | {"status": "draft"}
    with pytest.raises(LifecycleRejected, match="not reachable"):
        await validate_quotation_lifecycle(None, proposed, current, actor, entity_id="quotation-1")


@pytest.mark.asyncio
async def test_customer_facing_quotation_content_is_locked_but_lifecycle_can_advance():
    actor = principal()
    sent_event = quote_event(actor, "event-sent", "sent")
    current = quote_payload(status="sent", events=[sent_event])

    changed_content = dict(current) | {"number": "Q-CHANGED"}
    with pytest.raises(LifecycleRejected, match="content is locked"):
        await validate_quotation_lifecycle(None, changed_content, current, actor, entity_id="quotation-1")

    rejected = dict(current) | {
        "status": "rejected",
        "events": [sent_event, quote_event(actor, "event-rejected", "rejected")],
    }
    await validate_quotation_lifecycle(None, rejected, current, actor, entity_id="quotation-1")


@pytest.mark.asyncio
async def test_quotation_history_prefix_and_latest_connected_actor_are_enforced():
    old_actor = principal(user_id="user-old", session_id="session-old")
    actor = principal()
    sent = quote_event(old_actor, "event-sent", "sent")
    current = quote_payload(status="sent", events=[sent])

    rewritten = dict(current) | {
        "events": [dict(sent) | {"actorID": "forged"}],
    }
    with pytest.raises(LifecycleRejected, match="history is immutable"):
        await validate_quotation_lifecycle(None, rewritten, current, actor, entity_id="quotation-1")

    approved_by_old_actor = dict(current) | {
        "status": "approved",
        "events": [sent, quote_event(old_actor, "event-approved", "approved")],
    }
    with pytest.raises(LifecycleRejected, match="authenticated provenance"):
        await validate_quotation_lifecycle(None, approved_by_old_actor, current, actor, entity_id="quotation-1")
