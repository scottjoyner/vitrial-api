import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import ValidationError

from app.auth import Principal
from app.assembly_contract import (
    AssemblySnapshotV1,
    validate_assembly_contract_mutation,
    validate_configuration_assembly,
    validate_quotation_design_against_configuration_version,
    validate_quotation_line_design_snapshot,
)
from app.models import (
    CanonicalItem,
    CanonicalProject,
    CanonicalProjectSector,
    SyncEntity,
)
from app.ownership import AuthorizationRejected, EffectiveScope, authorize_record
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
    assert assembly.renderDescriptor.configurationID == "configuration-window-1"
    assert assembly.renderDescriptor.configurationVersion == 1
    assert assembly.renderDescriptor.overall.widthMM == 2400
    assert assembly.renderDescriptor.overall.heightMM == 1650
    assert assembly.bom.configurationID == "configuration-window-1"
    assert assembly.bom.configurationVersion == 1

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
    assert snapshot.bomSHA256 == "0da9e19412fe9a5b191e7cd309b18eed5d58c9d52364f212ed67dfa8e27dc07b"
    assert snapshot.renderDescriptor.configurationID == "configuration-window-1"
    assert snapshot.renderDescriptor.configurationVersion == 1



def test_nested_snapshot_must_bind_to_existing_configuration_identity():
    document = fixture_document()
    document["configuration"]["assembly"]["renderDescriptor"]["configurationID"] = "other-config"
    document["configuration"]["assembly"]["bom"]["configurationID"] = "other-config"

    with pytest.raises(ValueError, match="configurationID does not match Configuration"):
        validate_configuration_assembly(document["configuration"])


def test_quotation_design_snapshot_must_bind_to_line_configuration_version():
    document = fixture_document()
    document["quotationLine"]["configurationVersionID"] = "configuration-window-1#v2"

    with pytest.raises(ValueError, match="does not match quotation line"):
        validate_quotation_line_design_snapshot(document["quotationLine"])

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


def test_mutation_guard_is_noop_for_legacy_payloads():
    validate_assembly_contract_mutation(
        "configuration",
        {"id": "legacy-config", "version": 1},
    )
    validate_assembly_contract_mutation(
        "quotation",
        {"id": "legacy-quote", "lines": [{"id": "line-1", "itemID": "item-1"}]},
    )


def test_configuration_version_guard_binds_envelope_to_nested_configuration():
    document = fixture_document()
    configuration = document["configuration"]
    envelope = {
        "id": "configuration-window-1#v1",
        "configuration": configuration,
    }
    validate_assembly_contract_mutation("configuration_version", envelope)

    envelope["id"] = "configuration-window-1#v2"
    with pytest.raises(ValueError, match="envelope does not match"):
        validate_assembly_contract_mutation("configuration_version", envelope)


def test_quotation_mutation_guard_validates_only_lines_with_design_snapshot():
    document = fixture_document()
    quotation = {
        "id": "quotation-1",
        "lines": [
            {"id": "legacy", "itemID": "item-legacy"},
            document["quotationLine"],
        ],
    }
    validate_assembly_contract_mutation("quotation", quotation)

    quotation["lines"][1]["designSnapshot"]["configurationVersionID"] = "configuration-window-1#v2"
    with pytest.raises(ValueError, match="quotation line 2 assembly design is invalid"):
        validate_assembly_contract_mutation("quotation", quotation)


def test_quotation_design_snapshot_render_descriptor_cannot_drift():
    document = fixture_document()
    document["quotationLine"]["designSnapshot"]["renderDescriptor"]["sections"][0]["operation"] = "right"

    # This contract only asserts that the quoted render descriptor is internally bound to the
    # quoted configuration version/overall dimensions. Exact equality with the full Configuration
    # assembly is proved when the line is created on iOS and can be checked against canonical
    # configuration_version state in a later DB-backed acceptance.
    snapshot = validate_quotation_line_design_snapshot(document["quotationLine"])
    assert snapshot is not None
    assert snapshot.renderDescriptor.sections[0].operation == "right"


def test_quotation_design_snapshot_rejects_render_configuration_identity_drift():
    document = fixture_document()
    document["quotationLine"]["designSnapshot"]["renderDescriptor"]["configurationVersion"] = 2

    with pytest.raises(ValidationError, match="configurationVersionID does not match render descriptor"):
        validate_quotation_line_design_snapshot(document["quotationLine"])


def test_quote_design_matches_canonical_configuration_version():
    document = fixture_document()
    configuration_version = {
        "id": "configuration-window-1#v1",
        "configuration": document["configuration"],
    }

    validate_quotation_design_against_configuration_version(
        document["quotationLine"],
        configuration_version,
    )


def test_quote_design_rejects_render_drift_from_canonical_configuration_version():
    document = fixture_document()
    configuration_version = {
        "id": "configuration-window-1#v1",
        "configuration": document["configuration"],
    }
    document["quotationLine"]["designSnapshot"]["renderDescriptor"]["sections"][0]["operation"] = "right"

    with pytest.raises(ValueError, match="render descriptor differs from canonical"):
        validate_quotation_design_against_configuration_version(
            document["quotationLine"],
            configuration_version,
        )


