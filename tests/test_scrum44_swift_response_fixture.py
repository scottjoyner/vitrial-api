"""Test-only server calculator response fixture decoded by the Swift client.

The envelope is assembled without DB publication authority. Do not represent
this as a vendor-approved or authenticated production HTTP response.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.configurator_component_calculator import CalculationReceipt
from scripts.generate_scrum44_test_response import response, wire_bytes

ROOT = Path(__file__).resolve().parents[1]
RESPONSE = (
    ROOT / "contracts/scrum44/scrum44_component_preview_fixture_response_20261011.json"
)
REQUEST = ROOT / "contracts/scrum44/scrum44_component_preview_swift_wire_20261011.json"
EXPECTED_SHA = "c27005406f20b96ace0dafe945682144f7d21bc0211bfed6e85668f8b8e72e10"


def test_python_calculator_fixture_is_byte_pinned():
    raw = RESPONSE.read_bytes()
    assert raw.endswith(b"\n")
    assert raw[:-1] == wire_bytes()
    assert len(raw[:-1]) == 1720
    assert hashlib.sha256(raw[:-1]).hexdigest() == EXPECTED_SHA


def test_response_has_read_only_non_authoritative_fixture_boundary():
    original = json.loads(REQUEST.read_bytes())
    recorded = json.loads(RESPONSE.read_bytes())
    assert recorded == response()
    assert recorded["schemaVersion"] == "vitrial.item-component-preview.v1"
    assert recorded["authorization"] == "read_only_preview"
    assert recorded["catalogContentSHA256"] == original["catalogContentSHA256"]
    assert recorded["rulesContentSHA256"] == original["rulesContentSHA256"]
    assert len(recorded["catalogEntries"]) == 2
    assert all("Fixture-only" in item["name"] for item in recorded["catalogEntries"])
    assert not any(item["supplierID"] for item in recorded["catalogEntries"])


def test_calculator_receipt_revalidates_as_pydantic_and_remains_test_only():
    recorded = json.loads(RESPONSE.read_bytes())
    receipt = CalculationReceipt.model_validate(recorded["calculation"])
    assert receipt.ready
    assert receipt.itemID == "request-item"
    assert receipt.configurationID == "request-config"
    assert receipt.configurationVersion == 7
    assert len(receipt.components) == 2
    assert {x.catalogEntryID: x.quantity for x in receipt.components} == {
        "profile-approved": 1.0,
        "roller-approved": 2.0,
    }
    assert all(x.origin == "calculated" for x in receipt.components)
    assert all(x.sourceRuleID.startswith("fixture-only-") for x in receipt.components)


def test_fixture_receipt_has_independent_digest_from_publication_pins():
    recorded = json.loads(RESPONSE.read_bytes())
    assert len(recorded["calculation"]["inputSHA256"]) == 64
    assert recorded["calculation"]["inputSHA256"] != recorded["catalogContentSHA256"]
    assert recorded["calculation"]["inputSHA256"] != recorded["rulesContentSHA256"]


def test_fixture_contains_no_quotes_procurement_or_secrets():
    raw = RESPONSE.read_bytes()
    for forbidden in (
        b"authorization_token",
        b"secret",
        b"manufacturing",
        b"purchaseOrder",
        b"quotationID",
        b"realVendor",
    ):
        assert forbidden not in raw
