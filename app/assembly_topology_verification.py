from __future__ import annotations

import math
from datetime import datetime

from pydantic import Field

from app.assembly_topology import (
    AssemblySectionTopologyEvidence,
    DimensionPayload,
    StrictTopologyModel,
)


DIMENSION_TOLERANCE_MM = 2.0


class ConfigurationGeometryContext(StrictTopologyModel):
    """Read-only geometry context for the exact configuration revision being verified."""

    itemID: str = Field(min_length=1, max_length=256)
    configurationID: str = Field(min_length=1, max_length=256)
    configurationVersion: int = Field(ge=1)
    openingWidth: DimensionPayload | None = None
    openingHeight: DimensionPayload | None = None
    sectionIDs: list[str] = Field(default_factory=list)


class CornerLegDimensionVerification(StrictTopologyModel):
    legID: str = Field(min_length=1, max_length=256)
    measuredLength: DimensionPayload
    sectionIDs: list[str] = Field(default_factory=list)
    sectionWidths: list[DimensionPayload] = Field(default_factory=list)


class AssemblyTopologyDimensionVerificationEvidence(StrictTopologyModel):
    """Operator evidence; intentionally not a V1 sync entity or mutation model."""

    id: str = Field(min_length=1, max_length=256)
    topologyID: str = Field(min_length=1, max_length=256)
    itemID: str = Field(min_length=1, max_length=256)
    configurationID: str = Field(min_length=1, max_length=256)
    configurationVersion: int = Field(ge=1)
    verifiedBy: str = Field(min_length=1, max_length=256)
    verifiedAt: datetime
    evidenceReferenceIDs: list[str] = Field(default_factory=list)
    rowHeights: list[DimensionPayload] = Field(default_factory=list)
    columnWidths: list[DimensionPayload] = Field(default_factory=list)
    cornerLegs: list[CornerLegDimensionVerification] = Field(default_factory=list)
    cornerJunctionAngleDegrees: float | None = Field(
        default=None,
        allow_inf_nan=False,
    )


class VerifiedCornerLegGeometry(StrictTopologyModel):
    legID: str
    lengthMeters: float
    sectionIDs: list[str]
    sectionWidthsMeters: list[float]


class VerifiedAssemblyTopologyGeometry(StrictTopologyModel):
    verificationID: str
    topologyID: str
    sourceReferenceID: str
    sourceScopeReference: str
    itemID: str
    configurationID: str
    configurationVersion: int
    kind: str
    verifiedBy: str
    verifiedAt: datetime
    evidenceReferenceIDs: list[str]
    rowHeightsMeters: list[float] = Field(default_factory=list)
    columnWidthsMeters: list[float] = Field(default_factory=list)
    rowProportions: list[float] = Field(default_factory=list)
    columnProportions: list[float] = Field(default_factory=list)
    cornerLegs: list[VerifiedCornerLegGeometry] = Field(default_factory=list)
    cornerJunctionAngleDegrees: float | None = None


class TopologyDimensionVerificationError(ValueError):
    pass


def verify_topology_dimensions(
    *,
    topology: AssemblySectionTopologyEvidence,
    context: ConfigurationGeometryContext,
    verification: AssemblyTopologyDimensionVerificationEvidence,
) -> VerifiedAssemblyTopologyGeometry:
    """Pure verifier that returns derived geometry and mutates no canonical state."""

    _validate_identity(topology=topology, context=context, verification=verification)

    if topology.kind == "planarGrid":
        return _verify_planar(
            topology=topology,
            context=context,
            verification=verification,
        )

    return _verify_corner(
        topology=topology,
        context=context,
        verification=verification,
    )


def _validate_identity(
    *,
    topology: AssemblySectionTopologyEvidence,
    context: ConfigurationGeometryContext,
    verification: AssemblyTopologyDimensionVerificationEvidence,
) -> None:
    expected = (
        topology.itemID,
        topology.configurationID,
        topology.configurationVersion,
    )
    if (
        context.itemID,
        context.configurationID,
        context.configurationVersion,
    ) != expected:
        raise TopologyDimensionVerificationError(
            "configuration context does not match topology identity"
        )

    if (
        verification.itemID,
        verification.configurationID,
        verification.configurationVersion,
    ) != expected or verification.topologyID != topology.id:
        raise TopologyDimensionVerificationError(
            "verification does not match topology/configuration identity"
        )

    if not verification.verifiedBy.strip():
        raise TopologyDimensionVerificationError("verifiedBy is required")


