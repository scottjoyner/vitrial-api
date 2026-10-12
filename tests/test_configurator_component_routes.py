"""SCRUM-44: scoped published reference authority, pure read-only preview gates."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.auth import Principal
from app.configurator_component_calculator import (
    AuthorizedQuantityOverride, CalculationInput, SectionInput,
)
from app import configurator_component_routes as routes
from app.main import app


def _principal(org="org-1", capabilities=frozenset()):
    return Principal(
        user_id="user-1", organization_id=org, membership_id="member-1",
        session_id="session-1", authorization_revision=1,
        capabilities=capabilities, customer_ids=frozenset(),
        project_ids=frozenset(), all_customers=False, all_projects=False,
    )


def _request(*, item="item-1", catalog_id="catalog-1", rules_id="rules-1", overrides=()):
    return routes.ComponentCalculationPreviewRequest(
        configuration=CalculationInput(
            itemID=item, configurationID="config-1", configurationVersion=4,
            itemTypeID="window", aluminumSystemID="published-system",
            widthMM=950, heightMM=2620,
            sections=[SectionInput(id="left", type="sliding"),
                      SectionInput(id="right", type="sliding")],
            selectedComponentCatalogIDs=["published-roller"],
        ),
        catalogPublicationID=catalog_id,
        catalogContentSHA256="a" * 64,
        rulesPublicationID=rules_id,
        rulesContentSHA256="b" * 64,
        overrides=list(overrides),
    )


def _record(kind, version, digest, *, authoritative=True, quantity=2):
    if kind == "catalog":
        payload = {
            "source": "organization-approved", "authoritative": authoritative,
            "entries": [{
                "id": "published-roller", "kind": "hardware", "code": "R2",
                "name": "Published roller", "active": True,
                "attributes": {
                    "componentUnit": "each", "itemTypeIDs": ["window"],
                    "systemIDs": ["published-system"],
                },
            }],
        }
    else:
        payload = {
            "source": "organization-approved", "authoritative": authoritative,
            "compatibilityRules": [{
                "id": "published-rule-roller-v1", "itemTypeID": "window",
                "attributes": {"componentQuantityRule": {
                    "catalogEntryID": "published-roller", "systemID": "published-system",
                    "mode": "per_matching_section", "sectionType": "sliding",
                    "quantityPerOccurrence": quantity,
                }}
            }],
        }
    return SimpleNamespace(
        kind=kind, version_id=version, content_sha256=digest, payload_json=payload,
    )


class PublishedDb:
    def __init__(self, *, authoritative=True, bad_kind=False, wrong_org=False):
        self.records = {
            ("org-1", "catalog-1"): _record("catalog", "catalog-v1", "a" * 64,
                authoritative=authoritative),
            ("org-1", "rules-1"): _record(
                "catalog" if bad_kind else "compatibility_rules",
                "rules-v1", "b" * 64, authoritative=authoritative),
        }
        self.calls = []
        if wrong_org:
            self.records = {
                ("other-organization", pub_id): record
                for (_, pub_id), record in self.records.items()
            }

    async def get(self, model, key):
        self.calls.append(key)
        return self.records.get(key)


@pytest.fixture
def permitted(monkeypatch):
    access = AsyncMock(return_value=(object(), object()))
    monkeypatch.setattr(routes, "require_item_access", access)
    return access


@pytest.mark.asyncio
async def test_authenticated_authorized_preview_loads_only_org_published_rules(permitted):
    db = PublishedDb()
    result = await routes.preview_item_components(
        "item-1", _request(), principal=_principal(), db=db
    )
    assert result.authorization == "read_only_preview"
    assert result.schemaVersion == "vitrial.item-component-preview.v1"
    assert result.catalogContentSHA256 == "a" * 64
    assert result.rulesContentSHA256 == "b" * 64
    assert len(result.catalogEntries) == 1
    assert result.catalogEntries[0].category == "hardware"
    assert result.selectionCoverage == "explicit_selection_only"
    assert result.calculation.ready
    assert result.calculation.catalogVersionID == "catalog-v1"
    assert result.calculation.rulesVersionID == "rules-v1"
    assert result.calculation.components[0].quantity == 4
    assert result.calculation.components[0].sourceRuleID == "published-rule-roller-v1"
    assert result.calculation.components[0].itemID == "item-1"
    assert db.calls == [("org-1", "catalog-1"), ("org-1", "rules-1")]
    permitted.assert_awaited_once()


@pytest.mark.asyncio
async def test_cross_item_spoof_denied_before_db_or_published_rule_access(permitted):
    db = PublishedDb()
    with pytest.raises(HTTPException) as exception:
        await routes.preview_item_components(
            "different-item", _request(), principal=_principal(), db=db
        )
    assert exception.value.status_code == 400
    assert db.calls == []
    permitted.assert_not_awaited()


@pytest.mark.asyncio
async def test_item_scope_denial_returns_403_without_reading_catalog(monkeypatch):
    from app.ownership import AuthorizationRejected

    async def deny(*args):
        raise AuthorizationRejected("not in assigned customer/project")
    monkeypatch.setattr(routes, "require_item_access", deny)
    db = PublishedDb()
    with pytest.raises(HTTPException) as exc:
        await routes.preview_item_components(
            "item-1", _request(), principal=_principal(), db=db
        )
    assert exc.value.status_code == 403
    assert db.calls == []


@pytest.mark.asyncio
async def test_cross_org_publications_are_not_visible(permitted):
    with pytest.raises(HTTPException) as exc:
        await routes.preview_item_components(
            "item-1", _request(), principal=_principal(), db=PublishedDb(wrong_org=True)
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_digest_and_non_authoritative_pins_fail_closed(permitted):
    for db in [PublishedDb(authoritative=False), PublishedDb(bad_kind=True)]:
        with pytest.raises(HTTPException) as exc:
            await routes.preview_item_components(
                "item-1", _request(), principal=_principal(), db=db
            )
        assert exc.value.status_code == 409

    req = _request().model_copy(update={"rulesContentSHA256": "c" * 64})
    with pytest.raises(HTTPException) as exc:
        await routes.preview_item_components(
            "item-1", req, principal=_principal(), db=PublishedDb()
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_override_capability_must_be_derived_from_actual_session(permitted):
    override = AuthorizedQuantityOverride(
        catalogEntryID="published-roller", quantity=5,
        reason="Supervisor reviewed alternate roller quantity",
    )
    unauthorized = await routes.preview_item_components(
        "item-1", _request(overrides=[override]),
        principal=_principal(), db=PublishedDb()
    )
    assert not unauthorized.calculation.ready
    assert unauthorized.calculation.components == []
    authorized = await routes.preview_item_components(
        "item-1", _request(overrides=[override]),
        principal=_principal(capabilities=frozenset(["item.components.override"])),
        db=PublishedDb()
    )
    assert authorized.calculation.ready
    assert authorized.calculation.components[0].quantity == 5
    assert authorized.calculation.components[0].origin == "authorized_override"


def test_schema_rejects_client_supplied_catalog_rules_and_forged_trust():
    request = _request().model_dump(mode="json")
    request["trustedCatalog"] = [{"id": "rogue", "unit": "each"}]
    with pytest.raises(ValidationError):
        routes.ComponentCalculationPreviewRequest.model_validate(request)


def test_registered_preview_endpoint_uses_authorized_route():
    # FastAPI's lazy _IncludedRouter does not appear as a concrete APIRoute
    # until it composes OpenAPI; checking app.routes directly is incorrect.
    operation = app.openapi()["paths"][
        "/api/v1/items/{item_id}/component-calculation/preview"
    ]["post"]
    assert operation["operationId"] == "previewItemComponents"
    assert any(s.get("bearerAuth") == [] for s in operation.get("security", []))


def test_http_preview_enforces_token_and_rejects_client_rules(monkeypatch):
    """Exercise FastAPI dependency composition, not just direct Python functions."""
    from fastapi.testclient import TestClient
    from app.auth import current_principal
    from app.db import session_scope

    db = PublishedDb()
    monkeypatch.setattr(routes, "require_item_access", AsyncMock(return_value=(object(), object())))
    client = TestClient(app)

    # No bearer token must not reach an Item-scoped published catalog.
    app.dependency_overrides[session_scope] = lambda: db
    try:
        unauthorized = client.post(
            "/api/v1/items/item-1/component-calculation/preview",
            json=_request().model_dump(mode="json"),
        )
        assert unauthorized.status_code == 401
        assert db.calls == []

        app.dependency_overrides[current_principal] = lambda: _principal()
        valid = client.post(
            "/api/v1/items/item-1/component-calculation/preview",
            json=_request().model_dump(mode="json"),
        )
        assert valid.status_code == 200, valid.text
        output = valid.json()
        assert output["authorization"] == "read_only_preview"
        assert output["catalogContentSHA256"] == "a" * 64
        assert output["calculation"]["components"][0]["quantity"] == 4

        forged = _request().model_dump(mode="json")
        forged["trustedRules"] = [{"quantityPerOccurrence": 999999}]
        refused = client.post(
            "/api/v1/items/item-1/component-calculation/preview", json=forged
        )
        assert refused.status_code == 422
    finally:
        app.dependency_overrides.clear()
