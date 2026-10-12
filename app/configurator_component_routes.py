"""SCRUM-44 read-only, authenticated component-calculation preview.

Only published, organization-scoped *authoritative* reference data can supply
rules and component references. User input is never a technical rulebook.
No database updates, production release, supplier purchase, or quotation writes.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, current_principal
from app.db import session_scope
from app.ownership import AuthorizationRejected, require_item_access
from app.reference_models import ReferencePublication
from app.reference_data import ReferencePublicationPayload
from app.schemas import StrictModel
from app.configurator_component_calculator import (
    AuthorizedQuantityOverride,
    CalculationInput, CalculationReceipt, TrustedCatalogEntry,
    TrustedQuantityRule, calculate_components,
)

router = APIRouter(prefix="/api/v1/items", tags=["item-configurator"])


class ComponentCatalogSnapshot(StrictModel):
    id: str
    name: str
    category: str
    unit: Literal["each", "panel", "m", "m2"]
    active: bool
    itemTypeIDs: list[str]
    systemIDs: list[str]
    supplierID: str | None = None
    sku: str | None = None


class ComponentCalculationPreviewResponse(StrictModel):
    schemaVersion: Literal["vitrial.item-component-preview.v1"] = "vitrial.item-component-preview.v1"
    authorization: Literal["read_only_preview"] = "read_only_preview"
    # Explicit caller selection is not evidence of the mandatory BOM subset.
    selectionCoverage: Literal["explicit_selection_only"] = "explicit_selection_only"
    catalogContentSHA256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rulesContentSHA256: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalogEntries: list[ComponentCatalogSnapshot]
    calculation: CalculationReceipt


class ComponentCalculationPreviewRequest(StrictModel):
    configuration: CalculationInput
    catalogPublicationID: str = Field(min_length=1, max_length=256)
    catalogContentSHA256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rulesPublicationID: str = Field(min_length=1, max_length=256)
    rulesContentSHA256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overrides: list[AuthorizedQuantityOverride] = Field(default_factory=list, max_length=256)


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
            # Not every reference entry is a quantifiable component.
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


def _catalog_snapshots(
    payload: ReferencePublicationPayload,
    trusted: list[TrustedCatalogEntry],
) -> list[ComponentCatalogSnapshot]:
    source_by_id = {entry.id: entry for entry in payload.entries}
    return [
        ComponentCatalogSnapshot(
            id=entry.id,
            name=entry.name,
            category=source_by_id[entry.id].kind,
            unit=entry.unit,
            active=entry.active,
            itemTypeIDs=sorted(entry.itemTypeIDs),
            systemIDs=sorted(entry.systemIDs),
            sku=source_by_id[entry.id].vendorSKU,
            supplierID=source_by_id[entry.id].attributes.get("supplierID"),
        )
        for entry in trusted
    ]


@router.post(
    "/{item_id}/component-calculation/preview",
    response_model=ComponentCalculationPreviewResponse,
    operation_id="previewItemComponents",
)
async def preview_item_components(
    item_id: Annotated[str, Path(min_length=1, max_length=256)],
    request: ComponentCalculationPreviewRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(session_scope),
) -> ComponentCalculationPreviewResponse:
    if request.configuration.itemID != item_id:
        raise HTTPException(400, "Configuration Item identity does not match request path.")
    try:
        await require_item_access(db, principal, item_id)
    except AuthorizationRejected as exc:
        # Avoid leaking whether a different organization's Item exists.
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
    # A malformed approved publication is a configuration conflict, not a 500
    # and certainly not a reason to invent missing references.
    selected_ids = set(request.configuration.selectedComponentCatalogIDs)
    try:
        snapshots = _catalog_snapshots(
            catalog_payload, [entry for entry in catalog if entry.id in selected_ids]
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(409, "Published component references are invalid.") from exc
    return ComponentCalculationPreviewResponse(
        catalogContentSHA256=catalog_publication.content_sha256,
        rulesContentSHA256=rules_publication.content_sha256,
        catalogEntries=snapshots,
        calculation=calculation,
    )
