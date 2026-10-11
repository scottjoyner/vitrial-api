"""SCRUM-44 preview-only body guard settings in deployment declarations."""

from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml

from app.request_size import limit_for_path

ROOT = Path(__file__).resolve().parents[1]
KEY = "VITRIAL_MAX_BODY_BYTES_API_V1_ITEMS_COMPONENT_PREVIEW"
PREVIEW = "/api/v1/items/test-item/component-calculation/preview"
DEFAULT = 2 * 1024 * 1024
COMPOSE = ("deploy/compose.production.yml", "deploy/compose.acceptance.yml")
EXAMPLES = (".env.example", "deploy/env.production.example")


@pytest.mark.parametrize("filename", COMPOSE)
def test_compose_passes_preview_only_setting_to_all_api_jobs(filename):
    doc = yaml.safe_load((ROOT / filename).read_text())
    shared = doc["x-api-environment"]
    assert shared[KEY] == "$" + "{" + KEY + ":-2097152}"
    assert KEY not in {k for k in shared if k.endswith("COMPONENTS_OVERRIDE")}
    for service in ("api", "migrate"):
        assert doc["services"][service]["environment"] == shared


@pytest.mark.parametrize("filename", EXAMPLES)
def test_env_template_matches_source_default(filename):
    doc = (ROOT / filename).read_text()
    matches = re.findall(r"^" + KEY + r"=(\d+)$", doc, flags=re.MULTILINE)
    assert matches == [str(DEFAULT)]


def test_runtime_default_remains_bounded_and_preview_specific(monkeypatch):
    monkeypatch.delenv(KEY, raising=False)
    assert limit_for_path(PREVIEW) == DEFAULT
    assert limit_for_path("/api/v1/items/test-item/configuration") is None
    assert limit_for_path("/api/v1/sync/push") == 4 * 1024 * 1024


def test_source_override_is_configurable_but_cannot_relax_default_silently(monkeypatch):
    monkeypatch.setenv(KEY, "2621440")
    assert limit_for_path(PREVIEW) == 2_621_440
    monkeypatch.setenv(KEY, "not-a-number")
    assert limit_for_path(PREVIEW) == DEFAULT


def test_documented_edge_ceiling_is_not_below_preview_default():
    doc = (ROOT / "deploy/env.production.example").read_text()
    matching = re.findall(r"^CADDY_MAX_REQUEST_BODY=(\d+)MB$", doc, flags=re.MULTILINE)
    assert matching and int(matching[0]) * 1_000_000 > DEFAULT


def test_no_implicit_item_wide_rate_limit_added():
    from app.rate_limit import THROTTLED_PATH_PREFIXES

    assert not any(PREVIEW.startswith(prefix) for prefix in THROTTLED_PATH_PREFIXES)


def test_deployment_has_not_granted_override_capability():
    for filename in COMPOSE + EXAMPLES:
        doc = (ROOT / filename).read_text()
        assert "item.components.override" not in doc
