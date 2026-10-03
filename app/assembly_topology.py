from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TopologyKind = Literal["planarGrid", "corner"]
DimensionalReadiness = Literal[
    "semanticOnly",
    "cornerEnvelopeKnown",
    "dimensionallyRenderable",
]
DimensionUnit = Literal["cm", "mm"]


class StrictTopologyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DimensionPayload(StrictTopologyModel):
    value: float = Field(gt=0)
    unit: DimensionUnit = "cm"

    @property
    def millimeters(self) -> float:
        return self.value * 10.0 if self.unit == "cm" else self.value


class AssemblyTopologyCell(StrictTopologyModel):
    id: str = Field(min_length=1, max_length=256)
    sectionID: str = Field(min_length=1, max_length=256)
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    rowSpan: int = Field(default=1, ge=1)
    columnSpan: int = Field(default=1, ge=1)
    widthFraction: float | None = Field(default=None, gt=0)
    heightFraction: float | None = Field(default=None, gt=0)


class AssemblyCornerLegTopology(StrictTopologyModel):
    id: str = Field(min_length=1, max_length=256)
    length: DimensionPayload
    sectionIDs: list[str] = Field(default_factory=list)


class AssemblyTopologyConstraint(StrictTopologyModel):
    id: str = Field(min_length=1, max_length=256)
    statement: str = Field(min_length=1, max_length=2048)


class AssemblySectionTopologyEvidence(StrictTopologyModel):
    """Read-only topology evidence prototype.

    This is intentionally not a Sync EntityType, database model, or API mutation payload.
    It validates the same evidence shape explored by the iOS reference slice before any
    persistence or authority contract is adopted.
    """

    schemaVersion: Literal[1] = 1
    id: str = Field(min_length=1, max_length=256)
    sourceReferenceID: str = Field(min_length=1, max_length=256)
    sourceScopeReference: str = Field(min_length=1, max_length=256)
    itemID: str = Field(min_length=1, max_length=256)
    configurationID: str = Field(min_length=1, max_length=256)
    configurationVersion: int = Field(ge=1)
    kind: TopologyKind
    rowCount: int = Field(default=0, ge=0)
    columnCount: int = Field(default=0, ge=0)
    cells: list[AssemblyTopologyCell] = Field(default_factory=list)
    cornerLegs: list[AssemblyCornerLegTopology] = Field(default_factory=list)
    constraints: list[AssemblyTopologyConstraint] = Field(default_factory=list)
    dimensionalReadiness: DimensionalReadiness
    sourceBasis: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def topology_shape_must_be_self_consistent(self) -> "AssemblySectionTopologyEvidence":
        if self.kind == "planarGrid":
            if self.rowCount <= 0 or self.columnCount <= 0:
                raise ValueError("planarGrid topology requires positive rowCount and columnCount")
            if self.cornerLegs:
                raise ValueError("planarGrid topology cannot include cornerLegs")

            placed: set[str] = set()
            for cell in self.cells:
                if cell.sectionID in placed:
                    raise ValueError(f"duplicate section placement: {cell.sectionID}")
                placed.add(cell.sectionID)
                if cell.row + cell.rowSpan > self.rowCount:
                    raise ValueError(f"cell exceeds declared rows: {cell.id}")
                if cell.column + cell.columnSpan > self.columnCount:
                    raise ValueError(f"cell exceeds declared columns: {cell.id}")

        if self.kind == "corner":
            if self.cells:
                raise ValueError("corner topology cannot include planar cells")
            if self.rowCount != 0 or self.columnCount != 0:
                raise ValueError("corner topology does not use rowCount/columnCount")
            if len(self.cornerLegs) < 2:
                raise ValueError("corner topology requires at least two legs")

            seen_leg_ids: set[str] = set()
            for leg in self.cornerLegs:
                if leg.id in seen_leg_ids:
                    raise ValueError(f"duplicate corner leg: {leg.id}")
                seen_leg_ids.add(leg.id)

        return self


class AssemblyTopologyRenderability(StrictTopologyModel):
    topologyID: str
    dimensionallyRenderable: bool
    missingEvidence: list[str] = Field(default_factory=list)


def assess_topology_renderability(
    topology: AssemblySectionTopologyEvidence,
) -> AssemblyTopologyRenderability:
    """Pure evaluator; produces evidence only and mutates no canonical state."""

    if topology.dimensionalReadiness == "dimensionallyRenderable":
        return AssemblyTopologyRenderability(
            topologyID=topology.id,
            dimensionallyRenderable=True,
            missingEvidence=[],
        )

    if topology.dimensionalReadiness == "cornerEnvelopeKnown":
        return AssemblyTopologyRenderability(
            topologyID=topology.id,
            dimensionallyRenderable=False,
            missingEvidence=[
                "Per-leg section allocation",
                "Per-section widths on each corner leg",
                "Verified corner/junction geometry",
            ],
        )

    missing: list[str] = []
    if topology.kind == "planarGrid":
        has_width_evidence = all(
            cell.widthFraction is not None or cell.columnSpan == topology.columnCount
            for cell in topology.cells
        )
        has_height_evidence = all(
            cell.heightFraction is not None or cell.rowSpan == topology.rowCount
            for cell in topology.cells
        )
        if not has_width_evidence:
            missing.append("Internal column widths/proportions")
        if not has_height_evidence:
            missing.append("Internal row heights/proportions")

    if not missing:
        missing.append(
            "Topology is semantic-only and needs operator-verified dimensional geometry."
        )

    return AssemblyTopologyRenderability(
        topologyID=topology.id,
        dimensionallyRenderable=False,
        missingEvidence=missing,
    )
