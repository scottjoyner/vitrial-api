"""Exact Swift-encoded SCRUM-44 preview fixture, source-only backend validation.

Cannot substitute for authenticated HTTP + migrated PostgreSQL acceptance:
SQLAlchemy is absent from this isolated worker environment.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re

import pytest
from pydantic import ValidationError

from app.configurator_component_calculator import CalculationInput
from app.request_size import limit_for_path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "contracts/scrum44/scrum44_component_preview_swift_wire_20261011.json"
SWIFT_BYTES_SHA256 = "818e8f982742a6086a35b99c54992ce1150dc0b649f9e4956816a88c7aeaf94d"
EXPECTED_FIELDS = {
    "configuration",
    "catalogPublicationID",
    "catalogContentSHA256",
    "rulesPublicationID",
    "rulesContentSHA256",
}
PREVIEW_PATH = "/api/v1/items/request-item/component-calculation/preview"


def decoded_fixture():
    raw = FIXTURE.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    payload = raw[:-1]
    assert hashlib.sha256(payload).hexdigest() == SWIFT_BYTES_SHA256
    return payload, json.loads(payload)


def test_exact_swift_encoded_bytes_are_bounded_for_both_clients():
    payload, body = decoded_fixture()
    assert len(payload) == 1229
    assert len(payload) <= 512 * 1024  # iOS preview client @ local 0d49a04
    assert len(payload) < limit_for_path(PREVIEW_PATH)
    assert set(body) == EXPECTED_FIELDS
    assert "overrides" not in body  # Swift first slice cannot request overrides.


def test_swift_request_decodes_into_server_calculation_input():
    _, body = decoded_fixture()
    source = CalculationInput.model_validate(body["configuration"])
    assert source.itemID == "request-item"
    assert source.configurationID == "request-config"
    assert source.configurationVersion == 7
    assert source.vanoWidthMM == 1560
    assert source.vanoHeightMM == 1260
    assert source.widthMM == 1500
    assert source.heightMM == 1220
    assert source.itemTypeID == "window"
    assert source.measurementStage == "reported"
    assert len(source.sections) == 2
    assert {s.id for s in source.sections} == {"sliding", "fixed"}
    assert len(source.additionalVanoSegments) == 1
    assert source.additionalVanoSegments[0].stage == "confirmed"
    assert source.additionalVanoSegments[0].widthMM == 1260
    assert len(source.glassPanels) == 1
    assert source.glassPanels[0].sectionID == "fixed"
    assert source.glassPanels[0].widthMM is None
    assert source.selectedComponentCatalogIDs == ["profile-approved", "roller-approved"]


def test_pinned_publications_match_server_schema_and_are_placeholders():
    _, body = decoded_fixture()
    assert body["catalogPublicationID"] == "published-catalog-1"
    assert body["rulesPublicationID"] == "published-rules-1"
    for key in ("catalogContentSHA256", "rulesContentSHA256"):
        assert re.fullmatch(r"[a-f0-9]{64}", body[key])
    # 'a' and 'b' digest examples are placeholders, not signed vendor rules.
    assert set(body["catalogContentSHA256"]) == {"a"}
    assert set(body["rulesContentSHA256"]) == {"b"}


def test_invalid_panel_section_fails_closed():
    _, body = decoded_fixture()
    body["configuration"]["glassPanels"][0]["sectionID"] = "unknown"
    with pytest.raises(ValidationError):
        CalculationInput.model_validate(body["configuration"])


def test_nonpositive_corner_leg_fails_closed():
    _, body = decoded_fixture()
    body["configuration"]["additionalVanoSegments"][0]["widthMM"] = 0
    with pytest.raises(ValidationError):
        CalculationInput.model_validate(body["configuration"])


def test_published_route_request_fields_still_match_swift_envelope():
    # AST-only because this environment lacks SQLAlchemy; do not claim HTTP pass.
    src = (ROOT / "app/configurator_component_routes.py").read_text()
    tree = ast.parse(src)
    matching = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "ComponentCalculationPreviewRequest"
    ]
    assert len(matching) == 1
    fields = {
        assignment.target.id
        for assignment in matching[0].body
        if isinstance(assignment, ast.AnnAssign)
        and isinstance(assignment.target, ast.Name)
    }
    assert fields == EXPECTED_FIELDS | {"overrides"}
    # iOS deliberately omits server-only override requests.


def test_fixture_contains_no_manufacturing_authority_or_secrets():
    payload, _ = decoded_fixture()
    forbidden = (
        b"authorization",
        b"accessToken",
        b"quotation",
        b"manufacturing",
        b"cutLength",
        b"secret",
    )
    for name in forbidden:
        assert name not in payload


def test_preview_body_guard_tracks_composed_route_prefix_and_declaration():
    """A router-prefix rename must fail before shipping an unguarded endpoint."""
    from app.request_size import _COMPONENT_PREVIEW_PATH

    src = (ROOT / "app/configurator_component_routes.py").read_text()
    tree = ast.parse(src)
    routers = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "router" for t in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "APIRouter"
    ]
    assert len(routers) == 1
    prefixes = [
        k.value.value
        for k in routers[0].value.keywords
        if k.arg == "prefix" and isinstance(k.value, ast.Constant)
    ]
    assert len(prefixes) == 1
    endpoint_suffixes = [
        decorator.args[0].value
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "preview_item_components"
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "post"
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
    ]
    assert len(endpoint_suffixes) == 1
    actual_route = prefixes[0] + endpoint_suffixes[0]
    assert actual_route.replace("{item_id}", "request-item") == PREVIEW_PATH
    assert _COMPONENT_PREVIEW_PATH.fullmatch(PREVIEW_PATH)
