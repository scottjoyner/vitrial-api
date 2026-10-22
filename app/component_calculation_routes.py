"""SCRUM-44 authoritative component calculation endpoint.

Server-owned, immutable calculation receipt for an Item Configuration.
Only accessible to principals with `item.components.authorize` capability.
The receipt is evidence for quotations, deliveries, and audits.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal
from app.component_calculation_model import ComponentCalculationReceipt
from app.configurator_component_calculator import (
    AuthorizedQuantityOverride,
    CalculationInput,
    CalculationReceipt,
    TrustedCatalogEntry,
    TrustedQuantityRule,
    calculate_components,
)
from app.db import session_scope
from app.ownership import AuthorizationRejected, require_item_access
from app.reference_data import ReferencePublicationPayload
from app.reference_models import ReferencePublication
from app.schemas import StrictModel

router = APIRouter(prefix="/api/v1/items", tags=["item-configurator"])


class AuthoritativeComponentCalculationRequest(StrictModel):
    configuration: CalculationInput
    catalogPublicationID: str = Field(min_length=1, max_length=256)
    catalogContentSHA256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rulesPublicationID: str = Field(min_length=1, max_length=256)
    rulesContentSHA256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overrides: list[AuthorizedQuantityOverride] = Field(default_factory=list, max_length=256)


class AuthoritativeComponentCalculationResponse(StrictModel):
    schemaVersion: Literal["vitrial.item-components.v1"] = "vitrial.item-components.v1"
    authorization: Literal["authoritative"] = "authoritative"
    receiptID: str
    itemID: str
    configurationID: str
    configurationVersion: int
    catalogPublicationID: str
    catalogVersionID: str
    catalogContentSHA256: str
    rulesPublicationID: str
    rulesVersionID: str
    rulesContentSHA256: str
    inputSHA256: str
    components: list[dict]
    issues: list[str]
    ready: bool
    issuedAt: datetime
    authorizationRevision: int


async def _publication(
    db: AsyncSession, principal: Principal, publication_id: str,
    expected_sha256: str, kind: str,
) -> tuple[ReferencePublication, ReferencePublicationPayload]:
    record = await db.get(ReferencePublication, (principal.organization_id, publication_id))
    if record is None or record.kind != kind:
        raise HTTPException(409, f"Required {kind} publication is unavailable.")
    if record.content_sha256 != expected_sha256:
        raise HTTPException(409, f"Required {kind} publication digest mismatch.")
    try:
        payload = ReferencePublicationPayload.model_validate(record.payload_json)
    except ValidationError as exc:
        raise HTTPException(409, f"Invalid {kind} publication schema.") from exc
    if not payload.authoritative:
        raise HTTPException(409, f"Required {kind} publication is not authoritative.")
    return record, payload


def _catalog(payload: ReferencePublicationPayload) -> list[TrustedCatalogEntry]:
    entries: list[TrustedCatalogEntry] = []
    technical_kinds = {
        "aluminum_profile", "glass_type", "glass_thickness",
        "hardware", "accessory", "vendor_material"
    }
    for record in payload.entries:
        if record.kind not in technical_kinds:
            continue
        unit = record.attributes.get("componentUnit")
        if unit is None:
            continue
        entries.append(TrustedCatalogEntry(
            id=record.id,
            name=record.name,
            unit=unit,
            active=record.active,
            itemTypeIDs=frozenset(record.attributes.get("itemTypeIDs", [])),
            systemIDs=frozenset(record.attributes.get("systemIDs", [])),
        ))
    return entries


def _rules(payload: ReferencePublicationPayload) -> list[TrustedQuantityRule]:
    rules: list[TrustedQuantityRule] = []
    for record in payload.compatibilityRules:
        detail = record.attributes.get("componentQuantityRule")
        if detail is None:
            continue
        if not isinstance(detail, dict):
            raise ValueError("Invalid published component quantity rule.")
        rules.append(TrustedQuantityRule(
            id=record.id,
            catalogEntryID=detail["catalogEntryID"],
            itemTypeID=record.itemTypeID,
            systemID=detail.get("systemID"),
            mode=detail["mode"],
            sectionType=detail.get("sectionType"),
            quantityPerOccurrence=detail["quantityPerOccurrence"],
        ))
    return rules


@router.post(
    "/{item_id}/component-calculation/authoritative",
    response_model=AuthoritativeComponentCalculationResponse,
    operation_id="authorizeItemComponents",
    status_code=201,
)
async def authorize_item_components(
    item_id: Annotated[str, Path(min_length=1, max_length=256)],
    request: AuthoritativeComponentCalculationRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> AuthoritativeComponentCalculationResponse:
    if "item.components.authorize" not in principal.capabilities:
        raise HTTPException(403, "Capability `item.components.authorize` required.")
    if request.configuration.itemID != item_id:
        raise HTTPException(400, "Configuration Item identity does not match request path.")
    try:
        await require_item_access(db, principal, item_id)
    except AuthorizationRejected as exc:
        raise HTTPException(403, "Item access denied.") from exc

    catalog_publication, catalog_payload = await _publication(
        db, principal, request.catalogPublicationID,
        request.catalogContentSHA256, "catalog"
    )
    rules_publication, rules_payload = await _publication(
        db, principal, request.rulesPublicationID,
        request.rulesContentSHA256, "compatibility_rules"
    )
    try:
        catalog = _catalog(catalog_payload)
        rules = _rules(rules_payload)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(409, "Published technical rules cannot be interpreted safely.") from exc

    calculation = calculate_components(
        request.configuration,
        trusted_catalog=catalog,
        trusted_rules=rules,
        catalog_version_id=catalog_publication.version_id,
        rules_version_id=rules_publication.version_id,
        overrides=request.overrides,
        override_authorized="item.components.override" in principal.capabilities,
    )

    receipt_id = f"calc-{calculation.inputSHA256[:16]}"
    existing = await db.get(ComponentCalculationReceipt, (principal.organization_id, receipt_id))
    if existing:
        raise HTTPException(409, "Authoritative calculation for this input already exists.")

    receipt = ComponentCalculationReceipt(
        organization_id=principal.organization_id,
        receipt_id=receipt_id,
        item_id=item_id,
        configuration_id=request.configuration.configurationID,
        configuration_version=request.configuration.configurationVersion,
        catalog_publication_id=request.catalogPublicationID,
        catalog_version_id=catalog_publication.version_id,
        catalog_content_sha256=request.catalogContentSHA256,
        rules_publication_id=request.rulesPublicationID,
        rules_version_id=rules_publication.version_id,
        rules_content_sha256=request.rulesContentSHA256,
        input_sha256=calculation.inputSHA256,
        components_json=[c.model_dump(mode="json") for c in calculation.components],
        issues_json=calculation.issues,
        issued_by_user_id=principal.user_id,
        authorization_revision=principal.authorization_revision,
    )
    db.add(receipt)
    await db.flush()

    return AuthoritativeComponentCalculationResponse(
        receiptID=receipt_id,
        itemID=item_id,
        configurationID=request.configuration.configurationID,
        configurationVersion=request.configuration.configurationVersion,
        catalogPublicationID=request.catalogPublicationID,
        catalogVersionID=catalog_publication.version_id,
        catalogContentSHA256=request.catalogContentSHA256,
        rulesPublicationID=request.rulesPublicationID,
        rulesVersionID=rules_publication.version_id,
        rulesContentSHA256=request.rulesContentSHA256,
        inputSHA256=calculation.inputSHA256,
        components=[c.model_dump(mode="json") for c in calculation.components],
        issues=calculation.issues,
        ready=calculation.ready,
        issuedAt=receipt.issued_at,
        authorizationRevision=principal.authorization_revision,
    )
