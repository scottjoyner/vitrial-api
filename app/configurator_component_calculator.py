"""SCRUM-44 read-only, server-owned, fail-closed internal component rule engine.

Technical rules and catalog entries must originate from trusted server publications,
not from the calling iOS client. This prototype does NOT issue manufacturing or
quotation authority and intentionally has no live endpoint or database writes.
"""
from __future__ import annotations
import hashlib
import json
from typing import Literal, Sequence
from pydantic import BaseModel, ConfigDict, Field, model_validator

Unit = Literal["each", "panel", "m", "m2"]
QuantityMode = Literal["per_item", "per_matching_section"]
Origin = Literal["calculated", "authorized_override"]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

class SectionInput(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64)

class VanoSegmentInput(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=256)
    widthMM: float = Field(gt=0)
    heightMM: float = Field(gt=0)
    stage: Literal["reported", "confirmed", "final"] = "reported"


class GlassPanelInput(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    sectionID: str = Field(min_length=1, max_length=128)
    glassTypeID: str | None = Field(default=None, max_length=128)
    thicknessID: str | None = Field(default=None, max_length=128)
    colorID: str | None = Field(default=None, max_length=128)
    treatmentID: str | None = Field(default=None, max_length=128)
    widthMM: float | None = Field(default=None, gt=0)
    heightMM: float | None = Field(default=None, gt=0)
    quantity: int = Field(default=1, ge=1, le=1000)


class CalculationInput(StrictModel):
    itemID: str = Field(min_length=1, max_length=256)
    configurationID: str = Field(min_length=1, max_length=256)
    configurationVersion: int = Field(ge=1)
    itemTypeID: str = Field(min_length=1, max_length=128)
    aluminumSystemID: str | None = Field(default=None, max_length=128)
    aluminumProfileID: str | None = Field(default=None, max_length=128)
    aluminumColorID: str | None = Field(default=None, max_length=128)
    glassTypeID: str | None = Field(default=None, max_length=128)
    glassThicknessID: str | None = Field(default=None, max_length=128)
    glassColorID: str | None = Field(default=None, max_length=128)
    glassTreatmentID: str | None = Field(default=None, max_length=128)
    measurementStage: Literal["reported", "confirmed", "final"] = "reported"
    vanoWidthMM: float | None = Field(default=None, gt=0)
    vanoHeightMM: float | None = Field(default=None, gt=0)
    widthMM: float | None = Field(default=None, gt=0)
    heightMM: float | None = Field(default=None, gt=0)
    additionalVanoSegments: list[VanoSegmentInput] = Field(default_factory=list, max_length=64)
    glassPanels: list[GlassPanelInput] = Field(default_factory=list, max_length=256)
    sections: list[SectionInput] = Field(default_factory=list, max_length=256)
    hardwareIDs: list[str] = Field(default_factory=list, max_length=256)
    accessoryIDs: list[str] = Field(default_factory=list, max_length=256)
    selectedComponentCatalogIDs: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def uniqueness(self):
        if len({s.id for s in self.sections}) != len(self.sections):
            raise ValueError("section IDs must be unique")
        if len(set(self.selectedComponentCatalogIDs)) != len(self.selectedComponentCatalogIDs):
            raise ValueError("component catalog IDs must be unique")
        if len({s.id for s in self.additionalVanoSegments}) != len(self.additionalVanoSegments):
            raise ValueError("additional vano segment identities must be unique")
        if len({p.id for p in self.glassPanels}) != len(self.glassPanels):
            raise ValueError("glass panel identities must be unique")
        section_ids = {s.id for s in self.sections}
        if any(panel.sectionID not in section_ids for panel in self.glassPanels):
            raise ValueError("glass panel references missing structural section")
        for field in (self.hardwareIDs, self.accessoryIDs):
            if len(field) != len(set(field)):
                raise ValueError("selected hardware/accessory identities must be unique")
        return self

class TrustedCatalogEntry(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    unit: Unit
    active: bool = True
    itemTypeIDs: frozenset[str] = frozenset()
    systemIDs: frozenset[str] = frozenset()

class TrustedQuantityRule(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    catalogEntryID: str = Field(min_length=1, max_length=128)
    itemTypeID: str = Field(min_length=1, max_length=128)
    systemID: str | None = Field(default=None, max_length=128)
    mode: QuantityMode
    sectionType: str | None = Field(default=None, max_length=64)
    quantityPerOccurrence: float = Field(gt=0)

    @model_validator(mode="after")
    def section_mode_requires_type(self):
        if self.mode == "per_matching_section" and not self.sectionType:
            raise ValueError("section count rules require sectionType")
        if self.mode == "per_item" and self.sectionType is not None:
            raise ValueError("per-item rules cannot specify sectionType")
        return self

class AuthorizedQuantityOverride(StrictModel):
    catalogEntryID: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    reason: str = Field(min_length=8, max_length=2048)

class CalculatedComponent(StrictModel):
    id: str
    itemID: str
    configurationID: str
    configurationVersion: int
    catalogEntryID: str
    referenceSnapshot: str
    quantity: float
    unit: Unit
    origin: Origin
    sourceRuleID: str
    manualOverrideReason: str | None = None

class CalculationReceipt(StrictModel):
    schemaVersion: Literal["vitrial.item-components.v1"] = "vitrial.item-components.v1"
    inputSHA256: str
    catalogVersionID: str
    rulesVersionID: str
    itemID: str
    configurationID: str
    configurationVersion: int
    components: list[CalculatedComponent]
    issues: list[str]
    @property
    def ready(self) -> bool:
        return not self.issues


def calculate_components(
    configuration: CalculationInput,
    *,
    trusted_catalog: Sequence[TrustedCatalogEntry],
    trusted_rules: Sequence[TrustedQuantityRule],
    catalog_version_id: str,
    rules_version_id: str,
    overrides: Sequence[AuthorizedQuantityOverride] = (),
    override_authorized: bool = False,
) -> CalculationReceipt:
    """Compute only quantities backed by an exact trusted catalog rule.

    A server endpoint should load the rules/catalog from immutable publications
    and check principal+Item ownership BEFORE calling this service. The client
    must never supply trusted_catalog/trusted_rules or override_authorized.
    """
    if not catalog_version_id or not rules_version_id:
        raise ValueError("immutable catalog/rule version pins required")
    basis = configuration.model_dump(mode="json", exclude_none=False)
    digest = hashlib.sha256(json.dumps(
        basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    catalog = {entry.id: entry for entry in trusted_catalog}
    rules: dict[str, list[TrustedQuantityRule]] = {}
    for rule in trusted_rules:
        rules.setdefault(rule.catalogEntryID, []).append(rule)
    issues: list[str] = []
    if len(catalog) != len(trusted_catalog):
        issues.append("duplicate trusted component catalog identity")
    if len({rule.id for rule in trusted_rules}) != len(trusted_rules):
        issues.append("duplicate trusted technical rule identity")
    if not trusted_rules:
        issues.append("missing technical component rule publication")

    override_by_catalog = {entry.catalogEntryID: entry for entry in overrides}
    if len(override_by_catalog) != len(overrides):
        issues.append("duplicate manual override for component")
    if overrides and not override_authorized:
        issues.append("manual override requires independently verified permission")

    calculated: list[CalculatedComponent] = []
    for component_id in configuration.selectedComponentCatalogIDs:
        entry = catalog.get(component_id)
        if entry is None or not entry.active:
            issues.append(f"component reference unavailable: {component_id}")
            continue
        if entry.itemTypeIDs and configuration.itemTypeID not in entry.itemTypeIDs:
            issues.append(f"component incompatible with Item Type: {component_id}")
            continue
        if entry.systemIDs and configuration.aluminumSystemID not in entry.systemIDs:
            issues.append(f"component incompatible with aluminum system: {component_id}")
            continue
        matching = [
            rule for rule in rules.get(component_id, [])
            if rule.itemTypeID == configuration.itemTypeID
            and (rule.systemID is None or rule.systemID == configuration.aluminumSystemID)
        ]
        if len(matching) != 1:
            issues.append(f"missing or ambiguous technical calculation rule: {component_id}")
            continue
        rule = matching[0]
        if rule.mode == "per_item":
            multiplier = 1
        else:
            multiplier = sum(1 for s in configuration.sections if s.type == rule.sectionType)
        if multiplier == 0:
            issues.append(f"selected component has no applicable section: {component_id}")
            continue
        quantity = rule.quantityPerOccurrence * multiplier
        if not 0 < quantity < float("inf"):
            issues.append(f"invalid calculated quantity: {component_id}")
            continue
        override = override_by_catalog.get(component_id)
        origin: Origin = "calculated"
        reason = None
        if override and override_authorized:
            quantity = override.quantity
            origin = "authorized_override"
            reason = override.reason
        instance_id = "|".join([
            configuration.itemID, configuration.configurationID,
            str(configuration.configurationVersion), rule.id, entry.id
        ])
        calculated.append(CalculatedComponent(
            id=instance_id, itemID=configuration.itemID,
            configurationID=configuration.configurationID,
            configurationVersion=configuration.configurationVersion,
            catalogEntryID=entry.id, referenceSnapshot=entry.name,
            quantity=quantity, unit=entry.unit, origin=origin,
            sourceRuleID=rule.id, manualOverrideReason=reason
        ))

    for override in overrides:
        if override.catalogEntryID not in configuration.selectedComponentCatalogIDs:
            issues.append(f"override does not belong to selected component: {override.catalogEntryID}")
        if override.catalogEntryID not in {c.catalogEntryID for c in calculated}:
            issues.append(f"override lacks a verified calculation: {override.catalogEntryID}")

    return CalculationReceipt(
        inputSHA256=digest,
        catalogVersionID=catalog_version_id,
        rulesVersionID=rules_version_id,
        itemID=configuration.itemID,
        configurationID=configuration.configurationID,
        configurationVersion=configuration.configurationVersion,
        components=calculated if not issues else [],
        issues=sorted(set(issues))
    )