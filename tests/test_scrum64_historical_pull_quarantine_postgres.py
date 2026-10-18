"""SCRUM-64: refuse to emit historically poisoned tenant claims in V1/V2 pulls.

Production data are never read or rewritten by these tests; synthetic rows
are inserted in an isolated, freshly migrated PostgreSQL integration database.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db import SessionFactory
from app.idempotency import SyncMutationFingerprint
from app.main import app
from app.models import (
    AuthSession, CanonicalCustomer, Membership, Organization, SyncChangeLog,
    SyncEntity, SyncMutation, User,
)
from app.settings import settings
from app.tenant_claims import tenant_claims_match

ADMIN_KEY = "synthetic-scrum64-historical-test"
pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires disposable migrated PostgreSQL",
)


@pytest.mark.parametrize("claims,should_allow", [
    ({}, True),
    ({"organizationID":"synthetic-A"}, True),
    ({"organizationId":"synthetic-A","organization_id":"synthetic-A"}, True),
    ({"organizationID":"synthetic-B"}, False),
    ({"organizationId":"synthetic-B"}, False),
    ({"organization_id":"synthetic-B"}, False),
    ({"organizationID":"synthetic-A","organization_id":"synthetic-B"}, False),
    ({"organizationID":None}, False),
    ({"organizationID":True}, False),
    ({"organizationID":["synthetic-A"]}, False),
])
def test_shared_input_and_output_tenant_predicate(claims,should_allow):
    assert tenant_claims_match("synthetic-A",claims) is should_allow


@pytest.mark.parametrize("payload", [None, [], "{}", 23, True])
def test_non_mapping_legacy_tenant_claim_cannot_pass(payload):
    assert tenant_claims_match("synthetic-A", payload) is False


async def cleanup(organization_ids:tuple[str,str]) -> None:
    async with SessionFactory() as db:
        users=(await db.scalars(select(Membership.user_id).where(
            Membership.organization_id.in_(organization_ids)))).all()
        for model in (SyncChangeLog, SyncEntity, SyncMutation,
                      SyncMutationFingerprint, CanonicalCustomer,
                      AuthSession, Membership):
            await db.execute(delete(model).where(model.organization_id.in_(organization_ids)))
        await db.flush()
        if users:
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_key", [
    "organizationID","organizationId","organization_id",
])
async def test_historical_poisoned_row_quarantined_without_stalling_v1_v2(
    monkeypatch,tenant_key
):
    nonce=uuid4().hex[:16]
    org_a="synthetic-historical-A-"+nonce
    org_b="synthetic-historical-B-"+nonce
    ids=(org_a,org_b)
    poisoned="legacy-overlap-"+nonce
    safe="safe-"+nonce
    monkeypatch.setattr(
        settings,"admin_api_key_hash",hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
    await cleanup(ids)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
            async def token(org,label):
                request=dict(
                    organizationID=org,organizationName="Synthetic Historical",
                    userID="user-"+nonce+"-"+label,displayName="Synthetic "+label,
                    email=label+nonce+"@example.invalid",
                    membershipID="member-"+nonce+"-"+label,
                    active=True,sharedCompanyAccess=True,
                    allCustomers=True,allProjects=True,roles=[],
                    customerIDs=[],projectIDs=[],
                    capabilities=["sync"],pilotSeatLimit=2,
                    sessionID="session-"+uuid4().hex,sessionTTLSeconds=3600)
                response=await c.post(
                    "/internal/admin/v1/bootstrap",
                    headers={"X-Vitrial-Admin-Key":ADMIN_KEY},json=request)
                assert response.status_code==200,response.text
                return {"Authorization":"Bearer "+response.json()["accessToken"]}

            headers_a=await token(org_a,"a")
            headers_b=await token(org_b,"b")
            # Explicit legacy bypass: simulation of an EXISTING historical row
            # written before this security guard existed. Never use apply_push.
            async with SessionFactory() as db:
                for org,customer,claimed,marker in (
                    (org_a,poisoned,org_b,"POISON_NEVER_DELIVER"),
                    (org_a,safe,org_a,"SAFE_A"),
                    (org_b,poisoned,org_b,"SAFE_B"),
                ):
                    db.add(CanonicalCustomer(organization_id=org,
                                             customer_id=customer))
                    await db.flush()
                    db.add(SyncEntity(
                        organization_id=org,entity_type="customer",
                        entity_id=customer,server_revision=1,schema_version=1,
                        payload_json={"id":customer,tenant_key:claimed,
                                      "marker":marker},
                        updated_at=datetime.now(timezone.utc)))
                    db.add(SyncChangeLog(
                        organization_id=org,entity_type="customer",
                        entity_id=customer,server_revision=1,operation="upsert",
                        client_mutation_id="historical-"+uuid4().hex))
                    await db.flush()
                await db.commit()

            for route in ("/api/v1/sync/pull","/api/v2/sync/pull"):
                for headers,expected in ((headers_a,"SAFE_A"),(headers_b,"SAFE_B")):
                    response=await c.get(route+"?cursor=seq:0",headers=headers)
                    assert response.status_code==200,response.text
                    records=response.json()["records"]
                    assert len(records)==1
                    if "/v2/" in route:
                        payload=records[0]["payload"]
                    else:
                        payload=json.loads(base64.b64decode(records[0]["payload"]))
                    assert payload["marker"]==expected
                    assert "POISON_NEVER_DELIVER" not in response.text
                    assert response.json()["cursor"].startswith("seq:")
                    next_page=await c.get(
                        route+"?cursor="+response.json()["cursor"],headers=headers)
                    assert next_page.status_code==200
                    assert next_page.json()["records"]==[]
            # Source rows are preserved for a separately authorized remediation,
            # not overwritten or deleted by a sync read.
            async with SessionFactory() as db:
                untrusted=await db.get(SyncEntity,(org_a,"customer",poisoned))
                assert untrusted.payload_json["marker"]=="POISON_NEVER_DELIVER"
                assert untrusted.payload_json[tenant_key]==org_b
            assert (await c.get("/api/v1/sync/pull")).status_code==401
    finally:
        await cleanup(ids)
