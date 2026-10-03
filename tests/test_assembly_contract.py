import copy
import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from app.assembly_contract import (
    AssemblySnapshotV1,
    validate_configuration_assembly,
    validate_quotation_line_design_snapshot,
)
from app.schemas import EntityType


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "contracts" / "assembly" / "aluminum-window-3panel-v1.json"


def fixture_document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_three_panel_fixture_validates_without_new_sync_entity():
    document = fixture_document()

    assembly = validate_configuration_assembly(document["configuration"])
    assert isinstance(assembly, AssemblySnapshotV1)
    assert assembly.templateID == "aluminum_window_3_panel"
    assert [section.sectionType for section in assembly.sections] == [
        "sliding",
        "sliding",
        "fixed",
    ]
    assert [section.operation for section in assembly.sections] == [
        "left",
        "right",
        None,
    ]

    # The assembly is nested payload content, not a new authorization/sync surface.
    assert "assembly" not in get_args(EntityType)
    assert "configuration" in get_args(EntityType)
    assert "configuration_version" in get_args(EntityType)


def test_render_descriptor_cannot_drift_from_canonical_sections():
    document = fixture_document()
    document["configuration"]["assembly"]["renderDescriptor"]["sections"][1]["operation"] = "left"

    with pytest.raises(ValidationError, match="render descriptor section diverges"):
        validate_configuration_assembly(document["configuration"])


def test_planar_sections_must_close_without_gap_or_overlap():
    document = fixture_document()
    document["configuration"]["assembly"]["sections"][1]["frame"]["x"] = 0.40

    with pytest.raises(ValidationError, match="contiguous without gaps or overlap"):
        validate_configuration_assembly(document["configuration"])


def test_bom_section_provenance_must_reference_same_assembly():
    document = fixture_document()
    document["configuration"]["assembly"]["bom"]["lines"][0]["sourceSectionID"] = "other-item-panel"

    with pytest.raises(ValidationError, match="sourceSectionID is not part of the assembly"):
        validate_configuration_assembly(document["configuration"])


def test_reference_publication_hashes_are_required_and_sha256_shaped():
    document = fixture_document()
    document["configuration"]["assembly"]["referenceData"]["catalogContentSHA256"] = "not-a-hash"

    with pytest.raises(ValidationError):
        validate_configuration_assembly(document["configuration"])


def test_quotation_design_snapshot_pins_frozen_configuration_and_reference_versions():
    document = fixture_document()
    snapshot = validate_quotation_line_design_snapshot(document["quotationLine"])

    assert snapshot is not None
    assert snapshot.configurationVersionID == "configuration-window-1#v1"
    assert snapshot.referenceData.catalogVersionID == "configurator-catalog-v2"
    assert snapshot.referenceData.compatibilityRulesVersionID == "configurator-rules-v1"
    assert snapshot.referenceData.priceBookVersionID == "price-book-unconfigured-v1"
    assert snapshot.bomSHA256 == "4" * 64


def test_absent_nested_contract_preserves_legacy_payload_shape():
    assert validate_configuration_assembly({"id": "configuration-legacy"}) is None
    assert validate_quotation_line_design_snapshot({"id": "legacy-line"}) is None


def test_duplicate_section_ids_fail_closed():
    document = fixture_document()
    assembly = document["configuration"]["assembly"]
    assembly["sections"][1]["id"] = assembly["sections"][0]["id"]

    with pytest.raises(ValidationError, match="section ids must be unique"):
        validate_configuration_assembly(document["configuration"])


def test_contract_validation_is_deterministic_and_non_mutating():
    document = fixture_document()
    before = copy.deepcopy(document)

    first = validate_configuration_assembly(document["configuration"])
    second = validate_configuration_assembly(document["configuration"])

    assert first == second
    assert document == before
