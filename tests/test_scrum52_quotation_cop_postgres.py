"""SCRUM-52: one 20-row synthetic COP quote against canonical PostgreSQL ownership.

All customer/item identifiers and prices here are disposable and synthetic.
"""
import copy
import hashlib
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
    AuthSession, CanonicalCustomer, CanonicalItem, CanonicalProject,
    CanonicalProjectChild, CanonicalProjectSector, Membership,
    Organization, SyncChangeLog, SyncEntity, SyncMutation, User,
)
from app.settings import settings
from test_scrum52_cop_reconciliation import synthetic_quote

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="disposable PostgreSQL 17 / Alembic 0008 required",
)
ADMIN = "synthetic-estimator-quote-admin"


async def cleanup(org: str):
    async with SessionFactory() as db:
        users=(await db.scalars(select(Membership.user_id).where(
            Membership.organization_id == org))).all()
        for model in (SyncChangeLog, SyncEntity, SyncMutation,
                      SyncMutationFingerprint, CanonicalProjectChild,
                      CanonicalItem, CanonicalProjectSector, CanonicalProject,
                      CanonicalCustomer, AuthSession, Membership):
            await db.execute(delete(model).where(model.organization_id == org))
        await db.flush()
        if users:
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.execute(delete(Organization).where(Organization.id == org))
        await db.commit()


@pytest.mark.asyncio
async def test_twenty_line_synthetic_quote_is_reconciled_before_sync_mutation(monkeypatch):
    nonce=uuid4().hex[:14]
    company="synthetic-quote-company-"+nonce
    customer="synthetic-customer-"+nonce
    project="synthetic-project-"+nonce
    sector="synthetic-sector-"+nonce
    monkeypatch.setattr(settings,"admin_api_key_hash",hashlib.sha256(ADMIN.encode()).hexdigest())
    await cleanup(company)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
            identity=dict(
                organizationID=company,organizationName="Synthetic Quote Company",
                userID="user-"+nonce,displayName="Synthetic Estimator",
                email=nonce+"@example.invalid",
                membershipID="membership-"+nonce,
                active=True,sharedCompanyAccess=True,
                allCustomers=True,allProjects=True,
                roles=[{"id":"sales","displayName":"Sales"}],
                customerIDs=[],projectIDs=[],
                capabilities=["sync","quotation.create"],
                pilotSeatLimit=2,sessionID="session-"+uuid4().hex,
                sessionTTLSeconds=3600,
            )
            auth=await client.post(
                "/internal/admin/v1/bootstrap",
                headers={"X-Vitrial-Admin-Key":ADMIN},json=identity)
            assert auth.status_code==200,auth.text
            header={"Authorization":"Bearer "+auth.json()["accessToken"]}
            quote=synthetic_quote()
            quote["id"]="synthetic-quote-"+nonce
            quote["projectID"]=project
            quote["customerID"]=customer
            async with SessionFactory() as db:
                db.add(CanonicalCustomer(organization_id=company,customer_id=customer))
                await db.flush()
                db.add(CanonicalProject(organization_id=company,
                                        project_id=project,customer_id=customer))
                await db.flush()
                db.add(CanonicalProjectSector(
                    organization_id=company,
                    project_sector_id=sector,
                    project_id=project,
                    sector_id="sector-aluminum-glass-steel"))
                await db.flush()
                for row in quote["lines"]:
                    db.add(CanonicalItem(
                        organization_id=company,
                        item_id=row["itemID"],
                        project_id=project,
                        project_sector_id=sector))
                await db.commit()
            def push_payload(payload:dict, marker:str, base_revision=None):
                rec={
                    "id":"record-"+nonce+"-"+marker,
                    "entityType":"quotation",
                    "entityID":quote["id"],
                    "updatedAt":datetime.now(timezone.utc).isoformat(),
                    "payload":payload,
                    "clientMutationID":"mutation-"+nonce+"-"+marker,
                }
                if base_revision is not None:
                    rec["baseServerRevision"]=base_revision
                return {"protocolVersion":2,"deviceID":"synthetic-estimator",
                        "records":[rec]}
            # Before any quote is saved, bad subtotal must be rejected.
            bad=copy.deepcopy(quote)
            bad["groupSubtotalsCOP"]["1"] += 1
            response=await client.post("/api/v2/sync/push",headers=header,
                json=push_payload(bad,"bad-subtotal"))
            assert response.status_code==200,response.text
            assert response.json()["rejectedRecordIDs"]==[
                "record-"+nonce+"-bad-subtotal"]
            accepted=await client.post("/api/v2/sync/push",headers=header,
                json=push_payload(quote,"accepted"))
            assert accepted.status_code==200,accepted.text
            assert accepted.json()["acceptedRecordIDs"]==[
                "record-"+nonce+"-accepted"]
            async with SessionFactory() as db:
                saved=await db.get(SyncEntity,(company,"quotation",quote["id"]))
                assert saved is not None
                assert saved.payload_json["commercialTotalCOP"]==quote["commercialTotalCOP"]
                assert len(saved.payload_json["lines"])==20
                assert len(saved.payload_json["groupSubtotalsCOP"])==16
                revision=saved.server_revision
            tampered=copy.deepcopy(quote)
            tampered["lines"][2]["quantity"]=1
            result=await client.post("/api/v2/sync/push",headers=header,
                json=push_payload(tampered,"tampered",base_revision=revision))
            assert result.status_code==200
            assert result.json()["rejectedRecordIDs"]==[
                "record-"+nonce+"-tampered"]
            pulled=await client.get("/api/v2/sync/pull?cursor=seq:0",headers=header)
            assert pulled.status_code==200
            records=[x for x in pulled.json()["records"]
                     if x["entityType"]=="quotation" and x["entityID"]==quote["id"]]
            assert len(records)==1
            assert records[0]["payload"]["commercialTotalCOP"]==quote["commercialTotalCOP"]
            assert (await client.get("/api/v2/sync/pull")).status_code==401
    finally:
        await cleanup(company)
