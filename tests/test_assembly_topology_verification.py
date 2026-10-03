from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.assembly_topology import AssemblySectionTopologyEvidence, DimensionPayload
from app.assembly_topology_verification import (
    AssemblyTopologyDimensionVerificationEvidence,
    ConfigurationGeometryContext,
    CornerLegDimensionVerification,
    TopologyDimensionVerificationError,
    verify_topology_dimensions,
)


def v4_topology() -> AssemblySectionTopologyEvidence:
    return AssemblySectionTopologyEvidence.model_validate(
        {
            "schemaVersion": 1,
            "id": "villa-camila-scope-03-v4-topology",
            "sourceReferenceID": "villa-camila-quotation-001-2026-09-16",
            "sourceScopeReference": "scope-3-v4",
            "itemID": "villa-camila-scope-03-v4-item",
            "configurationID": "villa-camila-scope-03-v4-configuration",
            "configurationVersion": 1,
            "kind": "planarGrid",
            "rowCount": 2,
            "columnCount": 2,
            "rowProportions": [],
            "columnProportions": [],
            "cells": [
                {"id": "v4-slide-left", "sectionID": "v4-slide-left", "row": 0, "column": 0},
                {"id": "v4-slide-right", "sectionID": "v4-slide-right", "row": 0, "column": 1},
                {
                    "id": "v4-fixed-lower",
                    "sectionID": "v4-fixed-lower",
                    "row": 1,
                    "column": 0,
                    "columnSpan": 2,
                },
            ],
            "cornerLegs": [],
            "constraints": [],
            "dimensionalReadiness": "semanticOnly",
            "sourceBasis": "Villa Camila scope 3 narrative; internal track dimensions not quoted.",
        }
    )


def v1_topology() -> AssemblySectionTopologyEvidence:
    return AssemblySectionTopologyEvidence.model_validate(
        {
            "schemaVersion": 1,
            "id": "villa-camila-scope-08-v1-topology",
            "sourceReferenceID": "villa-camila-quotation-001-2026-09-16",
            "sourceScopeReference": "scope-8-v1",
            "itemID": "villa-camila-scope-08-v1-item",
            "configurationID": "villa-camila-scope-08-v1-configuration",
            "configurationVersion": 1,
            "kind": "corner",
            "rowCount": 0,
            "columnCount": 0,
            "rowProportions": [],
            "columnProportions": [],
            "cells": [],
            "cornerLegs": [
                {"id": "v1-leg-a", "length": {"value": 156, "unit": "cm"}, "sectionIDs": []},
                {"id": "v1-leg-b", "length": {"value": 126, "unit": "cm"}, "sectionIDs": []},
            ],
            "constraints": [],
            "dimensionalReadiness": "cornerEnvelopeKnown",
            "sourceBasis": "Villa Camila scope 8 corner envelope.",
        }
    )


def test_planar_operator_verification_derives_geometry_without_mutation():
    topology = v4_topology()
    context = ConfigurationGeometryContext(
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        openingWidth=DimensionPayload(value=129, unit="cm"),
        openingHeight=DimensionPayload(value=287, unit="cm"),
        sectionIDs=["v4-slide-left", "v4-slide-right", "v4-fixed-lower"],
    )
    verification = AssemblyTopologyDimensionVerificationEvidence(
        id="v4-operator-verification",
        topologyID=topology.id,
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        verifiedBy="field-operator",
        verifiedAt=datetime(2026, 10, 3, tzinfo=timezone.utc),
        evidenceReferenceIDs=["measurement-photo-v4"],
        # Test-only operator evidence; these values are not quote-derived facts.
        rowHeights=[
            DimensionPayload(value=210, unit="cm"),
            DimensionPayload(value=77, unit="cm"),
        ],
        columnWidths=[
            DimensionPayload(value=64.5, unit="cm"),
            DimensionPayload(value=64.5, unit="cm"),
        ],
    )

    geometry = verify_topology_dimensions(
        topology=topology,
        context=context,
        verification=verification,
    )

    assert geometry.kind == "planarGrid"
    assert geometry.rowHeightsMeters == [2.1, 0.77]
    assert geometry.columnWidthsMeters == [0.645, 0.645]
    assert geometry.columnProportions == [0.5, 0.5]
    assert geometry.sourceReferenceID == topology.sourceReferenceID
    assert geometry.verifiedBy == "field-operator"


