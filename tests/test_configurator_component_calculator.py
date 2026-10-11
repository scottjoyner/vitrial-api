import pytest
from pydantic import ValidationError

from app.configurator_component_calculator import (
    AuthorizedQuantityOverride, CalculationInput, SectionInput,
    TrustedCatalogEntry, TrustedQuantityRule, calculate_components,
)


def fixture(item="item-1", width=950):
    return CalculationInput(
        itemID=item, configurationID=f"{item}-config", configurationVersion=3,
        itemTypeID="window", aluminumSystemID="system-7-44",
        widthMM=width, heightMM=2620,
        sections=[SectionInput(id="left", type="sliding"),
                  SectionInput(id="right", type="sliding"),
                  SectionInput(id="bottom", type="fixed")],
        selectedComponentCatalogIDs=["roller"]
    )


def catalog():
    return [TrustedCatalogEntry(id="roller", name="Published roller reference",
        unit="each", itemTypeIDs=frozenset(["window"]),
        systemIDs=frozenset(["system-7-44"]))]


def rules():
    # Test-only fictional counts; not a published System 7-44 technical formula.
    return [TrustedQuantityRule(
        id="fixture-only-two-per-slider", catalogEntryID="roller",
        itemTypeID="window", systemID="system-7-44",
        mode="per_matching_section", sectionType="sliding",
        quantityPerOccurrence=2
    )]


def run(cfg=None, catalog_rows=None, rule_rows=None, overrides=(), authorized=False):
    return calculate_components(
        cfg or fixture(), trusted_catalog=catalog_rows if catalog_rows is not None else catalog(),
        trusted_rules=rule_rows if rule_rows is not None else rules(),
        catalog_version_id="fixture-catalog-v1", rules_version_id="fixture-rules-v1",
        overrides=overrides, override_authorized=authorized
    )


def test_fixture_rules_are_explicit_and_traceable():
    receipt = run()
    assert receipt.ready and len(receipt.components) == 1
    item = receipt.components[0]
    assert item.quantity == 4
    assert item.origin == "calculated"
    assert item.sourceRuleID == "fixture-only-two-per-slider"
    assert item.itemID == "item-1"
    assert receipt.inputSHA256 and len(receipt.inputSHA256) == 64


def test_no_published_rule_never_fabricates_materials():
    receipt = run(rule_rows=[])
    assert not receipt.ready and receipt.components == []
    assert any("missing" in issue for issue in receipt.issues)


def test_missing_component_reference_and_system_incompatibility():
    absent = run(catalog_rows=[])
    assert absent.components == [] and "component reference unavailable" in absent.issues[0]
    changed = run(cfg=fixture(), catalog_rows=[TrustedCatalogEntry(
        id="roller", name="Wrong system", unit="each", systemIDs=frozenset(["other"]))])
    assert changed.components == []
    assert any("incompatible" in issue for issue in changed.issues)


def test_cross_item_quantities_are_not_shared():
    first = run(fixture("item-a"))
    second = run(fixture("item-b"))
    assert first.components[0].catalogEntryID == second.components[0].catalogEntryID
    assert first.components[0].itemID != second.components[0].itemID
    assert first.components[0].id != second.components[0].id
    assert first.inputSHA256 != second.inputSHA256


def test_same_version_input_change_triggers_new_digest():
    first = run(fixture(width=950))
    changed = run(fixture(width=960))
    assert first.inputSHA256 != changed.inputSHA256


def test_override_requires_actual_authorization_and_reason():
    override = [AuthorizedQuantityOverride(
        catalogEntryID="roller", quantity=5, reason="Supplier specifies alternate count"
    )]
    denied = run(overrides=override)
    assert not denied.ready and denied.components == []
    permitted = run(overrides=override, authorized=True)
    assert permitted.ready
    assert permitted.components[0].quantity == 5
    assert permitted.components[0].origin == "authorized_override"
    assert permitted.components[0].manualOverrideReason
    with pytest.raises(ValidationError):
        AuthorizedQuantityOverride(catalogEntryID="roller", quantity=5, reason="no")


