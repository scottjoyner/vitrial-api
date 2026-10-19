"""Opt-in text-first SCRUM-51 item drafts never publish prices or manufacturing BOMs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.job_item_draft import validate_job_item_draft


def draft():
    return {
        "jobItemDraft": {
            "schemaVersion": "vitrial.job-item-draft.v1",
            "itemID": "synthetic-item",
            "projectID": "synthetic-job",
            "projectSectorID": "synthetic-sector",
            "nameEsCO": "Ventana mixta",
            "location": "Synthetic sala",
            "quantity": 3,
            "templateID": "synthetic-proposed-template-1",
            "templateVersion": 1,
            "templateReviewStatus": "pending_customer_review",
            "sourceGroup": 2,
            "opening": {
                "kind": "rectangular",
                "heightCm": 150,
                "widthCm": 124.5,
            },
            "components": [
                {"id": "g1", "kind": "glass", "name": "Vidrio", "quantity": "2.250000", "unit": "m2"},
                {"id": "a1", "kind": "aluminum", "name": "Perfil", "quantity": "4.2", "unit": "m"},
                {"id": "h1", "kind": "hardware", "name": "Manija"},
            ],
        }
    }


def validate(payload):
    return validate_job_item_draft(
        payload, item_id="synthetic-item",
        project_id="synthetic-job", project_sector_id="synthetic-sector",
    )


def test_manual_draft_with_multiple_independent_material_types():
    value = validate(draft())
    assert value.quantity == 3
    assert value.opening.widthCm == 124.5
    assert [c.kind for c in value.components] == ["glass", "aluminum", "hardware"]
    assert value.templateReviewStatus == "pending_customer_review"


def test_legacy_items_and_custom_items_stay_compatible():
    assert validate({"id": "synthetic-item", "projectID": "synthetic-job"}) is None
    doc = draft()
    doc["jobItemDraft"]["templateID"] = None
    doc["jobItemDraft"]["templateVersion"] = None
    doc["jobItemDraft"]["components"] = []
    doc["jobItemDraft"]["opening"] = None
    assert validate(doc).templateID is None


@pytest.mark.parametrize("change", [
    lambda d: d["jobItemDraft"].update(itemID="other-item"),
    lambda d: d["jobItemDraft"].update(projectID="other-project"),
    lambda d: d["jobItemDraft"].update(projectSectorID="other-sector"),
    lambda d: d["jobItemDraft"].update(templateReviewStatus="approved"),
    lambda d: d["jobItemDraft"].update(templateVersion=0),
    lambda d: d["jobItemDraft"].update(templateVersion=None),
    lambda d: d["jobItemDraft"].update(quantity=0),
    lambda d: d["jobItemDraft"].update(quantity=True),
    lambda d: d["jobItemDraft"].update(unitPriceCOP=120000),
    lambda d: d["jobItemDraft"].update(warehouseStock=12),
    lambda d: d["jobItemDraft"].update(generatedFabricationBOM=[]),
    lambda d: d["jobItemDraft"]["components"][0].update(unitCostCOP=2),
    lambda d: d["jobItemDraft"]["components"][0].update(vendorSKU="made-up"),
    lambda d: d["jobItemDraft"]["components"][0].update(quantity="0"),
    lambda d: d["jobItemDraft"]["components"][0].update(quantity="1e6"),
    lambda d: d["jobItemDraft"]["components"][0].update(quantity="0.0000001"),
    lambda d: d["jobItemDraft"]["components"][0].update(unit=None),
    lambda d: d["jobItemDraft"]["components"][1].update(id="g1"),
    lambda d: d["jobItemDraft"]["opening"].update(kind="corner"),
    lambda d: d["jobItemDraft"]["opening"].update(widthCm=float("inf")),
    lambda d: d["jobItemDraft"]["opening"].update(widthCm=0),
    lambda d: d["jobItemDraft"].update(components=d["jobItemDraft"]["components"] * 11),
])
def test_rejects_bogus_approval_price_stock_geometry_and_identity(change):
    payload = draft()
    change(payload)
    with pytest.raises(ValueError):
        validate(payload)


@pytest.mark.parametrize("kind", ["corner", "l_shape"])
def test_nonrectangular_draft_requires_two_manual_legs(kind):
    doc = draft()
    doc["jobItemDraft"]["opening"] = {
        "kind": kind, "heightCm": 160,
        "widthCm": None, "legsCm": [156, 126.5]
    }
    assert validate(doc).opening.legsCm == [156.0, 126.5]
    for bad in ([156], [156, 0], [156, -4], [156, 126, 40]):
        doc["jobItemDraft"]["opening"]["legsCm"] = bad
        with pytest.raises(ValueError):
            validate(doc)


def complete_rectangular_item() -> dict:
    payload = draft()
    payload.update(
        name="Ventana mixta", quantity=3,
        width={"value": 124.5, "unit": "cm"},
        height={"value": 150, "unit": "cm"},
    )
    return payload


def test_correct_flat_item_and_draft_measurements_remain_compatible():
    assert validate(complete_rectangular_item()).opening.widthCm == 124.5
    payload = complete_rectangular_item()
    payload["jobItemDraft"]["opening"] = {
        "kind": "l_shape", "heightCm": 160, "legsCm": [156, 126.5]
    }
    payload.pop("width")
    payload["height"] = {"value": 160, "unit": "cm"}
    assert validate(payload).opening.legsCm == [156, 126.5]


@pytest.mark.parametrize("change", [
    lambda p: p.update(name="Different supplied name"),
    lambda p: p.update(quantity=2),
    lambda p: p.update(quantity=True),
    lambda p: p.update(width={"value": 124.6, "unit": "cm"}),
    lambda p: p.update(width={"value": 1245, "unit": "mm"}),
    lambda p: p.update(width={"value": True, "unit": "cm"}),
    lambda p: p.update(height={"value": 1500, "unit": "mm"}),
    lambda p: p.update(height={"value": float("nan"), "unit": "cm"}),
    lambda p: p.update(height={"value": 150, "unit": "cm", "claimed": "external"}),
])
def test_disagreeing_legacy_item_contract_cannot_bypass_draft(change):
    payload = complete_rectangular_item()
    change(payload)
    with pytest.raises(ValueError):
        validate(payload)


@pytest.mark.parametrize("kind", ["corner", "l_shape"])
def test_two_leg_draft_rejects_any_supplied_flat_width(kind):
    payload = complete_rectangular_item()
    payload["jobItemDraft"]["opening"] = {
        "kind": kind, "heightCm": 150, "legsCm": [156, 126.5]
    }
    with pytest.raises(ValueError):
        validate(payload)
    payload.pop("width")
    assert validate(payload).opening.legsCm == [156, 126.5]


@pytest.mark.skipif(not os.getenv("VITRIAL_PRIVATE_VILLA_CAMILA_FIXTURE"),
                    reason="explicit private source fixture required")
def test_villa_camila_sixteen_template_crosswalk_is_unapproved():
    path = Path(os.environ["VITRIAL_PRIVATE_VILLA_CAMILA_FIXTURE"])
    assert path.is_file() and not path.is_symlink()
    contents = path.read_bytes()
    assert hashlib.sha256(contents).hexdigest() == (
        "07d6452d29e175bea666a66f3f76f0c1099a101e2a5cc4ccf00dc1f1e75e97c1"
    )
    data = json.loads(contents)
    assert data["publicationStatus"] == "not-approved"
    assert len(data["templates"]) == 16
    assert len(data["rows"]) == 20
    assert {x["sourceGroup"] for x in data["templates"]} == set(range(1, 17))
    # We import *only* reference identity/name into a job-local pending draft.
    # No source customer amounts are copied into the API catalog.
    for item in data["templates"]:
        doc = draft()
        draft_data = doc["jobItemDraft"]
        draft_data["templateID"] = item["templateID"]
        draft_data["nameEsCO"] = item["nameEsCO"]
        draft_data["sourceGroup"] = item["sourceGroup"]
        draft_data["templateVersion"] = 1
        draft_data["templateReviewStatus"] = "pending_customer_review"
        parsed = validate(doc)
        assert parsed.templateID == item["templateID"]
        assert parsed.templateReviewStatus == "pending_customer_review"
        assert not hasattr(parsed, "unitPriceCOP")
