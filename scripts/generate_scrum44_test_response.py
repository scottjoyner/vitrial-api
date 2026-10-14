#!/usr/bin/env python3
"""Generate TEST-ONLY SCRUM-44 calculation response from the real Swift input.

No vendor publications, DB, network, credentials, quotations or physical devices.
Do not deploy the fixture as a vendor-authoritative component calculation.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.configurator_component_calculator import (
    CalculationInput,
    TrustedCatalogEntry,
    TrustedQuantityRule,
    calculate_components,
)

ROOT = Path(__file__).resolve().parents[1]
REQUEST = ROOT / "contracts/scrum44/scrum44_component_preview_swift_wire_20261011.json"
OUTPUT = (
    ROOT / "contracts/scrum44/scrum44_component_preview_fixture_response_20261011.json"
)


def response() -> dict:
    request = json.loads(REQUEST.read_bytes())
    config = CalculationInput.model_validate(request["configuration"])
    catalog = [
        TrustedCatalogEntry(
            id="profile-approved",
            name="Fixture-only profile",
            unit="each",
            itemTypeIDs=frozenset({"window"}),
            systemIDs=frozenset({"system-744"}),
        ),
        TrustedCatalogEntry(
            id="roller-approved",
            name="Fixture-only roller",
            unit="each",
            itemTypeIDs=frozenset({"window"}),
            systemIDs=frozenset({"system-744"}),
        ),
    ]
    # These quantities are deliberately synthetic, not approved System 7-44.
    rules = [
        TrustedQuantityRule(
            id="fixture-only-profile-one",
            catalogEntryID="profile-approved",
            itemTypeID="window",
            systemID="system-744",
            mode="per_item",
            quantityPerOccurrence=1,
        ),
        TrustedQuantityRule(
            id="fixture-only-roller-two",
            catalogEntryID="roller-approved",
            itemTypeID="window",
            systemID="system-744",
            mode="per_item",
            quantityPerOccurrence=2,
        ),
    ]
    receipt = calculate_components(
        config,
        trusted_catalog=catalog,
        trusted_rules=rules,
        catalog_version_id="fixture-catalog-v1",
        rules_version_id="fixture-rules-v1",
    )
    if not receipt.ready or len(receipt.components) != 2:
        raise ValueError("test-only fixture calculator did not produce complete lines")
    catalog_items = [
        {
            "id": e.id,
            "name": e.name,
            "category": "hardware" if e.id == "roller-approved" else "aluminum_profile",
            "unit": e.unit,
            "active": e.active,
            "itemTypeIDs": sorted(e.itemTypeIDs),
            "systemIDs": sorted(e.systemIDs),
            "supplierID": None,
            "sku": None,
        }
        for e in catalog
    ]
    return {
        "schemaVersion": "vitrial.item-component-preview.v1",
        "authorization": "read_only_preview",
        "catalogContentSHA256": request["catalogContentSHA256"],
        "rulesContentSHA256": request["rulesContentSHA256"],
        "catalogEntries": catalog_items,
        "calculation": receipt.model_dump(mode="json", exclude_none=False),
    }


def wire_bytes() -> bytes:
    return json.dumps(
        response(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


if __name__ == "__main__":
    OUTPUT.write_bytes(wire_bytes() + b"\n")
    print(f"wrote TEST ONLY {OUTPUT.name}, {len(wire_bytes())} bytes")
