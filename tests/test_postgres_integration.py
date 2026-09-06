import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, func, select

from app.auth import Principal, current_principal, token_hash
from app.db import SessionFactory
from app.models import (
    AuthSession,
    EvidenceBlob,
    Membership,
    Organization,
    SyncChangeLog,
    SyncEntity,
    SyncMutation,
    User,
)
from app.schemas import SyncBatch
from app.sync_service import apply_push, pull_since

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)

ROOT = Path(__file__).resolve().parents[1]


async def clear_database(db):
    for model in (
        EvidenceBlob,
        SyncChangeLog,
        SyncMutation,
        SyncEntity,
        AuthSession,
        Membership,
        User,
        Organization,
    ):
        await db.execute(delete(model))
    await db.commit()


@pytest.mark.asyncio
async def test_session_authentication_and_revocation():
    token = "integration-access-token"
    now = datetime.now(timezone.utc)

    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=9))
        db.add(User(id="user-1", display_name="Operator", email="operator@example.invalid"))
        db.add(Membership(
            id="membership-1",
            organization_id="org-1",
            user_id="user-1",
            active=True,
            all_customers=False,
            all_projects=False,
            customer_ids=["customer-1"],
            project_ids=["project-1"],
            roles=[{"id": "operator", "displayName": "Operator"}],
            capabilities=["sync", "item.edit"],
        ))
        session = AuthSession(
            id="session-1",
            organization_id="org-1",
            membership_id="membership-1",
            user_id="user-1",
            access_token_hash=token_hash(token),
            issued_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        )
        db.add(session)
        await db.commit()

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        principal = await current_principal(credentials, db)
        assert principal.organization_id == "org-1"
        assert principal.authorization_revision == 9
        assert principal.project_ids == frozenset({"project-1"})

        session.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await current_principal(credentials, db)
        assert exc.value.status_code == 401

        await clear_database(db)


@pytest.mark.asyncio
async def test_sync_idempotency_stale_rejection_and_pull_acknowledgement():
    fixture = json.loads(
        (ROOT / "contracts/backend/v1/sync-push.request.json").read_text(encoding="utf-8")
    )
    batch = SyncBatch.model_validate(fixture)
    principal = Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=4,
        capabilities=frozenset({"sync"}),
        customer_ids=frozenset({"customer-001"}),
        project_ids=frozenset(),
        all_customers=False,
        all_projects=False,
    )

    async with SessionFactory() as db:
        await clear_database(db)
        db.add(Organization(id="org-1", name="Vitrial", authorization_revision=4))
        db.add(SyncEntity(
            organization_id="org-1",
            entity_type="customer",
            entity_id="customer-001",
            server_revision=7,
            schema_version=1,
            payload_json={"name": "Before"},
            updated_at=datetime.now(timezone.utc),
            deleted_at=None,
        ))
        await db.commit()

        first = await apply_push(db, principal, batch)
        assert first.acceptedRecordIDs == ["record-001"]
        assert first.rejectedRecordIDs == []

        entity = await db.get(SyncEntity, ("org-1", "customer", "customer-001"))
        assert entity is not None
        assert entity.server_revision == 8

        replay = await apply_push(db, principal, batch)
        assert replay.acceptedRecordIDs == ["record-001"]
        change_count = await db.scalar(
            select(func.count()).select_from(SyncChangeLog).where(
                SyncChangeLog.organization_id == "org-1"
            )
        )
        assert change_count == 1

        stale_payload = fixture.copy()
        stale_payload["records"] = [dict(fixture["records"][0])]
        stale_payload["records"][0]["id"] = "record-stale"
        stale_payload["records"][0]["clientMutationID"] = "mutation-stale"
        stale_payload["records"][0]["baseServerRevision"] = 7
        stale = await apply_push(db, principal, SyncBatch.model_validate(stale_payload))
        assert stale.acceptedRecordIDs == []
        assert stale.rejectedRecordIDs == ["record-stale"]

        pulled = await pull_since(db, principal, "seq:0")
        assert len(pulled.records) == 1
        assert pulled.records[0].serverRevision == 8
        assert pulled.records[0].clientMutationID == "mutation-001"
        assert pulled.cursor and pulled.cursor.startswith("seq:")

        await clear_database(db)
