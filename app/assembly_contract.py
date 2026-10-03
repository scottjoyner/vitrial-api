from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from app.schemas import StrictModel


AssemblySectionType = Literal["fixed", "sliding", "projected", "swing", "other"]
AssemblyOperation = Literal["left", "right", "inward", "outward"] | None
AssemblyUnit = Literal["mm"]
BOMUnit = Literal["m2", "m", "each"]

_FRAME_TOLERANCE = 1e-6
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class AssemblyOverall(StrictModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    unit: AssemblyUnit = "mm"


class AssemblyNormalizedFrame(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def frame_must_fit_opening(self) -> "AssemblyNormalizedFrame":
        if self.x + self.width > 1 + _FRAME_TOLERANCE:
            raise ValueError("normalized frame exceeds opening width")
        if self.y + self.height > 1 + _FRAME_TOLERANCE:
            raise ValueError("normalized frame exceeds opening height")
        return self


class AssemblySectionSnapshot(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    sectionType: AssemblySectionType
    operation: AssemblyOperation = None
    frame: AssemblyNormalizedFrame
    widthMM: float = Field(gt=0)
    heightMM: float = Field(gt=0)


class AssemblyReferenceDataPins(StrictModel):
    catalogVersionID: str = Field(min_length=1, max_length=256)
    catalogContentSHA256: str = Field(pattern=_SHA256_PATTERN)
    compatibilityRulesVersionID: str = Field(min_length=1, max_length=256)
    compatibilityRulesContentSHA256: str = Field(pattern=_SHA256_PATTERN)
    priceBookVersionID: str = Field(min_length=1, max_length=256)
    priceBookContentSHA256: str = Field(pattern=_SHA256_PATTERN)


class AssemblyBOMLine(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    role: str = Field(min_length=1, max_length=128)
    referenceEntryIDs: list[str] = Field(default_factory=list)
    sourceRuleID: str = Field(min_length=1, max_length=256)
    sourceSectionID: str | None = Field(default=None, max_length=256)
    quantity: float = Field(ge=0)
    unit: BOMUnit
    wasteQuantity: float = Field(default=0, ge=0)


class AssemblyBOMSnapshot(StrictModel):
    schema: Literal["vitrial.bom.v1"] = "vitrial.bom.v1"
    engineVersion: str = Field(min_length=1, max_length=256)
    configurationID: str = Field(min_length=1, max_length=256)
    configurationVersion: int = Field(ge=1)
    calculationBasis: str = Field(min_length=1, max_length=1024)
    lines: list[AssemblyBOMLine] = Field(default_factory=list)


class AssemblyRenderOverall(StrictModel):
    widthMM: float = Field(gt=0)
    heightMM: float = Field(gt=0)


class AssemblyRenderSection(StrictModel):
    sectionID: str = Field(min_length=1, max_length=256)
    sectionType: AssemblySectionType
    operation: AssemblyOperation = None
    frame: AssemblyNormalizedFrame


class AssemblyRenderDescriptor(StrictModel):
    schema: Literal["vitrial.render.v1"] = "vitrial.render.v1"
    coordinateSystem: Literal["normalized_opening"] = "normalized_opening"
    configurationID: str = Field(min_length=1, max_length=256)
    configurationVersion: int = Field(ge=1)
    overall: AssemblyRenderOverall
    sections: list[AssemblyRenderSection]


class AssemblySnapshotV1(StrictModel):
    """Read-only contract for the first deterministic assembly slice.

    This model intentionally does not add a sync entity or a new authority path.
    It validates the nested payload that can live inside the existing Configuration /
    ConfigurationVersion records.
    """

    schema: Literal["vitrial.assembly.v1"] = "vitrial.assembly.v1"
    templateID: str = Field(min_length=1, max_length=256)
    templateVersion: int = Field(ge=1)
    measurementID: str = Field(min_length=1, max_length=256)
    overall: AssemblyOverall
    sections: list[AssemblySectionSnapshot] = Field(min_length=1)
    selections: dict[str, JsonValue] = Field(default_factory=dict)
    referenceData: AssemblyReferenceDataPins
    bom: AssemblyBOMSnapshot
    renderDescriptor: AssemblyRenderDescriptor

    @model_validator(mode="after")
    def validate_consistent_projection(self) -> "AssemblySnapshotV1":
        section_ids = [section.id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("assembly section ids must be unique")

        # V1 is deliberately one horizontal planar band. More expressive vertical/corner
        # geometry should be a new reviewed contract, not silently invented here.
        cursor = 0.0
        for section in self.sections:
            if abs(section.frame.y) > _FRAME_TOLERANCE:
                raise ValueError("v1 planar section y must be zero")
            if abs(section.frame.height - 1.0) > _FRAME_TOLERANCE:
                raise ValueError("v1 planar sections must span the opening height")
            if abs(section.frame.x - cursor) > _FRAME_TOLERANCE:
                raise ValueError("v1 planar sections must be contiguous without gaps or overlap")
            cursor = section.frame.x + section.frame.width
        if abs(cursor - 1.0) > _FRAME_TOLERANCE:
            raise ValueError("v1 planar sections must fill the opening width")

        if self.renderDescriptor.configurationID != self.bom.configurationID:
            raise ValueError("render descriptor and BOM configuration identity disagree")
        if self.renderDescriptor.configurationVersion != self.bom.configurationVersion:
            raise ValueError("render descriptor and BOM configuration version disagree")
        if abs(self.renderDescriptor.overall.widthMM - self.overall.width) > _FRAME_TOLERANCE:
            raise ValueError("render descriptor width does not match assembly")
        if abs(self.renderDescriptor.overall.heightMM - self.overall.height) > _FRAME_TOLERANCE:
            raise ValueError("render descriptor height does not match assembly")

        render_by_id = {section.sectionID: section for section in self.renderDescriptor.sections}
        if set(render_by_id) != set(section_ids):
            raise ValueError("render descriptor section identity does not match assembly")

        for section in self.sections:
            rendered = render_by_id[section.id]
            if (
                rendered.sectionType != section.sectionType
                or rendered.operation != section.operation
                or rendered.frame != section.frame
            ):
                raise ValueError("render descriptor section diverges from canonical assembly")

        known_section_ids = set(section_ids)
        for line in self.bom.lines:
            if line.sourceSectionID is not None and line.sourceSectionID not in known_section_ids:
                raise ValueError("BOM line sourceSectionID is not part of the assembly")

        return self


class QuotationAssemblyDesignSnapshotV1(StrictModel):
    """Customer-facing design provenance embedded in an existing quotation line."""

    schema: Literal["vitrial.quote-line-design.v1"] = "vitrial.quote-line-design.v1"
    templateID: str = Field(min_length=1, max_length=256)
    templateVersion: int = Field(ge=1)
    configurationVersionID: str = Field(min_length=1, max_length=256)
    overall: AssemblyOverall
    sectionSummary: list[dict[str, JsonValue]] = Field(min_length=1)
    bomSHA256: str = Field(pattern=_SHA256_PATTERN)
    referenceData: AssemblyReferenceDataPins


def validate_configuration_assembly(payload: dict) -> AssemblySnapshotV1 | None:
    """Validate an optional nested assembly payload without changing sync authority.

    Callers can use this before wiring the contract into a mutation path. Absence preserves
    the existing Configuration shape.
    """

    raw = payload.get("assembly")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("configuration assembly payload must be an object")
    return AssemblySnapshotV1.model_validate(raw)


def validate_quotation_line_design_snapshot(
    line: dict,
) -> QuotationAssemblyDesignSnapshotV1 | None:
    raw = line.get("designSnapshot")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("quotation line designSnapshot must be an object")
    return QuotationAssemblyDesignSnapshotV1.model_validate(raw)
