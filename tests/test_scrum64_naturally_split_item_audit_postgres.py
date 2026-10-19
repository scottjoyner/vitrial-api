"""Synthetic Item + audit genuinely split by FastAPI V1 500-row PostgreSQL scan."""

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import SessionFactory
from app.main import app
from app.models import CanonicalCustomer, CanonicalProject, CanonicalProjectSector, SyncChangeLog
from app.settings import settings
from test_scrum51_swift_v1_wire_postgres import cleanup, ADMIN_KEY

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1"
    or not os.getenv("VITRIAL_SYNTHETIC_COMPLETE_ITEM_V1_BATCH_FILE"),
    reason="requires disposable PostgreSQL and explicitly generated synthetic Swift batch",
)


@pytest.mark.asyncio
async def test_actual_pg_scan_splits_swift_item_and_matching_audit(monkeypatch):
    fixture = Path(os.environ["VITRIAL_SYNTHETIC_COMPLETE_ITEM_V1_BATCH_FILE"])
    assert fixture.is_file() and not fixture.is_symlink()
    outgoing = json.loads(fixture.read_bytes())
    assert [r["entityType"] for r in outgoing["records"]] == [
        "item", "item_audit_event"
    ]
    original_item = json.loads(base64.b64decode(outgoing["records"][0]["payload"], validate=True))
    original_audit = json.loads(base64.b64decode(outgoing["records"][1]["payload"], validate=True))
    assert original_audit["after"] == original_item
    org = "synthetic-audit-org"
    monkeypatch.setattr(settings, "admin_api_key_hash",
                        hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
    await cleanup(org)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            identity = dict(
                organizationID=org, organizationName="Synthetic Scan Company",
                userID="synthetic-audit-user", displayName="Synthetic Estimator",
                email="split-audit@example.invalid", membershipID="synthetic-audit-member",
                sessionID="synthetic-audit-session", sessionTTLSeconds=3600,
                pilotSeatLimit=2, active=True, sharedCompanyAccess=True,
                allCustomers=True, allProjects=True, customerIDs=[],
                projectIDs=[], roles=[], capabilities=["sync", "item.create", "item.edit"],
            )
            auth = await client.post(
                "/internal/admin/v1/bootstrap",
                headers={"X-Vitrial-Admin-Key": ADMIN_KEY}, json=identity
            )
            assert auth.status_code == 200, auth.text
            bearer = {"Authorization": "Bearer " + auth.json()["accessToken"]}
            me = await client.get("/api/v1/auth/me", headers=bearer)
            assert me.status_code == 200
            assert me.json()["authorizationRevision"] == 1
            async with SessionFactory() as db:
                db.add(CanonicalCustomer(organization_id=org, customer_id="synthetic-customer"))
                await db.flush()
                db.add(CanonicalProject(organization_id=org,
                       project_id="synthetic-project", customer_id="synthetic-customer"))
                await db.flush()
                db.add(CanonicalProjectSector(organization_id=org,
                       project_sector_id="synthetic-sector", project_id="synthetic-project",
                       sector_id="sector-aluminum-glass-steel"))
                await db.commit()
            single = lambda record: {"deviceID": outgoing["deviceID"], "records": [record]}
            sent_item = await client.post(
                "/api/v1/sync/push", json=single(outgoing["records"][0]), headers=bearer
            )
            assert sent_item.status_code == 200, sent_item.text
            assert sent_item.json()["acceptedRecordIDs"] == [outgoing["records"][0]["id"]]
            # Disposable-test-only undeliverable entries. API scan cap = 500:
            # first pull sees Item + 499 skipped rows, second sees audit.
            async with SessionFactory() as db:
                db.add_all([
                    SyncChangeLog(
                        organization_id=org, entity_type="customer",
                        entity_id=f"synthetic-missing-{n:04d}",
                        server_revision=1,
                        client_mutation_id=f"synthetic-gap-{n:04d}",
                        operation="upsert",
                    ) for n in range(500)
                ])
                await db.commit()
            sent_audit = await client.post(
                "/api/v1/sync/push", json=single(outgoing["records"][1]), headers=bearer
            )
            assert sent_audit.status_code == 200, sent_audit.text
            assert sent_audit.json()["acceptedRecordIDs"] == [outgoing["records"][1]["id"]]
            first = await client.get("/api/v1/sync/pull?cursor=seq:0", headers=bearer)
            assert first.status_code == 200, first.text
            first_batch = first.json()
            assert [r["entityType"] for r in first_batch["records"]] == ["item"]
            assert first_batch["deviceID"] == "server"
            assert int(first_batch["cursor"][4:]) > int(first_batch["records"][0]["id"][7:])
            second = await client.get("/api/v1/sync/pull",
                params={"cursor": first_batch["cursor"]}, headers=bearer)
            assert second.status_code == 200, second.text
            second_batch = second.json()
            assert [r["entityType"] for r in second_batch["records"]] == ["item_audit_event"]
            assert int(second_batch["cursor"][4:]) > int(first_batch["cursor"][4:])
            returned_audit = json.loads(base64.b64decode(
                second_batch["records"][0]["payload"], validate=True
            ))
            assert returned_audit == original_audit
            terminal = await client.get("/api/v1/sync/pull",
                params={"cursor": second_batch["cursor"]}, headers=bearer)
            assert terminal.status_code == 200
            assert terminal.json()["records"] == []
            assert terminal.json()["cursor"] == second_batch["cursor"]
            assert (await client.get("/api/v1/sync/pull")).status_code == 401
            output = os.getenv("VITRIAL_SPLIT_HTTP_PULL_OUTPUT_DIR")
            if output:
                root = Path(output)
                assert root.is_dir() and not root.is_symlink()
                assert root.stat().st_mode & 0o077 == 0
                for n, response in enumerate((first, second), start=1):
                    target = root / f"real-fastapi-natural-split-page-{n}.json"
                    assert not target.exists() and not target.is_symlink()
                    fd = os.open(target, os.O_CREAT | os.O_EXCL |
                                 os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(response.content)
    finally:
        await cleanup(org)
