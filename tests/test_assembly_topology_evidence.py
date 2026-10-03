from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from app.assembly_topology import (
    AssemblySectionTopologyEvidence,
    assess_topology_renderability,
)
from app.schemas import EntityType


def v3_payload() -> dict:
    return {
        "schemaVersion": 1,
        "id": "villa-camila-scope-02-v3-topology",
        "sourceReferenceID": "villa-camila-quotation-001-2026-09-16",
        "sourceScopeReference": "scope-2-v3",
        "itemID": "villa-camila-scope-02-v3-item",
        "configurationID": "villa-camila-scope-02-v3-configuration",
        "configurationVersion": 1,
        "kind": "planarGrid",
        "rowCount": 3,
        "columnCount": 1,
        "rowProportions": [],
        "columnProportions": [1.0],
        "cells": [
            {
                "id": "v3-top",
                "sectionID": "v3-fixed-top",
                "row": 0,
                "column": 0,
            },
            {
                "id": "v3-center",
                "sectionID": "v3-projected-center",
                "row": 1,
                "column": 0,
            },
            {
                "id": "v3-bottom",
                "sectionID": "v3-fixed-bottom",
                "row": 2,
                "column": 0,
            },
        ],
        "cornerLegs": [],
        "constraints": [
            {
                "id": "v3-source-order",
                "statement": "Source identifies fixed upper, projected center, and fixed lower panels.",
            }
        ],
        "dimensionalReadiness": "semanticOnly",
        "sourceBasis": (
            "Villa Camila scope 2 narrative: fixed upper + fixed lower + "
            "projected center. Internal panel heights are not specified."
        ),
    }


def v4_payload() -> dict:
    return {
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
            {
                "id": "v4-slide-left",
                "sectionID": "v4-slide-left",
                "row": 0,
                "column": 0,
            },
            {
                "id": "v4-slide-right",
                "sectionID": "v4-slide-right",
                "row": 0,
                "column": 1,
            },
            {
                "id": "v4-fixed-lower",
                "sectionID": "v4-fixed-lower",
                "row": 1,
                "column": 0,
                "columnSpan": 2,
            },
        ],
        "cornerLegs": [],
        "constraints": [
            {
                "id": "v4-source-layout",
                "statement": "Source identifies a lower fixed panel and an upper two-leaf sliding window.",
            },
            {
                "id": "v4-sliding-composition",
                "statement": "Both upper leaves are sliding.",
            },
        ],
        "dimensionalReadiness": "semanticOnly",
        "sourceBasis": (
            "Villa Camila scope 3 narrative: lower fixed panel + two-leaf "
            "sliding Sistema 7-44. Internal row heights and upper leaf widths "
            "are not specified."
        ),
    }


def v1_payload() -> dict:
    return {
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
        "constraints": [
            {
                "id": "v1-composition",
                "statement": (
                    "Four-leaf corner assembly: two fixed leaves joined "
                    "glass-to-glass and two lateral sliding leaves."
                ),
            },
            {
                "id": "v1-height",
                "statement": "Overall quoted height is 126 cm.",
            },
            {
                "id": "v1-section-mapping-unresolved",
                "statement": (
                    "The source does not explicitly map each stored section ID "
                    "to a specific corner leg."
                ),
            },
        ],
        "dimensionalReadiness": "cornerEnvelopeKnown",
        "sourceBasis": (
            "Villa Camila scope 8 narrative and 156 x 126 cm corner widths "
            "with 126 cm height. Per-leg leaf identity is intentionally left unresolved."
        ),
    }


@pytest.mark.parametrize("payload", [v3_payload(), v4_payload(), v1_payload()])
def test_reference_evidence_shapes_validate_without_persistence(payload: dict):
    evidence = AssemblySectionTopologyEvidence.model_validate(payload)
    assert evidence.schemaVersion == 1
    assert evidence.configurationVersion == 1
    assert evidence.sourceReferenceID == "villa-camila-quotation-001-2026-09-16"
    assert evidence.itemID.startswith("villa-camila-")


def test_v3_is_semantically_complete_but_missing_internal_row_heights():
    evidence = AssemblySectionTopologyEvidence.model_validate(v3_payload())
    result = assess_topology_renderability(evidence)

    assert result.dimensionallyRenderable is False
    assert result.missingEvidence == ["Internal row heights/proportions"]


def test_v4_retains_spanning_lower_fixed_and_reports_both_missing_axes():
    evidence = AssemblySectionTopologyEvidence.model_validate(v4_payload())
    lower = next(cell for cell in evidence.cells if cell.sectionID == "v4-fixed-lower")

    assert lower.columnSpan == 2
    result = assess_topology_renderability(evidence)
    assert result.dimensionallyRenderable is False
    assert result.missingEvidence == [
        "Internal column widths/proportions",
        "Internal row heights/proportions",
    ]


def test_v1_corner_envelope_preserves_both_source_lengths_without_leaf_mapping_guess():
    evidence = AssemblySectionTopologyEvidence.model_validate(v1_payload())

    assert evidence.cornerLegs[0].length.millimeters == 1560
    assert evidence.cornerLegs[1].length.millimeters == 1260
    assert all(not leg.sectionIDs for leg in evidence.cornerLegs)

    result = assess_topology_renderability(evidence)
    assert result.dimensionallyRenderable is False
    assert result.missingEvidence == [
        "Per-leg section allocation",
        "Per-section widths on each corner leg",
        "Verified corner/junction geometry",
    ]


def test_topology_is_not_a_v1_sync_entity_type():
    assert "assembly_topology" not in set(get_args(EntityType))
    assert "opening" not in set(get_args(EntityType))





def test_track_proportions_must_match_grid_and_normalize():
    payload = v4_payload()
    payload["rowProportions"] = [0.5, 0.5]
    payload["columnProportions"] = [0.7]

    with pytest.raises(ValidationError, match="invalid column proportions"):
        AssemblySectionTopologyEvidence.model_validate(payload)

    payload["columnProportions"] = [0.7, 0.4]
    with pytest.raises(ValidationError, match="must sum to 1"):
        AssemblySectionTopologyEvidence.model_validate(payload)


def test_planar_cells_cannot_escape_declared_grid():
    payload = v4_payload()
    payload["cells"][0]["column"] = 2

    with pytest.raises(ValidationError, match="cell exceeds declared columns"):
        AssemblySectionTopologyEvidence.model_validate(payload)


def test_duplicate_planar_section_placement_is_rejected():
    payload = v4_payload()
    payload["cells"][1]["sectionID"] = payload["cells"][0]["sectionID"]

    with pytest.raises(ValidationError, match="duplicate section placement"):
        AssemblySectionTopologyEvidence.model_validate(payload)


def test_corner_evidence_requires_at_least_two_legs():
    payload = v1_payload()
    payload["cornerLegs"] = payload["cornerLegs"][:1]

    with pytest.raises(ValidationError, match="requires at least two legs"):
        AssemblySectionTopologyEvidence.model_validate(payload)


def test_unknown_fields_fail_closed():
    payload = v3_payload()
    payload["syncEntityType"] = "assembly_topology"

    with pytest.raises(ValidationError):
        AssemblySectionTopologyEvidence.model_validate(payload)



def test_unknown_schema_version_fails_closed():
    payload = v3_payload()
    payload["schemaVersion"] = 2

    with pytest.raises(ValidationError):
        AssemblySectionTopologyEvidence.model_validate(payload)
