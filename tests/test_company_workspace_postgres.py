from __future__ import annotations
import hashlib
import os
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from app.db import SessionFactory
from app.main import app
from app.models import AuthSession, Membership, Organization, User
from app.settings import settings

pytestmark=pytest.mark.skipif(os.getenv("POSTGRES_INTEGRATION")!="1",reason="disposable migrated PostgreSQL required")
ADMIN_KEY="synthetic-company-seat-test-key"

def request(org, name, *, active=True, shared=True, limit=None):
    payload=dict(organizationID=org,organizationName="Synthetic Company",
        userID="user-"+name, displayName="Test "+name,email=name+"@example.invalid",
        membershipID="membership-"+name, active=active,sharedCompanyAccess=shared,
        allCustomers=False,allProjects=False,customerIDs=[],projectIDs=[],
        roles=[{"id":"sales","displayName":"Sales"}],
        capabilities=["sync","customer.create","project.create"],
        sessionID="session-"+uuid.uuid4().hex,sessionTTLSeconds=3600)
    if limit is not None:
        payload["pilotSeatLimit"]=limit
    return payload

async def cleanup(orgs):
    async with SessionFactory() as db:
        users=(await db.scalars(select(Membership.user_id).where(
            Membership.organization_id.in_(orgs)))).all()
        await db.execute(delete(AuthSession).where(AuthSession.organization_id.in_(orgs)))
        await db.execute(delete(Membership).where(Membership.organization_id.in_(orgs)))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.execute(delete(Organization).where(Organization.id.in_(orgs)))
        await db.commit()

@pytest.mark.asyncio
async def test_two_seats_organizational_boundary_revocation(monkeypatch):
    run=uuid.uuid4().hex[:12]
    a,b="test-company-a-"+run,"test-company-b-"+run
    monkeypatch.setattr(settings,"admin_api_key_hash",hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
    h={"X-Vitrial-Admin-Key":ADMIN_KEY}
    async def provision(client,org,who,**opts):
        return await client.post("/internal/admin/v1/bootstrap",headers=h,
            json=request(org,run+"-"+who,**opts))
    await cleanup([a,b])
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
            assert (await c.get("/api/v1/company/workspace")).status_code==401
            first=await provision(c,a,"a1",limit=2)
            assert first.status_code==200,first.text
            token1=first.json()["accessToken"]
            second=await provision(c,a,"a2")
            assert second.status_code==200,second.text
            token2=second.json()["accessToken"]
            assert (await provision(c,a,"a3")).status_code==409
            third_company=await provision(c,b,"b1",limit=4)
            assert third_company.status_code==200,third_company.text
            token_b=third_company.json()["accessToken"]
            async def workspace(token,extra=""):
                return await c.get("/api/v1/company/workspace"+extra,
                    headers={"Authorization":"Bearer "+token})
            for token in (token1,token2):
                r=await workspace(token)
                assert r.status_code==200,r.text
                d=r.json()
                assert r.headers["Cache-Control"]=="no-store"
                assert d["organizationID"]==a
                assert (d["seatLimit"],d["occupiedSeats"],d["seatsRemaining"])==(2,2,0)
                assert d["currentMemberHasCompanyCustomerAccess"] is True
                assert d["currentMemberHasCompanyProjectAccess"] is True
                assert len(d["members"])==2
                assert all(x["userID"].startswith("user-"+run+"-a") for x in d["members"])
            forged=await workspace(token1,"?organizationID="+b)
            assert forged.json()["organizationID"]==a
            outside=await workspace(token_b)
            assert outside.status_code==200
            assert outside.json()["organizationID"]==b
            assert outside.json()["occupiedSeats"]==1
            assert (await provision(c,a,"a1",limit=1)).status_code==409
            deactivate=await provision(c,a,"a2",active=False)
            assert deactivate.status_code==200,deactivate.text
            assert (await c.get("/api/v1/auth/me",headers={
                "Authorization":"Bearer "+token2})).status_code==401
            assert (await workspace(token1)).json()["occupiedSeats"]==1
            assert (await provision(c,a,"a3")).status_code==200
            assert (await provision(c,a,"a2")).status_code==409
            bad=request(a,run+"-a4")
            bad["customerIDs"]=["explicit-test-only-id"]
            assert (await c.post("/internal/admin/v1/bootstrap",
                headers=h,json=bad)).status_code==422
    finally:
        await cleanup([a,b])

@pytest.mark.asyncio
async def test_concurrent_provisioning_cannot_exceed_seat_limit(monkeypatch):
    import asyncio
    run=uuid.uuid4().hex[:12]
    org="test-company-race-"+run
    monkeypatch.setattr(settings,"admin_api_key_hash",hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
    admin={"X-Vitrial-Admin-Key":ADMIN_KEY}
    await cleanup([org])
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
            first=await c.post("/internal/admin/v1/bootstrap",
                headers=admin,json=request(org,run+"-start",limit=2))
            assert first.status_code==200,first.text
            async def seat(n):
                return await c.post("/internal/admin/v1/bootstrap",headers=admin,
                    json=request(org,run+"-concurrent-"+n))
            left,right=await asyncio.gather(seat("left"),seat("right"))
            assert sorted([left.status_code,right.status_code])==[200,409],(
                left.status_code,right.status_code)
            access=first.json()["accessToken"]
            workspace=await c.get("/api/v1/company/workspace",
                headers={"Authorization":"Bearer "+access})
            assert workspace.status_code==200,workspace.text
            assert workspace.json()["occupiedSeats"]==2
            assert workspace.json()["seatsRemaining"]==0
    finally:
        await cleanup([org])

@pytest.mark.asyncio
async def test_concurrent_first_company_creation_does_not_duplicate_org(monkeypatch):
    import asyncio
    run=uuid.uuid4().hex[:12]
    org="test-first-company-race-"+run
    monkeypatch.setattr(settings,"admin_api_key_hash",hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
    h={"X-Vitrial-Admin-Key":ADMIN_KEY}
    await cleanup([org])
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
            async def add_user(which):
                return await c.post("/internal/admin/v1/bootstrap",headers=h,
                    json=request(org,run+"-"+which,limit=2))
            left,right=await asyncio.gather(add_user("first"),add_user("second"))
            assert (left.status_code,right.status_code)==(200,200),(
                left.status_code,right.status_code)
            assert (await c.post("/internal/admin/v1/bootstrap",headers=h,
                json=request(org,run+"-third"))).status_code==409
            response=await c.get("/api/v1/company/workspace",headers={
                "Authorization":"Bearer "+left.json()["accessToken"]})
            assert response.status_code==200
            assert response.json()["occupiedSeats"]==2
            assert response.json()["seatLimit"]==2
    finally:
        await cleanup([org])
