"""Tests for the authoritative component calculation endpoint."""
from __future__ import annotations

import pytest

from app.configurator_component_calculator import (
    AuthorizedQuantityOverride, CalculationInput, SectionInput,
    TrustedCatalogEntry, TrustedQuantityRule, calculate_components,
)
from app.component_calculation_routes import (
    AuthoritativeComponentCalculationRequest,
    AuthoritativeComponentCalculationResponse,
    _catalog, _rules,
)
from app.reference_data import ReferencePublicationPayload, ReferenceEntry, CompatibilityRule


class TestAuthoritativeCalculationRequest:
    """Validate the authoritative calculation request schema."""

    def test_valid_request(self):
        req = AuthoritativeComponentCalculationRequest(
            configuration=CalculationInput(
                itemID="item-1", configurationID="config-1", configurationVersion=3,
                itemTypeID="window", aluminumSystemID="system-7-44",
                sections=[SectionInput(id="left", type="sliding")],
                selectedComponentCatalogIDs=["roller"]
            ),
            catalogPublicationID="cat-1",
            catalogContentSHA256="a" * 64,
            rulesPublicationID="rules-1",
            rulesContentSHA256="b" * 64,
        )
        assert req.configuration.itemID == "item-1"
        assert req.catalogPublicationID == "cat-1"

    def test_invalid_sha256_rejected(self):
        with pytest.raises(Exception):
            AuthoritativeComponentCalculationRequest(
                configuration=CalculationInput(
                    itemID="item-1", configurationID="config-1", configurationVersion=3,
                    itemTypeID="window", sections=[], selectedComponentCatalogIDs=[]
                ),
                catalogPublicationID="cat-1",
                catalogContentSHA256="not-a-sha256",
                rulesPublicationID="rules-1",
                rulesContentSHA256="b" * 64,
            )

    def test_overrides_optional(self):
        req = AuthoritativeComponentCalculationRequest(
            configuration=CalculationInput(
                itemID="item-1", configurationID="config-1", configurationVersion=3,
                itemTypeID="window", sections=[], selectedComponentCatalogIDs=[]
            ),
            catalogPublicationID="cat-1",
            catalogContentSHA256="a" * 64,
            rulesPublicationID="rules-1",
            rulesContentSHA256="b" * 64,
            overrides=[
                AuthorizedQuantityOverride(
                    catalogEntryID="roller", quantity=5,
                    reason="Supplier specifies alternate count"
                )
            ],
        )
        assert len(req.overrides) == 1


class TestCatalogRulesExtraction:
    """Test extraction of trusted catalog/rules from authoritative publications."""

    def fixture_payload(self) -> ReferencePublicationPayload:
        return ReferencePublicationPayload(
            authoritative=True,
            source="test",
            entries=[
                ReferenceEntry(
                    id="roller", kind="hardware", code="RLR", name="Roller",
                    active=True, vendorSKU="SKU-1",
                    attributes={
                        "componentUnit": "each",
                        "itemTypeIDs": ["window"],
                        "systemIDs": ["system-7-44"],
                        "supplierID": "supplier-1"
                    }
                ),
                ReferenceEntry(
                    id="glass", kind="glass_type", code="GLS", name="Glass",
                    active=True, vendorSKU="SKU-2",
                    attributes={
                        "componentUnit": "m2",
                        "itemTypeIDs": ["window"],
                        "systemIDs": ["system-7-44"]
                    }
                ),
                ReferenceEntry(
                    id="not-a-component", kind="vendor_material", code="COL", name="Other",
                    active=True, vendorSKU="SKU-3",
                    attributes={}
                ),
            ],
            compatibilityRules=[
                CompatibilityRule(
                    id="rule-1", itemTypeID="window",
                    attributes={
                        "componentQuantityRule": {
                            "catalogEntryID": "roller",
                            "systemID": "system-7-44",
                            "mode": "per_matching_section",
                            "sectionType": "sliding",
                            "quantityPerOccurrence": 2
                        }
                    }
                ),
                CompatibilityRule(
                    id="rule-2", itemTypeID="window",
                    attributes={
                        "componentQuantityRule": {
                            "catalogEntryID": "glass",
                            "mode": "per_item",
                            "quantityPerOccurrence": 1.5
                        }
                    }
                ),
            ]
        )

    def test_catalog_extracts_only_technical_components(self):
        catalog = _catalog(self.fixture_payload())
        assert len(catalog) == 2
        ids = {c.id for c in catalog}
        assert ids == {"roller", "glass"}

    def test_catalog_preserves_attributes(self):
        catalog = _catalog(self.fixture_payload())
        roller = next(c for c in catalog if c.id == "roller")
        assert roller.unit == "each"
        assert roller.itemTypeIDs == frozenset(["window"])
        assert roller.systemIDs == frozenset(["system-7-44"])
        assert roller.active is True

    def test_rules_extracted_correctly(self):
        rules = _rules(self.fixture_payload())
        assert len(rules) == 2
        roller_rule = next(r for r in rules if r.catalogEntryID == "roller")
        assert roller_rule.mode == "per_matching_section"
        assert roller_rule.sectionType == "sliding"
        assert roller_rule.quantityPerOccurrence == 2
        glass_rule = next(r for r in rules if r.catalogEntryID == "glass")
        assert glass_rule.mode == "per_item"
        assert glass_rule.quantityPerOccurrence == 1.5