def _verify_planar(
    *,
    topology: AssemblySectionTopologyEvidence,
    context: ConfigurationGeometryContext,
    verification: AssemblyTopologyDimensionVerificationEvidence,
) -> VerifiedAssemblyTopologyGeometry:
    if context.openingWidth is None or context.openingHeight is None:
        raise TopologyDimensionVerificationError(
            "opening width and height are required for planar verification"
        )
    if verification.cornerLegs:
        raise TopologyDimensionVerificationError(
            "planar verification cannot include corner legs"
        )
    if verification.cornerJunctionAngleDegrees is not None:
        raise TopologyDimensionVerificationError(
            "planar verification cannot include a corner junction angle"
        )

    rows = [dimension.millimeters for dimension in verification.rowHeights]
    columns = [dimension.millimeters for dimension in verification.columnWidths]

    if len(rows) != topology.rowCount:
        raise TopologyDimensionVerificationError(
            f"row track count {len(rows)} does not match expected {topology.rowCount}"
        )
    if len(columns) != topology.columnCount:
        raise TopologyDimensionVerificationError(
            f"column track count {len(columns)} does not match expected {topology.columnCount}"
        )

    _reconcile_total(
        rows,
        expected_mm=context.openingHeight.millimeters,
        axis="row",
    )
    _reconcile_total(
        columns,
        expected_mm=context.openingWidth.millimeters,
        axis="column",
    )

    return VerifiedAssemblyTopologyGeometry(
        verificationID=verification.id,
        topologyID=topology.id,
        sourceReferenceID=topology.sourceReferenceID,
        sourceScopeReference=topology.sourceScopeReference,
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        kind=topology.kind,
        verifiedBy=verification.verifiedBy,
        verifiedAt=verification.verifiedAt,
        evidenceReferenceIDs=verification.evidenceReferenceIDs,
        rowHeightsMeters=[value / 1000.0 for value in rows],
        columnWidthsMeters=[value / 1000.0 for value in columns],
        rowProportions=_proportions(rows),
        columnProportions=_proportions(columns),
    )


def _verify_corner(
    *,
    topology: AssemblySectionTopologyEvidence,
    context: ConfigurationGeometryContext,
    verification: AssemblyTopologyDimensionVerificationEvidence,
) -> VerifiedAssemblyTopologyGeometry:
    if context.openingHeight is None:
        raise TopologyDimensionVerificationError(
            "opening height is required for corner verification"
        )
    if verification.rowHeights or verification.columnWidths:
        raise TopologyDimensionVerificationError(
            "corner verification cannot include planar row/column tracks"
        )

    angle = verification.cornerJunctionAngleDegrees
    if angle is None:
        raise TopologyDimensionVerificationError(
            "corner junction angle is required"
        )
    if not math.isfinite(angle) or not 0 < angle < 180:
        raise TopologyDimensionVerificationError(
            "corner junction angle must be finite and between 0 and 180 degrees"
        )

    if len(verification.cornerLegs) != len(topology.cornerLegs):
        raise TopologyDimensionVerificationError(
            "corner leg count does not match topology"
        )

    base_by_id = {leg.id: leg for leg in topology.cornerLegs}
    known_sections = set(context.sectionIDs)
    seen_legs: set[str] = set()
    assigned_sections: set[str] = set()
    verified_legs: list[VerifiedCornerLegGeometry] = []

    for leg in verification.cornerLegs:
        if leg.legID in seen_legs:
            raise TopologyDimensionVerificationError(
                f"duplicate corner leg {leg.legID}"
            )
        seen_legs.add(leg.legID)

        base = base_by_id.get(leg.legID)
        if base is None:
            raise TopologyDimensionVerificationError(
                f"unknown corner leg {leg.legID}"
            )

        measured_mm = leg.measuredLength.millimeters
        expected_mm = base.length.millimeters
        if abs(measured_mm - expected_mm) > DIMENSION_TOLERANCE_MM:
            raise TopologyDimensionVerificationError(
                f"corner leg {leg.legID} length {measured_mm} mm "
                f"does not match current topology envelope {expected_mm} mm"
            )

        if len(leg.sectionIDs) != len(leg.sectionWidths):
            raise TopologyDimensionVerificationError(
                f"corner leg {leg.legID} needs one width per assigned section"
            )

        widths = [dimension.millimeters for dimension in leg.sectionWidths]
        _reconcile_total(widths, expected_mm=measured_mm, axis=f"corner leg {leg.legID}")

        for section_id in leg.sectionIDs:
            if section_id not in known_sections:
                raise TopologyDimensionVerificationError(
                    f"unknown section assignment {section_id}"
                )
            if section_id in assigned_sections:
                raise TopologyDimensionVerificationError(
                    f"duplicate section assignment {section_id}"
                )
            assigned_sections.add(section_id)

        verified_legs.append(
            VerifiedCornerLegGeometry(
                legID=leg.legID,
                lengthMeters=measured_mm / 1000.0,
                sectionIDs=leg.sectionIDs,
                sectionWidthsMeters=[value / 1000.0 for value in widths],
            )
        )

    if assigned_sections != known_sections:
        raise TopologyDimensionVerificationError(
            "every configured section must be assigned exactly once"
        )

    return VerifiedAssemblyTopologyGeometry(
        verificationID=verification.id,
        topologyID=topology.id,
        sourceReferenceID=topology.sourceReferenceID,
        sourceScopeReference=topology.sourceScopeReference,
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        kind=topology.kind,
        verifiedBy=verification.verifiedBy,
        verifiedAt=verification.verifiedAt,
        evidenceReferenceIDs=verification.evidenceReferenceIDs,
        cornerLegs=verified_legs,
        cornerJunctionAngleDegrees=angle,
    )


def _reconcile_total(
    values: list[float],
    *,
    expected_mm: float,
    axis: str,
) -> None:
    actual = sum(values)
    if abs(actual - expected_mm) > DIMENSION_TOLERANCE_MM:
        raise TopologyDimensionVerificationError(
            f"{axis} dimensions total {actual} mm; expected {expected_mm} mm"
        )


def _proportions(values: list[float]) -> list[float]:
    total = sum(values)
    if total <= 0:
        return []
    return [value / total for value in values]
