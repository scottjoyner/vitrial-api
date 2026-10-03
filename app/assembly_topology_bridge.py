from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.assembly_contract import AssemblySnapshotV1
from app.assembly_topology import AssemblySectionTopologyEvidence, StrictTopologyModel


SuccessorRequirement = Literal[
    "verifiedTopologyDimensions",
    "multiRowPlanarGeometry",
    "cornerGeometry",
    "sectionToCornerLegAssignment",
]


class AssemblyTopologyAssemblyV1Context(StrictTopologyModel):
    itemID: str = Field(min_length=1, max_length=256)
    configurationID: str = Field(min_length=1, max_length=256)
    configurationVersion: int = Field(ge=1)
    sectionIDs: list[str] = Field(min_length=1)


class AssemblyTopologyAssemblyV1Assessment(StrictTopologyModel):
    topologyID: str
    sourceReferenceID: str
    sourceScopeReference: str
    itemID: str
    configurationID: str
    configurationVersion: int
    assemblySchema: str
    compatibleWithAssemblyV1: bool
    successorRequirements: list[SuccessorRequirement]
    corroboratedSectionIDs: list[str]


class AssemblyTopologyAssemblyV1BridgeError(ValueError):
    pass


_FRAME_TOLERANCE = 1e-6


def assess_topology_against_assembly_v1(
    *,
    topology: AssemblySectionTopologyEvidence,
    assembly: AssemblySnapshotV1,
    context: AssemblyTopologyAssemblyV1Context,
) -> AssemblyTopologyAssemblyV1Assessment:
    """Compare source/operator topology with the frozen V1 projection without mutation.

    A compatible result means independent topology evidence corroborates the existing
    horizontal-planar V1 render geometry. Missing topology dimensions are never filled from
    the assembly snapshot merely to make the evidence renderable.
    """

    _validate_identity(topology=topology, assembly=assembly, context=context)

    known_sections = set(context.sectionIDs)

    if topology.kind == "corner":
        requirements: list[SuccessorRequirement] = ["cornerGeometry"]
        if topology.dimensionalReadiness != "dimensionallyRenderable":
            requirements.append("verifiedTopologyDimensions")

        assigned = [
            section_id
            for leg in topology.cornerLegs
            for section_id in leg.sectionIDs
        ]
        if set(assigned) != known_sections or len(assigned) != len(context.sectionIDs):
            requirements.append("sectionToCornerLegAssignment")

        return _assessment(
            topology=topology,
            context=context,
            assembly=assembly,
            compatible=False,
            requirements=requirements,
            corroborated=[],
        )

    topology_section_ids = [cell.sectionID for cell in topology.cells]
    if (
        set(topology_section_ids) != known_sections
        or len(topology_section_ids) != len(context.sectionIDs)
    ):
        raise AssemblyTopologyAssemblyV1BridgeError(
            "planar topology must place every configured section exactly once"
        )

    requirements = []
    if topology.rowCount != 1:
        requirements.append("multiRowPlanarGeometry")
    if (
        topology.dimensionalReadiness != "dimensionallyRenderable"
        or len(topology.rowProportions) != topology.rowCount
        or len(topology.columnProportions) != topology.columnCount
    ):
        requirements.append("verifiedTopologyDimensions")

    if requirements:
        return _assessment(
            topology=topology,
            context=context,
            assembly=assembly,
            compatible=False,
            requirements=requirements,
            corroborated=[],
        )

    column_starts = _starts(topology.columnProportions)
    render_by_id = {
        section.sectionID: section
        for section in assembly.renderDescriptor.sections
    }

    corroborated: list[str] = []
    for cell in topology.cells:
        if (
            cell.row != 0
            or cell.rowSpan != 1
            or cell.column + cell.columnSpan > topology.columnCount
        ):
            raise AssemblyTopologyAssemblyV1BridgeError(
                f"topology cell {cell.id} cannot map to horizontal Assembly V1"
            )

        render = render_by_id.get(cell.sectionID)
        if render is None:
            raise AssemblyTopologyAssemblyV1BridgeError(
                f"render descriptor is missing section {cell.sectionID}"
            )

        expected_x = column_starts[cell.column]
        expected_width = sum(
            topology.columnProportions[
                cell.column : cell.column + cell.columnSpan
            ]
        )
        frame = render.frame

        if not (
            _approximately_equal(frame.x, expected_x)
            and _approximately_equal(frame.y, 0)
            and _approximately_equal(frame.width, expected_width)
            and _approximately_equal(frame.height, 1)
        ):
            raise AssemblyTopologyAssemblyV1BridgeError(
                f"topology geometry disagrees with frozen Assembly V1 section {cell.sectionID}"
            )

        corroborated.append(cell.sectionID)

    return _assessment(
        topology=topology,
        context=context,
        assembly=assembly,
        compatible=True,
        requirements=[],
        corroborated=corroborated,
    )


def _validate_identity(
    *,
    topology: AssemblySectionTopologyEvidence,
    assembly: AssemblySnapshotV1,
    context: AssemblyTopologyAssemblyV1Context,
) -> None:
    if (
        topology.itemID != context.itemID
        or topology.configurationID != context.configurationID
        or topology.configurationVersion != context.configurationVersion
    ):
        raise AssemblyTopologyAssemblyV1BridgeError(
            "topology identity does not match configuration context"
        )

    if (
        assembly.bom.configurationID != context.configurationID
        or assembly.bom.configurationVersion != context.configurationVersion
        or assembly.renderDescriptor.configurationID != context.configurationID
        or assembly.renderDescriptor.configurationVersion != context.configurationVersion
    ):
        raise AssemblyTopologyAssemblyV1BridgeError(
            "frozen Assembly V1 identity does not match configuration context"
        )

    configured = set(context.sectionIDs)
    assembly_ids = [section.id for section in assembly.sections]
    render_ids = [
        section.sectionID
        for section in assembly.renderDescriptor.sections
    ]
    if (
        set(assembly_ids) != configured
        or set(render_ids) != configured
        or len(assembly_ids) != len(context.sectionIDs)
        or len(render_ids) != len(context.sectionIDs)
    ):
        raise AssemblyTopologyAssemblyV1BridgeError(
            "frozen Assembly V1 section identities do not match configuration context"
        )


def _assessment(
    *,
    topology: AssemblySectionTopologyEvidence,
    context: AssemblyTopologyAssemblyV1Context,
    assembly: AssemblySnapshotV1,
    compatible: bool,
    requirements: list[SuccessorRequirement],
    corroborated: list[str],
) -> AssemblyTopologyAssemblyV1Assessment:
    return AssemblyTopologyAssemblyV1Assessment(
        topologyID=topology.id,
        sourceReferenceID=topology.sourceReferenceID,
        sourceScopeReference=topology.sourceScopeReference,
        itemID=context.itemID,
        configurationID=context.configurationID,
        configurationVersion=context.configurationVersion,
        assemblySchema=assembly.schema,
        compatibleWithAssemblyV1=compatible,
        successorRequirements=requirements,
        corroboratedSectionIDs=corroborated,
    )


def _starts(values: list[float]) -> list[float]:
    cursor = 0.0
    starts: list[float] = []
    for value in values:
        starts.append(cursor)
        cursor += value
    return starts


def _approximately_equal(lhs: float, rhs: float) -> bool:
    return abs(lhs - rhs) <= _FRAME_TOLERANCE
