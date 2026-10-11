"""SCRUM-44 preview-only request cap: source-only ASGI evidence, no DB."""

from __future__ import annotations

import asyncio

import pytest

from app.request_size import RequestSizeLimitMiddleware, limit_for_path

PREVIEW = "/api/v1/items/item-123/component-calculation/preview"


def _scope(path: str, size_header: bytes | None = None) -> dict:
    headers = [(b"content-type", b"application/json")]
    if size_header is not None:
        headers.append((b"content-length", size_header))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": path,
        "headers": headers,
    }


def _request(path: str, chunks: list[bytes], size_header: bytes | None = None):
    calls = []
    remaining = list(chunks)
    messages = []

    async def downstream(scope, receive, send):
        calls.append(scope["path"])
        parts = []
        while True:
            item = await receive()
            if item["type"] != "http.request":
                break
            parts.append(item.get("body", b""))
            if not item.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"".join(parts)})

    async def receive():
        if remaining:
            return {
                "type": "http.request",
                "body": remaining.pop(0),
                "more_body": bool(remaining),
            }
        return {"type": "http.disconnect"}

    async def send(event):
        messages.append(event)

    asyncio.run(
        RequestSizeLimitMiddleware(downstream)(_scope(path, size_header), receive, send)
    )
    status = next(x["status"] for x in messages if x["type"] == "http.response.start")
    return status, calls


def test_preview_limit_is_only_exact_single_item_route():
    assert limit_for_path(PREVIEW) == 2 * 1024 * 1024
    for path in (
        "/api/v1/items/item-123",
        "/api/v1/items/item-123/configuration",
        "/api/v1/items/item-123/component-calculation",
        PREVIEW + "/next",
        "/api/v1/items/a/b/component-calculation/preview",
    ):
        assert limit_for_path(path) is None


def test_existing_guard_prefixes_are_unchanged():
    assert limit_for_path("/api/v1/auth/pair") == 16 * 1024
    assert limit_for_path("/api/v1/sync/push") == 4 * 1024 * 1024


def test_preview_env_override_only_affects_preview(monkeypatch):
    monkeypatch.setenv("VITRIAL_MAX_BODY_BYTES_API_V1_ITEMS_COMPONENT_PREVIEW", "32768")
    assert limit_for_path(PREVIEW) == 32768
    assert limit_for_path("/api/v1/items/item-123/configuration") is None
    assert limit_for_path("/api/v1/auth/pair") == 16 * 1024


@pytest.mark.parametrize("invalid", ["0", "-1", "0.1", "invalid"])
def test_bad_preview_override_uses_bounded_default(monkeypatch, invalid):
    monkeypatch.setenv("VITRIAL_MAX_BODY_BYTES_API_V1_ITEMS_COMPONENT_PREVIEW", invalid)
    assert limit_for_path(PREVIEW) == 2 * 1024 * 1024


def test_oversized_header_is_rejected_before_downstream():
    status, calls = _request(PREVIEW, [], str(3_000_000).encode("ascii"))
    assert status == 413
    assert calls == []


def test_lying_small_header_still_rejects_oversize_stream():
    status, calls = _request(PREVIEW, [b"x" * 1_400_000, b"x" * 900_000], b"10")
    assert status == 413
    assert calls == []


def test_missing_content_length_still_rejects_oversize_stream():
    status, calls = _request(PREVIEW, [b"x" * 1_700_000, b"x" * 600_000])
    assert status == 413
    assert calls == []


def test_under_limit_reaches_downstream():
    status, calls = _request(PREVIEW, [b'{"configuration":1}'])
    assert status == 200
    assert calls == [PREVIEW]


def test_unrelated_item_path_remains_unlimited():
    path = "/api/v1/items/item-123/configuration"
    status, calls = _request(path, [b"x" * 300000])
    assert status == 200
    assert calls == [path]


