"""Every documented request limit, measured against the real app.

docs/contract/LIMITS.md tells client developers what each ceiling is and what exceeding it
returns. Those are two separate claims and both can rot: the number (a constant changes) and
the response (a validator moves, a middleware is reordered). tests/test_contract_limits_doc.py
pins the numbers against the constants; THIS file pins the responses, by actually exceeding
each limit through the real app with a real authenticated session.

Each test asserts the status AND the specific reason, so a 422 for an unrelated field cannot
satisfy a test about a record-count ceiling.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth import token_hash
from app.db import SessionFactory
from app.main import app
from app.models import AuthSession, Membership, Organization, User
from app.rate_limit import reset_limiter_for_tests
from app.schemas import MAX_SYNC_RECORDS, MAX_SYNC_V1_BATCH_PAYLOAD_BYTES

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

TOKEN = "contract-limits-token-long-enough-to-be-realistic-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
async def _session():
    """A live session for this file's ids only; never truncates shared tables."""
    from sqlalchemy import delete

    reset_limiter_for_tests()
    now = datetime.now(timezone.utc)
    async with SessionFactory() as db:
        await db.execute(delete(AuthSession).where(AuthSession.id == "sess-limits-1"))
        await db.execute(delete(Membership).where(Membership.id == "membership-limits-1"))
        await db.execute(delete(User).where(User.id == "user-limits-1"))
        await db.execute(delete(Organization).where(Organization.id == "org-limits-1"))
        await db.commit()
        db.add(Organization(id="org-limits-1", name="Limits", authorization_revision=1))
        db.add(User(id="user-limits-1", display_name="Limits", email="limits@example.invalid"))
        await db.flush()
        db.add(Membership(
            id="membership-limits-1", organization_id="org-limits-1", user_id="user-limits-1",
            active=True, all_customers=True, all_projects=True,
            capabilities=["sync", "item.evidence.manage"],
        ))
        await db.flush()
        db.add(AuthSession(
            id="sess-limits-1", organization_id="org-limits-1",
            membership_id="membership-limits-1", user_id="user-limits-1",
            access_token_hash=token_hash(TOKEN),
            issued_at=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
        ))
        await db.commit()
    yield
    reset_limiter_for_tests()


def _record(index: int, payload: str = "e30=") -> dict:
    return {
        "id": f"rec-{index}",
        "entityType": "customer",
        "entityID": f"customer-{index}",
        "updatedAt": "2026-10-07T00:00:00Z",
        "payload": payload,
    }


def _errors(response) -> list[dict]:
    body = response.json()
    return body.get("detail", []) if isinstance(body.get("detail"), list) else []


def test_push_rejects_more_than_the_record_ceiling():
    batch = {"deviceID": "dev-1", "records": [_record(i) for i in range(MAX_SYNC_RECORDS + 1)]}
    with TestClient(app) as client:
        response = client.post("/api/v1/sync/push", json=batch, headers=AUTH)
    assert response.status_code == 422, response.text[:300]
    assert any(
        e.get("loc", [])[-1:] == ["records"] and e.get("type") == "too_long"
        for e in _errors(response)
    ), response.text[:300]


def test_push_rejects_an_aggregate_payload_over_the_batch_ceiling():
    # Two records, each legal on its own, whose payloads sum past the aggregate ceiling.
    half = MAX_SYNC_V1_BATCH_PAYLOAD_BYTES // 2 + 1024
    batch = {"deviceID": "dev-1", "records": [_record(0, "A" * half), _record(1, "A" * half)]}
    with TestClient(app) as client:
        response = client.post("/api/v1/sync/push", json=batch, headers=AUTH)
    assert response.status_code == 422, response.text[:300]
    assert "aggregate sync payload exceeds" in response.text


def test_push_body_over_the_byte_guard_is_413_before_parsing():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sync/push",
            content=b"x" * (4 * 1024 * 1024 + 1),
            headers={**AUTH, "Content-Type": "application/json"},
        )
    assert response.status_code == 413


def test_pairing_body_over_its_byte_guard_is_413():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/pair",
            content=b"x" * (16 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413


def test_pull_cursor_beyond_int64_is_a_400_not_a_500():
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/sync/pull", params={"cursor": "seq:" + "9" * 20}, headers=AUTH
        )
    assert response.status_code == 400, response.text[:300]


def test_evidence_filename_header_over_512_is_422():
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/sync/evidence-blobs/doc-limits-1",
            content=b"x",
            headers={
                **AUTH,
                "X-Vitrial-Item-ID": "item-1",
                "X-Vitrial-Filename": "f" * 513,
                "X-Content-SHA256": "0" * 64,
                "Content-Type": "application/octet-stream",
            },
        )
    assert response.status_code == 422, response.text[:300]
    assert "X-Vitrial-Filename" in response.text or "x-vitrial-filename" in response.text.lower()
