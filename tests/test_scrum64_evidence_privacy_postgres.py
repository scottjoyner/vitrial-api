"""SCRUM-64: company-isolated evidence bytes, no-store and no MIME sniffing.

Disposable migrated PostgreSQL + synthetic in-memory blob storage only.
"""
import hashlib
import os
import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select

from app.db import SessionFactory
from app.main import app
from app.models import (
    AuthSession, CanonicalCustomer, CanonicalItem, CanonicalProject,
    CanonicalProjectSector, EvidenceBlob, Membership, Organization, User,
)
from app.settings import settings

DB_ENABLED=os.environ.get("POSTGRES_INTEGRATION")=="1"
KEY="synthetic-evidence-isolation-key"


class MemoryStore:
    provider="local"
    def __init__(self, blobs: dict[str, bytes]):
        self.blobs=blobs
    async def exists(self,key:str)->bool:
        return key in self.blobs
    async def stream(self,key:str):
        yield self.blobs[key]


async def cleanup(org_ids):
    async with SessionFactory() as db:
        users=(await db.scalars(select(Membership.user_id).where(
            Membership.organization_id.in_(org_ids)))).all()
        for model in (EvidenceBlob, AuthSession, Membership):
            await db.execute(delete(model).where(model.organization_id.in_(org_ids)))
        await db.execute(delete(CanonicalItem).where(CanonicalItem.organization_id.in_(org_ids)))
        await db.execute(delete(CanonicalProjectSector).where(CanonicalProjectSector.organization_id.in_(org_ids)))
        await db.execute(delete(CanonicalProject).where(CanonicalProject.organization_id.in_(org_ids)))
        await db.execute(delete(CanonicalCustomer).where(CanonicalCustomer.organization_id.in_(org_ids)))
        await db.flush()
        if users:await db.execute(delete(User).where(User.id.in_(users)))
        await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))
        await db.commit()


@pytest.mark.skipif(not DB_ENABLED,reason="disposable migrated PostgreSQL required")
@pytest.mark.asyncio
async def test_company_evidence_headers_and_cross_tenant_bytes(monkeypatch):
    nonce=uuid.uuid4().hex[:14]
    org_a="synthetic-evidence-a-"+nonce
    org_b="synthetic-evidence-b-"+nonce
    doc="shared-document-"+nonce
    item="shared-item-"+nonce
    orgs=(org_a,org_b)
    from app import main as main_mod
    data={org_a:b"A:synthetic-private-evidence",
          org_b:b"B:synthetic-private-evidence"}
    objects={}
    monkeypatch.setattr(settings,"admin_api_key_hash",hashlib.sha256(KEY.encode()).hexdigest())
    monkeypatch.setattr(main_mod,"store_for_provider",lambda provider:MemoryStore(objects))
    await cleanup(orgs)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
            async def seat(org,label,all_projects=True):
                body={
                    "organizationID":org, "organizationName":"Synthetic Evidence",
                    "userID":"user-"+nonce+"-"+label,
                    "displayName":"Synthetic "+label,
                    "email":label+nonce+"@example.invalid",
                    "membershipID":"member-"+nonce+"-"+label,
                    "active":True,"sharedCompanyAccess":all_projects,
                    "allCustomers":all_projects,"allProjects":all_projects,
                    "roles":[{"id":"operator","displayName":"Operator"}],
                    "customerIDs":[],"projectIDs":[],
                    "capabilities":["sync","item.evidence.manage"],
                    "pilotSeatLimit":4,
                    "sessionID":"session-"+uuid.uuid4().hex,"sessionTTLSeconds":3600}
                result=await c.post("/internal/admin/v1/bootstrap",
                    headers={"X-Vitrial-Admin-Key":KEY},json=body)
                assert result.status_code==200,result.text
                return {"Authorization":"Bearer "+result.json()["accessToken"]}

            a=await seat(org_a,"a")
            b=await seat(org_b,"b")
            scoped=await seat(org_a,"scoped",all_projects=False)
            async with SessionFactory() as db:
                for org in orgs:
                    customer="cust-"+org
                    project="project-"+org
                    db.add(CanonicalCustomer(organization_id=org,customer_id=customer))
                    await db.flush()
                    db.add(CanonicalProject(organization_id=org,project_id=project,
                                            customer_id=customer))
                    await db.flush()
                    project_sector="sector-"+org
                    db.add(CanonicalProjectSector(
                        organization_id=org,project_sector_id=project_sector,
                        project_id=project,sector_id="sector-aluminum-glass-steel"))
                    await db.flush()
                    db.add(CanonicalItem(organization_id=org,item_id=item,
                                         project_id=project,project_sector_id=project_sector))
                    await db.flush()
                    payload=data[org]
                    name="memory/"+org
                    objects[name]=payload
                    db.add(EvidenceBlob(
                        organization_id=org,document_id=doc,item_id=item,
                        filename="synthetic.bin",mime_type="text/html",
                        sha256=hashlib.sha256(payload).hexdigest(),
                        size_bytes=len(payload),storage_provider="local",
                        object_key=name))
                await db.commit()
            url="/api/v1/sync/evidence-blobs/"+doc
            for headers,expected,forbidden in (
                (a,data[org_a],data[org_b]),(b,data[org_b],data[org_a])):
                head=await c.head(url,headers=headers)
                body=await c.get(url,headers=headers)
                assert head.status_code==200 and body.status_code==200
                assert body.content==expected and forbidden not in body.content
                assert body.headers["content-type"].startswith("text/html")
                assert body.headers["content-disposition"]=="attachment"
                assert body.headers["content-security-policy"]=="sandbox"
                for response in (head,body):
                    assert response.headers["cache-control"]=="private, no-store"
                    assert response.headers["x-content-type-options"]=="nosniff"
                    assert response.headers["x-content-sha256"]==hashlib.sha256(expected).hexdigest()
            for method in ("head","get"):
                denied=await getattr(c,method)(url,headers=scoped)
                assert denied.status_code==403
                assert data[org_a] not in denied.content
                assert data[org_b] not in denied.content
                unauth=await getattr(c,method)(url)
                assert unauth.status_code==401
                missing=await getattr(c,method)(url+"-unknown",headers=a)
                assert missing.status_code==404
            # Revoking the real server-side session invalidates the old bearer.
            async with SessionFactory() as db:
                from app.auth import token_hash
                session=await db.scalar(select(AuthSession).where(
                    AuthSession.access_token_hash==token_hash(a["Authorization"][7:])))
                from datetime import datetime,timezone
                session.revoked_at=datetime.now(timezone.utc)
                await db.commit()
            assert (await c.get(url,headers=a)).status_code==401
    finally:
        await cleanup(orgs)