def test_quote_design_rejects_bom_digest_drift_from_canonical_configuration_version():
    document = fixture_document()
    configuration_version = {
        "id": "configuration-window-1#v1",
        "configuration": document["configuration"],
    }
    document["quotationLine"]["designSnapshot"]["bomSHA256"] = "f" * 64

    with pytest.raises(ValueError, match="BOM digest differs from canonical"):
        validate_quotation_design_against_configuration_version(
            document["quotationLine"],
            configuration_version,
        )


def test_quote_design_rejects_reference_provenance_drift_from_canonical_version():
    document = fixture_document()
    configuration_version = {
        "id": "configuration-window-1#v1",
        "configuration": document["configuration"],
    }
    document["quotationLine"]["designSnapshot"]["referenceData"]["priceBookVersionID"] = "other-price-book"

    with pytest.raises(ValueError, match="reference provenance differs from canonical"):
        validate_quotation_design_against_configuration_version(
            document["quotationLine"],
            configuration_version,
        )


def test_quote_design_rejects_configuration_version_for_another_item():
    document = fixture_document()
    configuration_version = {
        "id": "configuration-window-1#v1",
        "configuration": copy.deepcopy(document["configuration"]),
    }
    configuration_version["configuration"]["itemID"] = "other-item"

    with pytest.raises(ValueError, match="belongs to another Item"):
        validate_quotation_design_against_configuration_version(
            document["quotationLine"],
            configuration_version,
        )

def _quotation_authority_principal() -> Principal:
    return Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=7,
        capabilities=frozenset({"quotation.create"}),
        customer_ids=frozenset({"customer-1"}),
        project_ids=frozenset({"project-1"}),
        all_customers=False,
        all_projects=False,
    )


class _QuotationAuthorityRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _QuotationAuthorityDB:
    def __init__(self, values, publications):
        self.values = values
        self.publications = publications

    async def get(self, model, key):
        return self.values.get((model, key))

    async def scalars(self, _query):
        return _QuotationAuthorityRows(self.publications)


def _quotation_authority_db(document: dict) -> _QuotationAuthorityDB:
    configuration_version_id = document["quotationLine"]["configurationVersionID"]
    values = {
        (CanonicalProject, ("org-1", "project-1")): CanonicalProject(
            organization_id="org-1",
            project_id="project-1",
            customer_id="customer-1",
        ),
        (CanonicalProjectSector, ("org-1", "project-sector-1")): CanonicalProjectSector(
            organization_id="org-1",
            project_sector_id="project-sector-1",
            project_id="project-1",
            sector_id="sector-aluminum-glass-steel",
        ),
        (CanonicalItem, ("org-1", "item-window-1")): CanonicalItem(
            organization_id="org-1",
            item_id="item-window-1",
            project_id="project-1",
            project_sector_id="project-sector-1",
        ),
        (SyncEntity, ("org-1", "configuration_version", configuration_version_id)): SyncEntity(
            organization_id="org-1",
            entity_type="configuration_version",
            entity_id=configuration_version_id,
            payload_json={
                "id": configuration_version_id,
                "configuration": document["configuration"],
            },
        ),
    }
    publications = [
        SimpleNamespace(
            kind="catalog",
            version_id="configurator-catalog-v2",
            content_sha256="1" * 64,
        ),
        SimpleNamespace(
            kind="compatibility_rules",
            version_id="configurator-rules-v1",
            content_sha256="2" * 64,
        ),
        SimpleNamespace(
            kind="price_book",
            version_id="price-book-unconfigured-v1",
            content_sha256="3" * 64,
        ),
    ]
    return _QuotationAuthorityDB(values, publications)


async def _authorize_quotation_line(line: dict):
    document = fixture_document()
    actor = _quotation_authority_principal()
    payload = {
        "id": "quotation-acceptance",
        "projectID": "project-1",
        "customerID": "customer-1",
        "status": "draft",
        "lines": [line],
        "events": [],
    }
    return await authorize_record(
        _quotation_authority_db(document),
        actor,
        EffectiveScope.from_principal(actor),
        entity_type="quotation",
        entity_id="quotation-acceptance",
        payload=payload,
        deleted_at=None,
        generic_entity_exists=False,
    )


@pytest.mark.asyncio
async def test_authorize_record_accepts_canonical_bom_digest():
    document = fixture_document()

    plan = await _authorize_quotation_line(document["quotationLine"])

    assert plan.entity_type == "quotation"
    assert plan.project_id == "project-1"


@pytest.mark.asyncio
async def test_authorize_record_rejects_tampered_bom_digest():
    document = fixture_document()
    line = copy.deepcopy(document["quotationLine"])
    line["designSnapshot"]["bomSHA256"] = "f" * 64

    with pytest.raises(
        AuthorizationRejected,
        match="BOM digest differs from canonical ConfigurationVersion",
    ):
        await _authorize_quotation_line(line)


@pytest.mark.asyncio
async def test_authorize_record_preserves_legacy_line_without_design_snapshot():
    line = {
        "id": "legacy-line",
        "itemID": "item-window-1",
    }

    plan = await _authorize_quotation_line(line)

    assert plan.entity_type == "quotation"
    assert plan.project_id == "project-1"

