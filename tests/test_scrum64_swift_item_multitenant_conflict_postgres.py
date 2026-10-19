"""Real Swift Item V1 wire: two organizations, conflicting editors, replay, revocation.

PostgreSQL-only. Uses synthetic identities and previously verified Swift JSON bytes.
No production services, customer quotes or supplier authority.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import SessionFactory
from app.main import app
from app.models import CanonicalCustomer, CanonicalProject, CanonicalProjectSector, SyncEntity
from app.settings import settings
from test_scrum51_swift_v1_wire_postgres import cleanup

FIXTURE = "VITRIAL_SYNTHETIC_SWIFT_ITEM_V1_BATCH_FILE"
ADMIN_KEY = "synthetic-multitenant-lease-acceptance"
pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1" or not os.getenv(FIXTURE),
    reason="disposable PostgreSQL and synthetic Swift-generated fixture required",
)


def make_wire(
    fixture: dict, *,
    mutation_id: str,
    base_revision: int | None = None,
    location: str = "Sala",
    item_id: str = "synthetic-item",
) -> dict:
    """Retain the exact Swift envelope/payload shape, updating only test fields."""
    wire = copy.deepcopy(fixture)
    record = wire["records"][0]
    item = json.loads(base64.b64decode(record["payload"], validate=True))
    assert item["id"] == item_id
    assert item["projectID"] == "synthetic-project"
    assert item["jobItemDraft"]["templateReviewStatus"] == "pending_customer_review"
    item["location"] = location
    item["jobItemDraft"]["location"] = location
    record["id"] = mutation_id
    record["clientMutationID"] = mutation_id
    record["payload"] = base64.b64encode(
        json.dumps(item, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    record.pop("baseServerRevision", None)
    if base_revision is not None:
        record["baseServerRevision"] = base_revision
    assert "serverRevision" not in record
    return wire


async def bootstrap(client: AsyncClient, *, org: str, name: str,
                    session_id: str, suffix: str) -> dict[str, str]:
    response = await client.post(
        "/internal/admin/v1/bootstrap",
        headers={"X-Vitrial-Admin-Key": ADMIN_KEY},
        json={
            "organizationID": org, "organizationName": "Synthetic " + org,
            "userID": f"user-{suffix}", "displayName": name,
            "email": f"{suffix}@example.invalid",
            "membershipID": f"member-{suffix}", "sessionID": session_id,
            "sessionTTLSeconds": 3600, "pilotSeatLimit": 3,
            "active": True, "sharedCompanyAccess": True,
            "allCustomers": True, "allProjects": True,
            "customerIDs": [], "projectIDs": [], "roles": [],
            "capabilities": ["sync", "item.create", "item.edit"],
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["accessToken"]}


async def seed_company(org: str) -> None:
    # The Item/project IDs intentionally collide across companies.
    async with SessionFactory() as db:
        db.add(CanonicalCustomer(organization_id=org,
                                 customer_id="synthetic-customer"))
        await db.flush()
        db.add(CanonicalProject(organization_id=org,
                                project_id="synthetic-project",
                                customer_id="synthetic-customer"))
        await db.flush()
        db.add(CanonicalProjectSector(
            organization_id=org, project_sector_id="synthetic-sector",
            project_id="synthetic-project",
            sector_id="sector-aluminum-glass-steel",
        ))
        await db.commit()


def pulled_item(batch: dict) -> tuple[dict, int]:
    items = [record for record in batch["records"]
             if record["entityType"] == "item"
             and record["entityID"] == "synthetic-item"]
    assert items, "the scoped company must receive its own Item"
    # A full-history pull can enumerate the same current entity from
    # successive change-log entries. Explicitly check this contract instead
    # of silently discarding duplicates as a fake single-record response.
    sequences = [int(record["id"].removeprefix("server:"))
                 for record in items]
    assert sequences == sorted(set(sequences))
    contents = [
        json.loads(base64.b64decode(record["payload"], validate=True))
        for record in items
    ]
    # Select the latest sequence rather than asserting that historical
    # entries must mirror the latest state. The current backend may return
    # repeated current-state records; a future true historical replay may
    # instead carry different snapshots or revisions.
    assert items[-1]["serverRevision"] > 0
    return contents[-1], items[-1]["serverRevision"]


@pytest.mark.asyncio
async def test_two_company_swift_item_conflict_replay_and_session_revocation(monkeypatch):
    fixture = json.loads(Path(os.environ[FIXTURE]).read_bytes())
    assert fixture["records"][0]["entityType"] == "item"
    assert "serverRevision" not in fixture["records"][0]
    nonce = uuid4().hex[:12]
    alpha, beta = "synthetic-alpha-" + nonce, "synthetic-beta-" + nonce
    monkeypatch.setattr(settings, "admin_api_key_hash",
                        hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            owner = await bootstrap(client, org=alpha, name="Alpha Owner",
                                    suffix="owner-" + nonce,
                                    session_id="sess-owner-" + nonce)
            outsider = await bootstrap(client, org=beta, name="Beta Owner",
                                       suffix="outside-" + nonce,
                                       session_id="sess-outside-" + nonce)
            await seed_company(alpha)
            await seed_company(beta)

            # Even the same Item ID AND client mutation ID must be independent
            # when authenticated by different companies.
            shared_id = "same-swift-mutation-" + nonce
            alpha_wire = make_wire(fixture, mutation_id=shared_id,
                                   location="Alpha-only state")
            beta_wire = make_wire(fixture, mutation_id=shared_id,
                                  location="Beta-only state")
            a = await client.post("/api/v1/sync/push", json=alpha_wire,
                                  headers=owner)
            b = await client.post("/api/v1/sync/push", json=beta_wire,
                                  headers=outsider)
            assert a.status_code == b.status_code == 200
            assert a.json()["acceptedRecordIDs"] == [shared_id], a.text
            assert b.json()["acceptedRecordIDs"] == [shared_id], b.text

            a_read = await client.get("/api/v1/sync/pull?cursor=seq:0",
                                      headers=owner)
            b_read = await client.get("/api/v1/sync/pull?cursor=seq:0",
                                      headers=outsider)
            assert a_read.status_code == b_read.status_code == 200
            a_item, old_revision = pulled_item(a_read.json())
            b_item, b_revision = pulled_item(b_read.json())
            assert a_item["location"] == "Alpha-only state"
            assert b_item["location"] == "Beta-only state"
            assert a_item["jobItemDraft"]["opening"]["legsCm"] == [
                124.25, 163.75]
            assert b_item["jobItemDraft"]["opening"]["legsCm"] == [
                124.25, 163.75]
            assert old_revision == 1 and b_revision == 1

            # Supplying an explicit foreign company hint must never make
            # an authenticated Beta record writable under Alpha.
            forged_tenant = make_wire(
                fixture, mutation_id="beta-spoof-" + nonce,
                base_revision=b_revision, location="Cross-tenant injection",
            )
            forged_item = json.loads(base64.b64decode(
                forged_tenant["records"][0]["payload"], validate=True))
            forged_item["organizationID"] = alpha
            forged_tenant["records"][0]["payload"] = base64.b64encode(
                json.dumps(forged_item).encode()).decode()
            denied_hint = await client.post(
                "/api/v1/sync/push", json=forged_tenant, headers=outsider
            )
            assert denied_hint.status_code == 200
            assert denied_hint.json()["acceptedRecordIDs"] == []
            assert denied_hint.json()["rejectedRecordIDs"] == [
                "beta-spoof-" + nonce]
            forged_read = await client.get(
                "/api/v1/sync/pull?cursor=seq:0&organizationID=" + alpha,
                headers=outsider,
            )
            assert forged_read.status_code == 200
            hinted_item, hinted_revision = pulled_item(forged_read.json())
            assert hinted_item["location"] == "Beta-only state"
            assert hinted_revision == b_revision

            # A second authenticated editor in Alpha takes a stale offline
            # snapshot before Owner's competing update is committed.
            editor_session = "sess-editor-" + nonce
            editor = await bootstrap(client, org=alpha, name="Alpha Editor",
                                     suffix="editor-" + nonce,
                                     session_id=editor_session)
            owner_edit = make_wire(fixture, mutation_id="owner-new-" + nonce,
                                   base_revision=old_revision,
                                   location="Owner newer online edit")
            owner_result = await client.post("/api/v1/sync/push",
                                             json=owner_edit, headers=owner)
            assert owner_result.status_code == 200
            assert owner_result.json()["acceptedRecordIDs"] == [
                "owner-new-" + nonce], owner_result.text
            now = await client.get("/api/v1/sync/pull?cursor=seq:0",
                                   headers=owner)
            online_item, fresh_revision = pulled_item(now.json())
            assert online_item["location"] == "Owner newer online edit"
            assert fresh_revision > old_revision

            stale = make_wire(fixture, mutation_id="editor-stale-" + nonce,
                              base_revision=old_revision,
                              location="Stale editor offline edit")
            stale_response = await client.post("/api/v1/sync/push",
                                               json=stale, headers=editor)
            assert stale_response.status_code == 200
            assert stale_response.json()["acceptedRecordIDs"] == []
            assert stale_response.json()["rejectedRecordIDs"] == [
                "editor-stale-" + nonce]
            # The rejected mutation ID is spent; it cannot be reused with
            # a corrected base revision or another payload.
            retry_rejected = await client.post("/api/v1/sync/push",
                                               json=stale, headers=editor)
            assert retry_rejected.json()["rejectedRecordIDs"] == [
                "editor-stale-" + nonce]
            collision = make_wire(fixture, mutation_id="editor-stale-" + nonce,
                                  base_revision=fresh_revision,
                                  location="Illegitimate mutation ID reuse")
            collision_result = await client.post("/api/v1/sync/push",
                                                  json=collision,
                                                  headers=editor)
            assert collision_result.json()["rejectedRecordIDs"] == [
                "editor-stale-" + nonce]
            after_stale = await client.get(
                "/api/v1/sync/pull?cursor=seq:0", headers=owner
            )
            unchanged, unchanged_revision = pulled_item(after_stale.json())
            assert unchanged["location"] == "Owner newer online edit"
            assert unchanged_revision == fresh_revision

            # The editor may explicitly rebase to a new mutation identity
            # and the verified current server revision.
            rebased = make_wire(fixture, mutation_id="editor-rebased-" + nonce,
                                base_revision=fresh_revision,
                                location="Rebased editor accepted")
            successful = await client.post("/api/v1/sync/push",
                                            json=rebased, headers=editor)
            assert successful.status_code == 200
            assert successful.json()["acceptedRecordIDs"] == [
                "editor-rebased-" + nonce], successful.text
            assert successful.json()["rejectedRecordIDs"] == []

            # Revocation closes both read and write for the editor.
            revoke = await client.post(
                f"/internal/admin/v1/sessions/{editor_session}/revoke",
                headers={"X-Vitrial-Admin-Key": ADMIN_KEY},
                json={"organizationID": alpha},
            )
            assert revoke.status_code == 200
            blocked_read = await client.get("/api/v1/sync/pull",
                                            headers=editor)
            assert blocked_read.status_code == 401
            after = await client.get("/api/v1/sync/pull?cursor=seq:0",
                                     headers=owner)
            final_alpha, accepted_revision = pulled_item(after.json())
            assert final_alpha["location"] == "Rebased editor accepted"
            blocked_push = await client.post(
                "/api/v1/sync/push", headers=editor,
                json=make_wire(fixture,
                    mutation_id="revoked-editor-" + nonce,
                    base_revision=accepted_revision,
                    location="Must not be accepted"),
            )
            assert blocked_push.status_code == 401

            # Two concurrent owner writes from one identical base revision
            # may not both win. The organization row lock serializes them
            # before optimistic revision checks, preserving one current Item.
            race_a = make_wire(
                fixture, mutation_id="owner-race-a-" + nonce,
                base_revision=accepted_revision, location="Race winner A"
            )
            race_b = make_wire(
                fixture, mutation_id="owner-race-b-" + nonce,
                base_revision=accepted_revision, location="Race winner B"
            )
            race_results = await asyncio.gather(
                client.post("/api/v1/sync/push", json=race_a, headers=owner),
                client.post("/api/v1/sync/push", json=race_b, headers=owner),
            )
            assert all(response.status_code == 200 for response in race_results)
            assert sorted(len(r.json()["acceptedRecordIDs"]) for r in race_results) == [0, 1]
            assert sorted(len(r.json()["rejectedRecordIDs"]) for r in race_results) == [0, 1]
            final_read = await client.get(
                "/api/v1/sync/pull?cursor=seq:0", headers=owner
            )
            race_item, race_revision = pulled_item(final_read.json())
            assert race_item["location"] in {"Race winner A", "Race winner B"}
            assert race_revision == accepted_revision + 1

            # Tenant B's identical Item IDs are still unaffected by any
            # Alpha updates or revocations.
            beta_after = await client.get("/api/v1/sync/pull?cursor=seq:0",
                                          headers=outsider)
            beta_item, beta_last_revision = pulled_item(beta_after.json())
            assert beta_item["location"] == "Beta-only state"
            assert beta_last_revision == b_revision
            async with SessionFactory() as db:
                left = await db.get(SyncEntity, (alpha, "item", "synthetic-item"))
                right = await db.get(SyncEntity, (beta, "item", "synthetic-item"))
                assert left.payload_json["location"] == race_item["location"]
                assert left.server_revision == race_revision
                assert right.payload_json["location"] == "Beta-only state"
    finally:
        for org in [alpha, beta]:
            await cleanup(org)