def test_unknown_override_is_rejected():
    extra = [AuthorizedQuantityOverride(
        catalogEntryID="foreign", quantity=2, reason="Justified foreign override"
    )]
    receipt = run(overrides=extra, authorized=True)
    assert not receipt.ready and receipt.components == []
    assert any("override" in issue for issue in receipt.issues)


def test_inputs_reject_nonfinite_counts_and_duplicate_sections():
    with pytest.raises(ValidationError):
        fixture(width=float("nan"))
    with pytest.raises(ValidationError):
        TrustedQuantityRule(id="bad", catalogEntryID="roller",
            itemTypeID="window", mode="per_item", quantityPerOccurrence=float("inf"))
    with pytest.raises(ValidationError):
        CalculationInput(
            itemID="i", configurationID="c", configurationVersion=1,
            itemTypeID="window", sections=[SectionInput(id="same", type="fixed"),
                                          SectionInput(id="same", type="sliding")]
        )
def test_glass_profile_and_additional_vano_changes_alter_input_lineage():
    from app.configurator_component_calculator import GlassPanelInput, VanoSegmentInput

    basis = fixture().model_dump()
    original = run(CalculationInput.model_validate(basis))

    glass_changed = CalculationInput.model_validate({
        **basis, "glassTypeID": "glass-tempered",
        "glassPanels": [
            GlassPanelInput(id="g1", sectionID="left", glassTypeID="glass-tempered",
                            thicknessID="glass-6mm", quantity=1).model_dump()
        ]
    })
    segment_changed = CalculationInput.model_validate({
        **basis, "additionalVanoSegments": [
            VanoSegmentInput(id="return", label="corner return",
                             widthMM=1260, heightMM=1260, stage="confirmed").model_dump()
        ]
    })
    profile_changed = CalculationInput.model_validate({
        **basis, "aluminumProfileID": "profile-7-44"
    })
    for changed in [glass_changed, segment_changed, profile_changed]:
        assert run(changed).inputSHA256 != original.inputSHA256


def test_orphan_panels_and_duplicate_segments_are_rejected():
    from app.configurator_component_calculator import GlassPanelInput, VanoSegmentInput

    basis = fixture().model_dump()
    with pytest.raises(ValidationError):
        CalculationInput.model_validate({
            **basis,
            "glassPanels": [
                GlassPanelInput(id="p", sectionID="unknown").model_dump()
            ]
        })
    with pytest.raises(ValidationError):
        CalculationInput.model_validate({
            **basis,
            "additionalVanoSegments": [
                VanoSegmentInput(id="dup", label="leg 1", widthMM=1200, heightMM=1500).model_dump(),
                VanoSegmentInput(id="dup", label="leg 2", widthMM=1100, heightMM=1500).model_dump(),
            ],
        })


def test_discrete_hardware_requires_integral_quantity_and_bound():
    # Test-only counts; a fractional roller, panel or extreme order is invalid.
    bad_rule = rules()[0].model_copy(update={"quantityPerOccurrence": 1.25})
    fractional = run(rule_rows=[bad_rule])
    assert not fractional.ready
    assert fractional.components == []
    assert any("non-integral" in issue for issue in fractional.issues)

    oversized_rule = rules()[0].model_copy(update={"quantityPerOccurrence": 600_000})
    too_many = run(rule_rows=[oversized_rule])
    assert not too_many.ready
    assert too_many.components == []
    assert any("safety bound" in issue for issue in too_many.issues)


def test_fractional_quantity_override_on_count_unit_fails_closed():
    override = [AuthorizedQuantityOverride(
        catalogEntryID="roller", quantity=2.5,
        reason="Requested half of a discrete roller unit"
    )]
    receipt = run(overrides=override, authorized=True)
    assert not receipt.ready
    assert receipt.components == []
    assert any("non-integral" in issue for issue in receipt.issues)
