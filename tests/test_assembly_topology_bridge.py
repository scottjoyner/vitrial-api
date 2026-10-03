from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.assembly_contract import AssemblySnapshotV1
from app.assembly_topology import AssemblySectionTopologyEvidence
from app.assembly_topology_bridge import (
    AssemblyTopologyAssemblyV1BridgeError,
    AssemblyTopologyAssemblyV1Context,
    assess_topology_against_assembly_v1,
)


FIXTURE = Path("contracts/assembly/aluminum-window-3panel-v1.json")


def frozen_assembly() -> AssemblySnapshotV1:
    payload = json.loads(FIXTURE.read_text())
    return AssemblySnapshotV1.model_validate(payload["configuration"]["assembly"])


def context() -> AssemblyTopologyAssemblyV1Context:
    return AssemblyTopologyAssemblyV1Context(
        itemID="item-window-1",
        configurationID="configuration-window-1",
        configurationVersion=1,
        sectionIDs=["panel-1", "panel-2", "panel-3"],
    )


def planar_topology(
    *,
    readiness: str = "dimensionallyRenderable",
    columns: list[float] | None = None,
) -> AssemblySectionTopologyEvidence:
    third = 1.0 / 3.0
    return AssemblySectionTopologyEvidence.model_validate(
        {
            "schemaVersion": 1,
            "id": "three-panel-topology",
            "sourceReferenceID": "operator-design-record-001",
            "sourceScopeReference": "opening-a",
            "itemID": "item-window-1",
            "configurationID": "configuration-window-1",
            "configurationVersion": 1,
            "kind": "planarGrid",
            "rowCount": 1,
            "columnCount": 3,
            "rowProportions": [1.0] if readiness == "dimensionallyRenderable" else [],
            "columnProportions": (
                columns
                if columns is not None
                else ([third, third, third] if readiness == "dimensionallyRenderable" else [])
            ),
            "cells": [
                {"id": "cell-1", "sectionID": "panel-1", "row": 0, "column": 0},
                {"id": "cell-2", "sectionID": "panel-2", "row": 0, "column": 1},
                {"id": "cell-3", "sectionID": "panel-3", "row": 0, "column": 2},
            ],
            "cornerLegs": [],
            "constraints": [],
            "dimensionalReadiness": readiness,
            "sourceBasis": "Operator/source topology evidence.",
        }
    )


def test_verified_single_row_topology_corroborates_frozen_assembly_v1():
    result = assess_topology_against_assembly_v1(
        topology=planar_topology(),
        assembly=frozen_assembly(),
        context=context(),
    )

    assert result.compatibleWithAssemblyV1 is True
    assert result.successorRequirements == []
    assert result.corroboratedSectionIDs == ["panel-1", "panel-2", "panel-3"]
    assert result.assemblySchema == "vitrial.assembly.v1"


def test_semantic_only_topology_cannot_borrow_frozen_render_geometry():
    result = assess_topology_against_assembly_v1(
        topology=planar_topology(readiness="semanticOnly"),
        assembly=frozen_assembly(),
        context=context(),
    )

    assert result.compatibleWithAssemblyV1 is False
    assert result.successorRequirements == ["verifiedTopologyDimensions"]
    assert result.corroboratedSectionIDs == []


def test_multi_row_topology_requires_successor_geometry_contract():
    topology = AssemblySectionTopologyEvidence.model_validate(
        {
            "schemaVersion": 1,
            "id": "stacked-topology",
            "sourceReferenceID": "villa-camila-quotation-001-2026-09-16",
            "sourceScopeReference": "scope-test",
            "itemID": "item-window-1",
            "configurationID": "configuration-window-1",
            "configurationVersion": 1,
            "kind": "planarGrid",
            "rowCount": 2,
            "columnCount": 2,
            "rowProportions": [],
            "columnProportions": [],
            "cells": [
                {"id": "a", "sectionID": "panel-1", "row": 0, "column": 0},
                {"id": "b", "sectionID": "panel-2", "row": 0, "column": 1},
                {
                    "id": "c",
                    "sectionID": "panel-3",
                    "row": 1,
                    "column": 0,
                    "columnSpan": 2,
                },
            ],
            "cornerLegs": [],
            "constraints": [],
            "dimensionalReadiness": "semanticOnly",
            "sourceBasis": "Source establishes stacked topology without internal dimensions.",
        }
    )

    result = assess_topology_against_assembly_v1(
        topology=topology,
        assembly=frozen_assembly(),
        context=context(),
    )

    assert result.compatibleWithAssemblyV1 is False
    assert result.successorRequirements == [
        "multiRowPlanarGeometry",
        "verifiedTopologyDimensions",
    ]


def test_corner_topology_requires_successor_and_explicit_section_assignment():
    topology = AssemblySectionTopologyEvidence.model_validate(
        {
            "schemaVersion": 1,
            "id": "corner-topology",
            "sourceReferenceID": "villa-camila-quotation-001-2026-09-16",
            "sourceScopeReference": "scope-test-corner",
            "itemID": "item-window-1",
            "configurationID": "configuration-window-1",
            "configurationVersion": 1,
            "kind": "corner",
            "rowCount": 0,
            "columnCount": 0,
            "rowProportions": [],
            "columnProportions": [],
            "cells": [],
            "cornerLegs": [
                {"id": "leg-a", "length": {"value": 156, "unit": "cm"}, "sectionIDs": []},
                {"id": "leg-b", "length": {"value": 126, "unit": "cm"}, "sectionIDs": []},
            ],
            "constraints": [],
            "dimensionalReadiness": "cornerEnvelopeKnown",
            "sourceBasis": "Source-backed corner envelope; section-to-leg mapping unresolved.",
        }
    )

    result = assess_topology_against_assembly_v1(
        topology=topology,
        assembly=frozen_assembly(),
        context=context(),
    )

    assert result.compatibleWithAssemblyV1 is False
    assert result.successorRequirements == [
        "cornerGeometry",
        "verifiedTopologyDimensions",
        "sectionToCornerLegAssignment",
    ]


def test_verified_topology_disagreement_with_frozen_render_fails_closed():
    topology = planar_topology(columns=[0.25, 0.25, 0.5])

    with pytest.raises(
        AssemblyTopologyAssemblyV1BridgeError,
        match="disagrees with frozen Assembly V1 section panel-1",
    ):
        assess_topology_against_assembly_v1(
            topology=topology,
            assembly=frozen_assembly(),
            context=context(),
        )


def test_identity_drift_fails_closed():
    topology = planar_topology()
    wrong_context = context().model_copy(update={"configurationVersion": 2})

    with pytest.raises(
        AssemblyTopologyAssemblyV1BridgeError,
        match="topology identity does not match configuration context",
    ):
        assess_topology_against_assembly_v1(
            topology=topology,
            assembly=frozen_assembly(),
            context=wrong_context,
        )
