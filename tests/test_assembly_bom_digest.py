import hashlib
import json
import math

import pytest

from app.assembly_contract import (
    AssemblyBOMSnapshot,
    assembly_bom_sha256,
    canonical_bom_digest_material,
)


def fixture_bom() -> AssemblyBOMSnapshot:
    return AssemblyBOMSnapshot.model_validate({
        "schema": "vitrial.bom.v1",
        "engineVersion": "deterministic-planar-rules-v1",
        "configurationID": "configuration-window-1",
        "configurationVersion": 1,
        "calculationBasis": (
            "Deterministic planar reference geometry; "
            "not a fabrication cut list or procurement authority."
        ),
        "lines": [
            {
                "id": "bom-glass-panel-1",
                "role": "glass_infill",
                "referenceEntryIDs": [
                    "glass-tempered",
                    "glass-6mm",
                    "glass-treatment-none",
                ],
                "sourceRuleID": "planar.glass.area.v1",
                "sourceSectionID": "panel-1",
                "quantity": 1.32,
                "unit": "m2",
                "wasteQuantity": 0.066,
            },
            {
                "id": "bom-profile-reference",
                "role": "profile_reference_geometry",
                "referenceEntryIDs": ["al-profile-frame"],
                "sourceRuleID": "planar.profile.perimeter-dividers.reference.v1",
                "sourceSectionID": None,
                "quantity": 12.3,
                "unit": "m",
                "wasteQuantity": 0,
            },
        ],
    })


def test_canonical_material_and_digest_match_swift_fixture():
    material = canonical_bom_digest_material(fixture_bom()).decode("utf-8")
    expected = (
        '{"bomSchema":"vitrial.bom.v1",'
        '"calculationBasis":"Deterministic planar reference geometry; '
        'not a fabrication cut list or procurement authority.",'
        '"configurationID":"configuration-window-1",'
        '"configurationVersion":1,'
        '"engineVersion":"deterministic-planar-rules-v1",'
        '"lines":['
        '{"id":"bom-glass-panel-1",'
        '"quantity":"1.320000",'
        '"referenceEntryIDs":["glass-tempered","glass-6mm","glass-treatment-none"],'
        '"role":"glass_infill",'
        '"sourceRuleID":"planar.glass.area.v1",'
        '"sourceSectionID":"panel-1",'
        '"unit":"m2",'
        '"wasteQuantity":"0.066000"},'
        '{"id":"bom-profile-reference",'
        '"quantity":"12.300000",'
        '"referenceEntryIDs":["al-profile-frame"],'
        '"role":"profile_reference_geometry",'
        '"sourceRuleID":"planar.profile.perimeter-dividers.reference.v1",'
        '"sourceSectionID":"",'
        '"unit":"m",'
        '"wasteQuantity":"0.000000"}],'
        '"schema":"vitrial.bom-digest.v1"}'
    )

    assert material == expected
    assert hashlib.sha256(material.encode("utf-8")).hexdigest() == (
        "c647f652caa08b1ffdb3a6f1106d05ff82e29bfb5b46a096f47efc650e0df2be"
    )
    assert assembly_bom_sha256(fixture_bom()) == (
        "c647f652caa08b1ffdb3a6f1106d05ff82e29bfb5b46a096f47efc650e0df2be"
    )


def test_digest_normalizes_negative_zero_and_fixed_precision():
    bom = AssemblyBOMSnapshot.model_validate({
        "schema": "vitrial.bom.v1",
        "engineVersion": "test",
        "configurationID": "configuration-1",
        "configurationVersion": 1,
        "calculationBasis": "test",
        "lines": [{
            "id": "line-1",
            "role": "test",
            "referenceEntryIDs": [],
            "sourceRuleID": "rule-1",
            "sourceSectionID": None,
            "quantity": 1.2,
            "unit": "each",
            "wasteQuantity": -0.0,
        }],
    })

    material = canonical_bom_digest_material(bom).decode("utf-8")
    assert '"quantity":"1.200000"' in material
    assert '"wasteQuantity":"0.000000"' in material
    assert "-0.000000" not in material


def test_digest_rejects_non_finite_quantity():
    bom = AssemblyBOMSnapshot.model_construct(
        schema="vitrial.bom.v1",
        engineVersion="test",
        configurationID="configuration-1",
        configurationVersion=1,
        calculationBasis="test",
        lines=[
            # model_construct is intentional here: the digest guard must remain fail-closed
            # even if a future caller constructs an already-validated model internally.
            type("Line", (), {
                "id": "line-1",
                "role": "test",
                "referenceEntryIDs": [],
                "sourceRuleID": "rule-1",
                "sourceSectionID": None,
                "quantity": math.inf,
                "unit": "each",
                "wasteQuantity": 0.0,
            })()
        ],
    )

    with pytest.raises(ValueError, match="non-finite"):
        assembly_bom_sha256(bom)


def test_canonical_material_is_valid_compact_sorted_json():
    material = canonical_bom_digest_material(fixture_bom())
    decoded = json.loads(material)
    assert decoded["schema"] == "vitrial.bom-digest.v1"
    assert decoded["lines"][0]["quantity"] == "1.320000"
    assert b" " not in material.split(b'"calculationBasis":', 1)[0]