class TestAuthoritativeCalculationIntegration:
    """Integration tests for authoritative calculation flow."""

    def test_authoritative_calculation_persists_receipt(self):
        """Verify the calculate_components function produces a ready receipt."""
        cfg = CalculationInput(
            itemID="item-1", configurationID="config-1", configurationVersion=3,
            itemTypeID="window", aluminumSystemID="system-7-44",
            sections=[SectionInput(id="left", type="sliding")],
            selectedComponentCatalogIDs=["roller"]
        )
        catalog = [TrustedCatalogEntry(
            id="roller", name="Roller", unit="each",
            itemTypeIDs=frozenset(["window"]),
            systemIDs=frozenset(["system-7-44"])
        )]
        rules = [TrustedQuantityRule(
            id="rule-1", catalogEntryID="roller", itemTypeID="window",
            systemID="system-7-44", mode="per_matching_section",
            sectionType="sliding", quantityPerOccurrence=2
        )]
        receipt = calculate_components(
            cfg, trusted_catalog=catalog, trusted_rules=rules,
            catalog_version_id="cat-v1", rules_version_id="rules-v1"
        )
        assert receipt.ready
        assert len(receipt.components) == 1
        assert receipt.components[0].quantity == 2
        assert receipt.components[0].origin == "calculated"

    def test_authoritative_calculation_with_override(self):
        """Authorized override changes quantity and origin."""
        cfg = CalculationInput(
            itemID="item-1", configurationID="config-1", configurationVersion=3,
            itemTypeID="window", aluminumSystemID="system-7-44",
            sections=[SectionInput(id="left", type="sliding")],
            selectedComponentCatalogIDs=["roller"]
        )
        catalog = [TrustedCatalogEntry(
            id="roller", name="Roller", unit="each",
            itemTypeIDs=frozenset(["window"]),
            systemIDs=frozenset(["system-7-44"])
        )]
        rules = [TrustedQuantityRule(
            id="rule-1", catalogEntryID="roller", itemTypeID="window",
            systemID="system-7-44", mode="per_matching_section",
            sectionType="sliding", quantityPerOccurrence=2
        )]
        override = [AuthorizedQuantityOverride(
            catalogEntryID="roller", quantity=5,
            reason="Supplier confirms alternate count"
        )]
        receipt = calculate_components(
            cfg, trusted_catalog=catalog, trusted_rules=rules,
            catalog_version_id="cat-v1", rules_version_id="rules-v1",
            overrides=override, override_authorized=True
        )
        assert receipt.ready
        assert receipt.components[0].quantity == 5
        assert receipt.components[0].origin == "authorized_override"
        assert receipt.components[0].manualOverrideReason == "Supplier confirms alternate count"

    def test_unauthorized_override_rejected(self):
        """Override without authorization flag is rejected."""
        cfg = CalculationInput(
            itemID="item-1", configurationID="config-1", configurationVersion=3,
            itemTypeID="window", aluminumSystemID="system-7-44",
            sections=[SectionInput(id="left", type="sliding")],
            selectedComponentCatalogIDs=["roller"]
        )
        catalog = [TrustedCatalogEntry(
            id="roller", name="Roller", unit="each",
            itemTypeIDs=frozenset(["window"]),
            systemIDs=frozenset(["system-7-44"])
        )]
        rules = [TrustedQuantityRule(
            id="rule-1", catalogEntryID="roller", itemTypeID="window",
            systemID="system-7-44", mode="per_matching_section",
            sectionType="sliding", quantityPerOccurrence=2
        )]
        override = [AuthorizedQuantityOverride(
            catalogEntryID="roller", quantity=5,
            reason="Supplier confirms alternate count"
        )]
        receipt = calculate_components(
            cfg, trusted_catalog=catalog, trusted_rules=rules,
            catalog_version_id="cat-v1", rules_version_id="rules-v1",
            overrides=override, override_authorized=False
        )
        assert not receipt.ready
        assert receipt.components == []
        assert any("override" in issue for issue in receipt.issues)