def test_exact_guard_stays_bound_to_registered_preview_route():
    """AST-only route linkage avoids importing production DB/auth modules."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app/configurator_component_routes.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    patterns = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            if (
                isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "post"
            ):
                pattern = decorator.args[0]
                if isinstance(pattern, ast.Constant) and isinstance(pattern.value, str):
                    patterns.append(pattern.value)
    assert "/{item_id}/component-calculation/preview" in patterns
    # Route prefix and registration are independent sources of potential drift.
    assigned_prefixes = [
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "APIRouter"
        for keyword in node.keywords
        if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant)
    ]
    assert "/api/v1/items" in assigned_prefixes
    main = source.with_name("main.py").read_text(encoding="utf-8")
    assert (
        "from app.configurator_component_routes import router as "
        "configurator_component_router" in main
    )
    assert "app.include_router(configurator_component_router)" in main
    assert limit_for_path(PREVIEW) == 2 * 1024 * 1024


def test_encoded_newline_matches_router_and_is_also_guarded():
    from urllib.parse import unquote
    from starlette.routing import compile_path

    route_pattern, _, _ = compile_path(
        "/api/v1/items/{item_id}/component-calculation/preview"
    )
    decoded = unquote(PREVIEW + "%0A")
    assert decoded.endswith("\n")
    assert route_pattern.match(decoded), "Starlette accepts terminal newline"
    assert limit_for_path(decoded) == 2 * 1024 * 1024
    status, calls = _request(decoded, [], b"3000000")
    assert status == 413 and calls == []

    for path in (
        PREVIEW,
        decoded,
        PREVIEW + "\r\n",
        PREVIEW + "/extra",
        "/api/v1/items/a/b/component-calculation/preview",
    ):
        assert bool(route_pattern.match(path)) == (limit_for_path(path) is not None)


def test_maximal_ascii_request_fits_provisional_2mib_cap():
    """Source type limits, not invented vendor quantity rules or iOS proof."""
    import json
    from app.configurator_component_calculator import (
        AuthorizedQuantityOverride,
        CalculationInput,
    )

    def ident(index, prefix):
        return (prefix + str(index).zfill(5)).ljust(128, "x")

    sections = [
        {
            "id": ident(i, "S"),
            "type": "sliding".ljust(64, "t"),
            "openingDirection": "left".ljust(64, "d"),
            "widthMM": 100.0,
            "heightMM": 220.0,
            "relativeWidth": 1.0,
        }
        for i in range(256)
    ]
    panels = [
        {
            "id": ident(i, "P"),
            "sectionID": ident(i, "S"),
            **{
                k: ident(i, label)
                for k, label in (
                    ("glassTypeID", "G"),
                    ("thicknessID", "T"),
                    ("colorID", "C"),
                    ("treatmentID", "H"),
                    ("classificationID", "L"),
                    ("dimensionSource", "D"),
                    ("calculationRuleID", "R"),
                )
            },
            "widthMM": 100.0,
            "heightMM": 220.0,
            "quantity": 1,
        }
        for i in range(256)
    ]
    vanos = [
        {"id": ident(i, "V"), "label": "x" * 256, "widthMM": 100.0, "heightMM": 200.0}
        for i in range(64)
    ]
    fields = {
        "itemID": "item-123",
        "configurationID": "b" * 256,
        "configurationVersion": 1,
        "itemTypeID": ident(1, "I"),
        "sections": sections,
        "glassPanels": panels,
        "additionalVanoSegments": vanos,
        "sectionWidthProvenance": {row["id"]: "operatorEntered" for row in sections},
        "hardwareIDs": [ident(i, "W") for i in range(256)],
        "accessoryIDs": [ident(i, "A") for i in range(256)],
        "selectedComponentCatalogIDs": [ident(i, "K") for i in range(256)],
    }
    configuration = CalculationInput.model_validate(fields)
    overrides = [
        AuthorizedQuantityOverride(
            catalogEntryID=ident(i, "K"), quantity=1.0, reason="r" * 2048
        )
        for i in range(256)
    ]
    body = {
        "configuration": configuration.model_dump(mode="json"),
        "overrides": [entry.model_dump(mode="json") for entry in overrides],
        "catalogPublicationID": "c" * 256,
        "catalogContentSHA256": "a" * 64,
        "rulesPublicationID": "d" * 256,
        "rulesContentSHA256": "b" * 64,
    }
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    assert 256 * 1024 < len(encoded) < limit_for_path(PREVIEW)
    status, calls = _request(PREVIEW, [encoded[:600_000], encoded[600_000:]])
    assert status == 200 and calls == [PREVIEW]
