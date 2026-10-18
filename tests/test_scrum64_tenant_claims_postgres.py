"""SCRUM-64: real disposable-PG, overlapping-ID, cross-tenant sync denial."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.auth import Principal
from app.db import SessionFactory
from app.idempotency import SyncMutationFingerprint
from app.main import app
from app.models import (
    AuthSession, CanonicalCustomer, Membership, Organization,
    SyncChangeLog, SyncEntity, SyncMutation, User,
)
from app.ownership import AuthorizationRejected, _require_matching_tenant_claim
from app.settings import settings

KEY = "synthetic-scrum64-admin"
DB_ENABLED = os.getenv("POSTGRES_INTEGRATION") == "1"


@pytest.fixture
def tenant_principal():
    return Principal(
        user_id="synthetic-user", organization_id="synthetic-company-a",
        membership_id="synthetic-member", session_id="synthetic-session",
        authorization_revision=0, capabilities=frozenset({"sync"}),
        customer_ids=frozenset(), project_ids=frozenset(),
        all_customers=True, all_projects=True,
    )


@pytest.mark.parametrize("name,value", [
    ("organizationID", "synthetic-company-b"),
    ("organizationId", "synthetic-company-b"),
    ("organization_id", "synthetic-company-b"),
    ("organizationID", None),
    ("organizationID", 23),
    ("organizationID", " synthetic-company-a "),
])
def test_optional_foreign_tenant_claim_is_rejected(tenant_principal,name,value):
    with pytest.raises(AuthorizationRejected, match="authenticated tenant"):
        _require_matching_tenant_claim(tenant_principal,{"id":"same-id",name:value})


@pytest.mark.parametrize("payload", [
    {"id":"same-id"},
    {"id":"same-id","organizationID":"synthetic-company-a"},
    {"id":"same-id","organization_id":"synthetic-company-a"},
    {"id":"same-id","organizationId":"synthetic-company-a"},
    {"id":"same-id","organizationID":"synthetic-company-a",
     "organizationId":"synthetic-company-a"},
])
def test_missing_or_matching_tenant_claim_preserves_legacy_payload(tenant_principal,payload):
    _require_matching_tenant_claim(tenant_principal,payload)


async def cleanup(org_ids:tuple[str,str]) -> None:
    async with SessionFactory() as db:
        users=(await db.scalars(select(Membership.user_id).where(
            Membership.organization_id.in_(org_ids)))).all()
        for model in (SyncChangeLog, SyncEntity, SyncMutation, SyncMutationFingerprint,
                      CanonicalCustomer, AuthSession, Membership):
            await db.execute(delete(model).where(model.organization_id.in_(org_ids)))
        await db.flush()
        if users:
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))
        await db.commit()


@pytest.mark.skipif(not DB_ENABLED, reason="isolated migrated PostgreSQL required")
@pytest.mark.asyncio
async def test_overlapping_customer_ids_no_cross_tenant_v1_v2_sync(monkeypatch):
    nonce=uuid.uuid4().hex[:14]
    company_a="synthetic-a-"+nonce
    company_b="synthetic-b-"+nonce
    orgs=(company_a,company_b)
    shared_id="customer-shared-"+nonce
    monkeypatch.setattr(settings,"admin_api_key_hash",hashlib.sha256(KEY.encode()).hexdigest())
    await cleanup(orgs)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
            async def admit(organization, label):
                body={
                  "organizationID":organization, "organizationName":"Synthetic Organization",
                  "userID":"user-"+nonce+"-"+label,
                  "displayName":"Synthetic "+label,
                  "email":label+nonce+"@example.invalid",
                  "membershipID":"member-"+nonce+"-"+label,
                  "active":True,"sharedCompanyAccess":True,
                  "allCustomers":False,"allProjects":False,
                  "roles":[{"id":"sales","displayName":"Sales"}],
                  "customerIDs":[],"projectIDs":[],
                  "capabilities":["sync","customer.create","customer.edit"],
                  "pilotSeatLimit":2,
                  "sessionID":"session-"+uuid.uuid4().hex,
                  "sessionTTLSeconds":3600}
                r=await c.post("/internal/admin/v1/bootstrap",
                    headers={"X-Vitrial-Admin-Key":KEY},json=body)
                assert r.status_code==200,r.text
                return {"Authorization":"Bearer "+r.json()["accessToken"]}

            headers_a=await admit(company_a,"a")
            headers_b=await admit(company_b,"b")
            def body(tenant,mutation,marker,alias="organizationID"):
                return {
                  "protocolVersion":2, "deviceID":"synthetic-device",
                  "records":[{"id":mutation, "entityType":"customer",
                      "entityID":shared_id,
                      "updatedAt":datetime(2026,10,17,tzinfo=timezone.utc).isoformat(),
                      "payload":{"id":shared_id,alias:tenant,"marker":marker},
                      "clientMutationID":mutation}]}
            async def push(headers,doc):
                r=await c.post("/api/v2/sync/push",headers=headers,json=doc)
                assert r.status_code==200,r.text
                return r.json()

            forged=await push(headers_a,body(company_b,"forge-"+nonce,"should-never-persist"))
            assert forged["acceptedRecordIDs"]==[]
            assert forged["rejectedRecordIDs"]==["forge-"+nonce]
            # V1 uses base64 JSON but reaches exactly the same server-side
            # ownership guard; a forged alias must also be denied here.
            v1_forged_payload={
                "id":shared_id, "organizationId":company_b, "marker":"v1-forgery"}
            v1_record={
                "id":"v1-forge-"+nonce, "entityType":"customer",
                "entityID":shared_id,
                "updatedAt":datetime(2026,10,17,tzinfo=timezone.utc).isoformat(),
                "payload":base64.b64encode(
                    json.dumps(v1_forged_payload).encode()).decode(),
                "clientMutationID":"v1-forge-"+nonce}
            v1_response=await c.post("/api/v1/sync/push",headers=headers_a,
                json={"deviceID":"synthetic-v1-device","records":[v1_record]})
            assert v1_response.status_code==200,v1_response.text
            assert v1_response.json()["acceptedRecordIDs"]==[]
            assert v1_response.json()["rejectedRecordIDs"]==["v1-forge-"+nonce]
            # Client omitted the optional organization field: still valid.
            good_a=body(company_a,"ok-a-"+nonce,"private-A")
            good_a["records"][0]["payload"].pop("organizationID")
            result_a=await push(headers_a,good_a)
            assert result_a["acceptedRecordIDs"]==["ok-a-"+nonce]
            result_b=await push(headers_b,body(company_b,"ok-b-"+nonce,"private-B"))
            assert result_b["acceptedRecordIDs"]==["ok-b-"+nonce]

            # Different tenants have the exact SAME entity ID and can still
            # independently retrieve their own canonical record.
            for headers,expected,forbidden in (
                (headers_a,"private-A","private-B"),
                (headers_b,"private-B","private-A"),
            ):
                for route in ("/api/v1/sync/pull","/api/v2/sync/pull"):
                    response=await c.get(route+"?cursor=seq:0",headers=headers)
                    assert response.status_code==200,response.text
                    records=[v for v in response.json()["records"]
                             if v["entityID"]==shared_id]
                    assert len(records)==1
                    if "/v2/" in route:
                        payload=records[0]["payload"]
                    else:
                        wire=records[0]["payload"]
                        payload=json.loads(base64.b64decode(wire))
                    assert payload["marker"]==expected
                    assert payload["marker"]!=forbidden

            # A forged B marker cannot overwrite company A's own record,
            # even when a matching entity ID and revision exist in both.
            newer=body(company_b,"overwrite-"+nonce,"cross-write","organization_id")
            newer["records"][0]["baseServerRevision"]=1
            denied=await push(headers_a,newer)
            assert denied["rejectedRecordIDs"]==["overwrite-"+nonce]
            async with SessionFactory() as db:
                rows=[await db.get(SyncEntity,(tenant,"customer",shared_id))
                      for tenant in orgs]
                assert all(row is not None for row in rows)
                assert rows[0].payload_json["marker"]=="private-A"
                assert rows[1].payload_json["marker"]=="private-B"
                a_canonical=await db.get(CanonicalCustomer,(company_a,shared_id))
                b_canonical=await db.get(CanonicalCustomer,(company_b,shared_id))
                assert a_canonical is not None and b_canonical is not None
            # Forged query tenant is ignored by the authenticated pull.
            forged_pull=await c.get(
                "/api/v2/sync/pull?cursor=seq:0&organizationID="+company_b,
                headers=headers_a)
            assert forged_pull.status_code==200
            assert len(forged_pull.json()["records"])==1
            assert forged_pull.json()["records"][0]["payload"]["marker"]=="private-A"
            assert (await c.get("/api/v2/sync/pull")).status_code==401
    finally:
        await cleanup(orgs)
