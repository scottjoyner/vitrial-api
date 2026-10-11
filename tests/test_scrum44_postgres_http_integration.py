"""SCRUM-44 authenticated HTTP preview against migrated PostgreSQL (synthetic references).

No live vendor quantities or manufacturing authority. Requires POSTGRES_INTEGRATION=1
and the usual isolated integration database; does not mutate production data.
"""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.auth import token_hash
from app.db import SessionFactory
from app.main import app
from app.models import (
    AuthSession, CanonicalCustomer, CanonicalItem, CanonicalProject,
    CanonicalProjectSector, Membership, Organization, SyncChangeLog,
    SyncEntity, SyncMutation, User,
)
from app.reference_data import (
    ReferencePublicationCreate, ReferencePublicationPayload,
    _insert_publication, publication_sha256,
)
from app.reference_models import ReferencePublication

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires a migrated isolated PostgreSQL integration database",
)


@pytest.mark.asyncio
async def test_scoped_real_session_and_published_rules_over_http():
    suffix = uuid4().hex
    org, user = f"scrum44-org-{suffix}", f"scrum44-user-{suffix}"
    member, session = f"scrum44-member-{suffix}", f"scrum44-session-{suffix}"
    customer, project = f"scrum44-customer-{suffix}", f"scrum44-project-{suffix}"
    sector, item = f"scrum44-sector-{suffix}", f"scrum44-item-{suffix}"
    token = f"isolated-scrum44-token-{suffix}"
    now = datetime.now(timezone.utc)
    catalog = ReferencePublicationCreate(
        publicationID="scrum44-catalog", versionID="scrum44-catalog-v1",
        kind="catalog", effectiveFrom=now,
        payload=ReferencePublicationPayload.model_validate({
            "source": "synthetic-integration-only", "authoritative": True,
            "entries": [{
                "id": "synthetic-roller", "kind": "hardware", "code": "TEST-ROLLER",
                "name": "NON-MANUFACTURING test roller",
                "attributes": {"componentUnit": "each",
                               "itemTypeIDs": ["window"], "systemIDs": ["test-system"]}
            }]
        }),
    )
    rules = ReferencePublicationCreate(
        publicationID="scrum44-rules", versionID="scrum44-rules-v1",
        kind="compatibility_rules", effectiveFrom=now,
        payload=ReferencePublicationPayload.model_validate({
            "source": "synthetic-integration-only", "authoritative": True,
            "compatibilityRules": [{
                "id": "synthetic-rule", "itemTypeID": "window",
                "attributes": {"componentQuantityRule": {
                    "catalogEntryID": "synthetic-roller", "systemID": "test-system",
                    "mode": "per_matching_section", "sectionType": "sliding",
                    "quantityPerOccurrence": 2,
                }}
            }]
        }),
    )
    request = {
        "catalogPublicationID": catalog.publicationID,
        "catalogContentSHA256": publication_sha256(catalog),
        "rulesPublicationID": rules.publicationID,
        "rulesContentSHA256": publication_sha256(rules),
        "configuration": {
            "itemID": item, "configurationID": "test-configuration",
            "configurationVersion": 4, "itemTypeID": "window",
            "aluminumSystemID": "test-system",
            "sections": [{"id": "s1", "type": "sliding"},
                         {"id": "s2", "type": "sliding"}],
            "selectedComponentCatalogIDs": ["synthetic-roller"],
        },
    }
    try:
        async with SessionFactory() as db:
            db.add(Organization(id=org, name="Isolated SCRUM44 Test"))
            db.add(User(id=user, display_name="SCRUM44 Integration Tester"))
            await db.flush()
            db.add(Membership(
                id=member, organization_id=org, user_id=user, active=True,
                all_customers=True, all_projects=True,
                customer_ids=[], project_ids=[], roles=[], capabilities=[],
            ))
            await db.flush()
            db.add(AuthSession(
                id=session, organization_id=org, membership_id=member, user_id=user,
                access_token_hash=token_hash(token),
                issued_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(minutes=30),
            ))
            db.add(CanonicalCustomer(organization_id=org, customer_id=customer))
            await db.flush()
            db.add(CanonicalProject(
                organization_id=org, project_id=project, customer_id=customer,
            ))
            await db.flush()
            db.add(CanonicalProjectSector(
                organization_id=org, project_sector_id=sector,
                project_id=project, sector_id="sector-aluminum-glass-steel",
            ))
            await db.flush()
            db.add(CanonicalItem(
                organization_id=org, item_id=item, project_id=project,
                project_sector_id=sector,
            ))
            await _insert_publication(db, org, catalog, published_by_user_id=user)
            await _insert_publication(db, org, rules, published_by_user_id=user)
            await db.commit()

        path = f"/api/v1/items/{item}/component-calculation/preview"
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://isolated-test") as client:
            assert (await client.post(path, json=request)).status_code == 401
            headers = {"Authorization": f"Bearer {token}"}
            success = await client.post(path, headers=headers, json=request)
            assert success.status_code == 200, success.text
            output = success.json()
            assert output["authorization"] == "read_only_preview"
            assert output["catalogContentSHA256"] == request["catalogContentSHA256"]
            assert output["rulesContentSHA256"] == request["rulesContentSHA256"]
            assert output["calculation"]["itemID"] == item
            assert output["calculation"]["configurationVersion"] == 4
            assert output["calculation"]["components"][0]["quantity"] == 4
            assert output["calculation"]["components"][0]["sourceRuleID"] == "synthetic-rule"
            assert len(output["catalogEntries"]) == 1
            mismatch = dict(request, rulesContentSHA256="c" * 64)
            assert (await client.post(path, headers=headers, json=mismatch)).status_code == 409
            foreign_path = "/api/v1/items/unowned-item/component-calculation/preview"
            foreign = dict(request, configuration=dict(request["configuration"],
                                                       itemID="unowned-item"))
            assert (await client.post(foreign_path, headers=headers, json=foreign)).status_code == 403
            forged = dict(request, trustedRules=[{"quantityPerOccurrence": 999}])
            assert (await client.post(path, headers=headers, json=forged)).status_code == 422
            # Read-only means no commercial or sync mutation, not merely HTTP 200.
            async with SessionFactory() as db:
                for model in (SyncEntity, SyncMutation, SyncChangeLog):
                    count = await db.scalar(select(func.count()).select_from(model).where(
                        model.organization_id == org
                    ))
                    assert count == 0, f"preview unexpectedly wrote {model.__tablename__}"
                stored = await db.get(ReferencePublication, (org, catalog.publicationID))
                assert stored is not None
                assert stored.content_sha256 == publication_sha256(catalog)
                session_row = await db.get(AuthSession, session)
                assert session_row is not None
                session_row.revoked_at = datetime.now(timezone.utc)
                await db.commit()
            assert (await client.post(path, headers=headers, json=request)).status_code == 401
    finally:
        async with SessionFactory() as db:
            for model, field, value in (
                (ReferencePublication, ReferencePublication.organization_id, org),
                (AuthSession, AuthSession.organization_id, org),
                (CanonicalItem, CanonicalItem.organization_id, org),
                (CanonicalProjectSector, CanonicalProjectSector.organization_id, org),
                (CanonicalProject, CanonicalProject.organization_id, org),
                (CanonicalCustomer, CanonicalCustomer.organization_id, org),
                (Membership, Membership.organization_id, org),
                (User, User.id, user),
                (Organization, Organization.id, org),
            ):
                await db.execute(delete(model).where(field == value))
            await db.commit()

