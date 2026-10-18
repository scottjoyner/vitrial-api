"""SCRUM-64 HTTP cross-company pricing/catalog isolation and cache privacy.

Uses only synthetic identities, 2 companies and a disposable PostgreSQL 17 DB.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db import SessionFactory
from app.main import app
from app.models import AuthSession, Membership, Organization, User
from app.reference_models import ReferencePublication
from app.settings import settings
from app.auth import token_hash

pytestmark = pytest.mark.skipif(
    os.environ.get("POSTGRES_INTEGRATION") != "1",
    reason="requires an isolated migrated PostgreSQL database",
)
ADMIN_KEY = "synthetic-scrum64-price-catalog-authority"

async def cleanup(org_ids):
    async with SessionFactory() as db:
        users=(await db.scalars(select(Membership.user_id).where(
            Membership.organization_id.in_(org_ids)))).all()
        await db.execute(delete(ReferencePublication).where(
            ReferencePublication.organization_id.in_(org_ids)))
        await db.execute(delete(AuthSession).where(AuthSession.organization_id.in_(org_ids)))
        await db.execute(delete(Membership).where(Membership.organization_id.in_(org_ids)))
        if users:
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.execute(delete(Organization).where(Organization.id.in_(org_ids)))
        await db.commit()


@pytest.mark.asyncio
async def test_two_company_pricebook_same_identifier_is_private_and_scoped(monkeypatch):
    suffix=uuid4().hex[:14]
    company_a="synthetic-price-A-"+suffix
    company_b="synthetic-price-B-"+suffix
    companies=(company_a,company_b)
    shared_pub_id="shared-price-version-"+suffix
    unique_a="a-only-catalog-"+suffix
    monkeypatch.setattr(settings,"admin_api_key_hash",hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
    await cleanup(companies)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
            async def provision(org,who,capabilities):
                body={
                    "organizationID":org,"organizationName":"Synthetic Company",
                    "userID":"user-"+suffix+"-"+who,
                    "displayName":"Synthetic "+who,
                    "email":who+suffix+"@example.invalid",
                    "membershipID":"member-"+suffix+"-"+who,
                    "active":True,"sharedCompanyAccess":True,
                    "allCustomers":True,"allProjects":True,
                    "customerIDs":[],"projectIDs":[],
                    "roles":[{"id":"synthetic","displayName":"Synthetic"}],
                    "capabilities":capabilities,"pilotSeatLimit":4,
                    "sessionID":"session-"+uuid4().hex,
                    "sessionTTLSeconds":3600}
                resp=await c.post("/internal/admin/v1/bootstrap",
                    headers={"X-Vitrial-Admin-Key":ADMIN_KEY},json=body)
                assert resp.status_code==200,resp.text
                return {"Authorization":"Bearer "+resp.json()["accessToken"]}

            a=await provision(company_a,"publisher-A",["sync","pricing.manage","catalog.manage"])
            b=await provision(company_b,"publisher-B",["sync","pricing.manage","catalog.manage"])
            observer=await provision(company_a,"observer",["sync"])
            root="/api/v1/reference-data"
            def private(response):
                assert response.headers["cache-control"]=="private, no-store"
                assert response.headers["x-content-type-options"]=="nosniff"

            # Both organizations are seeded independently with baseline
            # catalogs, rules and a deliberately non-authoritative price book.
            for headers in (a,b,observer):
                response=await c.get(root+"/manifest",headers=headers)
                assert response.status_code==200,response.text
                private(response)
                assert {p["kind"] for p in response.json()["current"]}=={
                    "catalog","compatibility_rules","price_book"}
            now=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
            def price_request(amount):
                return {
                    "publicationID":shared_pub_id,"kind":"price_book",
                    "versionID":shared_pub_id,"effectiveFrom":now,
                    "supersedesPublicationID":"price-book-unconfigured-v1",
                    "payload":{
                        "source":"synthetic-private-tenant-test",
                        "authoritative":True,
                        "priceBookLines":[{
                            "id":"synthetic-test-line","itemTypeID":"item-type-window",
                            "description":"Synthetic private unit price",
                            "unitPriceMinor":amount,"currency":"COP"
                        }],
                    },
                }
            for headers,price in ((a,100000),(b,900000)):
                resp=await c.post(root+"/publications",
                    headers=headers,json=price_request(price))
                assert resp.status_code==201,resp.text
                private(resp)
                assert resp.json()["payload"]["priceBookLines"][0]["unitPriceMinor"]==price

            # Role manager for members is not automatically a pricing publisher.
            forbidden=await c.post(root+"/publications",
                headers=observer,json={
                    **price_request(777777),
                    "publicationID":"forbidden-"+suffix,
                    "versionID":"forbidden-"+suffix,
                })
            assert forbidden.status_code==403
            assert "pricing.manage" in forbidden.text
            # IDs overlap across tenants but authority never crosses.
            digests=[]
            for headers,price,foreign_price in (
                (a,100000,900000),(b,900000,100000)):
                for route in (
                    "/publications/"+shared_pub_id,
                    "/publications?kind=price_book",
                    "/current",
                ):
                    separator="&" if "?" in route else "?"
                    resp=await c.get(root+route+separator+"organizationID="+(
                        company_b if headers is a else company_a),headers=headers)
                    assert resp.status_code==200,resp.text
                    private(resp)
                    payload=resp.json()
                    publications=payload["publications"] if "publications" in payload else [payload]
                    match=[p for p in publications if p["publicationID"]==shared_pub_id]
                    assert len(match)==1
                    lines=match[0]["payload"]["priceBookLines"]
                    assert lines[0]["unitPriceMinor"]==price
                    assert lines[0]["unitPriceMinor"]!=foreign_price
                    if route.startswith("/publications/"):
                        digests.append(match[0]["contentSHA256"])
                manifest=await c.get(root+"/manifest?organizationID="+company_b,
                                     headers=headers)
                private(manifest)
                assert any(row["publicationID"]==shared_pub_id
                           for row in manifest.json()["current"])
            assert len(digests)==2 and digests[0]!=digests[1]

            # A unique publication in A cannot be guessed/fetched by B.
            own_catalog={
                "publicationID":unique_a,"kind":"catalog",
                "versionID":unique_a,"effectiveFrom":now,
                "supersedesPublicationID":"catalog-bundled-v2",
                "payload":{"source":"synthetic-catalog-a","authoritative":True},
            }
            posted=await c.post(root+"/publications",headers=a,json=own_catalog)
            assert posted.status_code==201,posted.text
            private(posted)
            own=await c.get(root+"/publications/"+unique_a,headers=a)
            foreign=await c.get(root+"/publications/"+unique_a,headers=b)
            assert own.status_code==200 and foreign.status_code==404
            private(own)
            for route in ("/manifest","/current","/publications"):
                r=await c.get(root+route,headers=b)
                assert r.status_code==200
                assert unique_a not in r.text
            assert (await c.get(root+"/manifest")).status_code==401
            # Revocation is server-side; request-supplied tenant IDs cannot
            # revive a token or give access to the other company's catalog.
            async with SessionFactory() as db:
                session=await db.scalar(select(AuthSession).where(
                    AuthSession.access_token_hash==token_hash(a["Authorization"][7:])))
                session.revoked_at=datetime.now(timezone.utc)
                await db.commit()
            assert (await c.get(root+"/current",headers=a)).status_code==401
    finally:
        await cleanup(companies)