def test_planar_verification_rejects_track_total_drift():
    topology = v4_topology()
    context = ConfigurationGeometryContext(
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        openingWidth=DimensionPayload(value=129, unit="cm"),
        openingHeight=DimensionPayload(value=287, unit="cm"),
    )
    verification = AssemblyTopologyDimensionVerificationEvidence(
        id="bad-v4",
        topologyID=topology.id,
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        verifiedBy="field-operator",
        verifiedAt=datetime(2026, 10, 3, tzinfo=timezone.utc),
        rowHeights=[
            DimensionPayload(value=200, unit="cm"),
            DimensionPayload(value=70, unit="cm"),
        ],
        columnWidths=[
            DimensionPayload(value=64.5, unit="cm"),
            DimensionPayload(value=64.5, unit="cm"),
        ],
    )

    with pytest.raises(TopologyDimensionVerificationError, match="row dimensions total"):
        verify_topology_dimensions(
            topology=topology,
            context=context,
            verification=verification,
        )


def test_verification_rejects_stale_configuration_revision():
    topology = v4_topology()
    context = ConfigurationGeometryContext(
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=2,
        openingWidth=DimensionPayload(value=129, unit="cm"),
        openingHeight=DimensionPayload(value=287, unit="cm"),
    )
    verification = AssemblyTopologyDimensionVerificationEvidence(
        id="stale",
        topologyID=topology.id,
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        verifiedBy="field-operator",
        verifiedAt=datetime(2026, 10, 3, tzinfo=timezone.utc),
        rowHeights=[
            DimensionPayload(value=210, unit="cm"),
            DimensionPayload(value=77, unit="cm"),
        ],
        columnWidths=[
            DimensionPayload(value=64.5, unit="cm"),
            DimensionPayload(value=64.5, unit="cm"),
        ],
    )

    with pytest.raises(TopologyDimensionVerificationError, match="context does not match topology"):
        verify_topology_dimensions(
            topology=topology,
            context=context,
            verification=verification,
        )


def test_corner_operator_verification_requires_exact_section_assignment():
    topology = v1_topology()
    context = ConfigurationGeometryContext(
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        openingHeight=DimensionPayload(value=126, unit="cm"),
        sectionIDs=["v1-slide-left", "v1-fixed-a", "v1-fixed-b", "v1-slide-right"],
    )
    verification = AssemblyTopologyDimensionVerificationEvidence(
        id="v1-operator-verification",
        topologyID=topology.id,
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        verifiedBy="field-operator",
        verifiedAt=datetime(2026, 10, 3, tzinfo=timezone.utc),
        evidenceReferenceIDs=["corner-measurement-photo"],
        # Test-only operator assignment; the quotation does not map stored section IDs to legs.
        cornerLegs=[
            CornerLegDimensionVerification(
                legID="v1-leg-a",
                measuredLength=DimensionPayload(value=156, unit="cm"),
                sectionIDs=["v1-slide-left", "v1-fixed-a"],
                sectionWidths=[
                    DimensionPayload(value=78, unit="cm"),
                    DimensionPayload(value=78, unit="cm"),
                ],
            ),
            CornerLegDimensionVerification(
                legID="v1-leg-b",
                measuredLength=DimensionPayload(value=126, unit="cm"),
                sectionIDs=["v1-fixed-b", "v1-slide-right"],
                sectionWidths=[
                    DimensionPayload(value=63, unit="cm"),
                    DimensionPayload(value=63, unit="cm"),
                ],
            ),
        ],
        cornerJunctionAngleDegrees=90,
    )

    geometry = verify_topology_dimensions(
        topology=topology,
        context=context,
        verification=verification,
    )

    assert geometry.kind == "corner"
    assert geometry.cornerJunctionAngleDegrees == 90
    assert geometry.cornerLegs[0].lengthMeters == pytest.approx(1.56)
    assert geometry.cornerLegs[1].lengthMeters == pytest.approx(1.26)
    assert {
        section
        for leg in geometry.cornerLegs
        for section in leg.sectionIDs
    } == set(context.sectionIDs)


def test_corner_verification_rejects_envelope_mismatch_instead_of_rewriting_source():
    topology = v1_topology()
    context = ConfigurationGeometryContext(
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        openingHeight=DimensionPayload(value=126, unit="cm"),
        sectionIDs=["v1-slide-left", "v1-fixed-a", "v1-fixed-b", "v1-slide-right"],
    )
    verification = AssemblyTopologyDimensionVerificationEvidence(
        id="v1-mismatch",
        topologyID=topology.id,
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        verifiedBy="field-operator",
        verifiedAt=datetime(2026, 10, 3, tzinfo=timezone.utc),
        cornerLegs=[
            CornerLegDimensionVerification(
                legID="v1-leg-a",
                measuredLength=DimensionPayload(value=160, unit="cm"),
                sectionIDs=["v1-slide-left", "v1-fixed-a"],
                sectionWidths=[
                    DimensionPayload(value=80, unit="cm"),
                    DimensionPayload(value=80, unit="cm"),
                ],
            ),
            CornerLegDimensionVerification(
                legID="v1-leg-b",
                measuredLength=DimensionPayload(value=126, unit="cm"),
                sectionIDs=["v1-fixed-b", "v1-slide-right"],
                sectionWidths=[
                    DimensionPayload(value=63, unit="cm"),
                    DimensionPayload(value=63, unit="cm"),
                ],
            ),
        ],
        cornerJunctionAngleDegrees=90,
    )

    with pytest.raises(TopologyDimensionVerificationError, match="does not match current topology envelope"):
        verify_topology_dimensions(
            topology=topology,
            context=context,
            verification=verification,
        )


def test_corner_verification_requires_angle():
    topology = v1_topology()
    context = ConfigurationGeometryContext(
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        openingHeight=DimensionPayload(value=126, unit="cm"),
        sectionIDs=["v1-slide-left", "v1-fixed-a", "v1-fixed-b", "v1-slide-right"],
    )
    verification = AssemblyTopologyDimensionVerificationEvidence(
        id="v1-no-angle",
        topologyID=topology.id,
        itemID=topology.itemID,
        configurationID=topology.configurationID,
        configurationVersion=topology.configurationVersion,
        verifiedBy="field-operator",
        verifiedAt=datetime(2026, 10, 3, tzinfo=timezone.utc),
        cornerLegs=[
            CornerLegDimensionVerification(
                legID="v1-leg-a",
                measuredLength=DimensionPayload(value=156, unit="cm"),
                sectionIDs=["v1-slide-left", "v1-fixed-a"],
                sectionWidths=[
                    DimensionPayload(value=78, unit="cm"),
                    DimensionPayload(value=78, unit="cm"),
                ],
            ),
            CornerLegDimensionVerification(
                legID="v1-leg-b",
                measuredLength=DimensionPayload(value=126, unit="cm"),
                sectionIDs=["v1-fixed-b", "v1-slide-right"],
                sectionWidths=[
                    DimensionPayload(value=63, unit="cm"),
                    DimensionPayload(value=63, unit="cm"),
                ],
            ),
        ],
    )

    with pytest.raises(TopologyDimensionVerificationError, match="junction angle is required"):
        verify_topology_dimensions(
            topology=topology,
            context=context,
            verification=verification,
        )
