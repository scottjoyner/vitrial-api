import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.auth import Principal
from app.ownership import (
    AuthorizationRejected,
    _require_canonical_assembly_reference_pins,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "contracts" / "assembly" / "aluminum-window-3panel-v1.json"


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    async def scalars(self, _query):
        return _ScalarRows(self.rows)


def _configuration() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["configuration"]


def _principal() -> Principal:
    return Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=1,
        capabilities=frozenset({"item.configuration.manage"}),
        all_customers=True,
        all_projects=True,
        customer_ids=frozenset(),
        project_ids=frozenset(),
    )


def _row(kind: str, version: str, digest: str):
    return SimpleNamespace(
        kind=kind,
        version_id=version,
        content_sha256=digest,
    )


def _canonical_rows():
    return [
        _row("catalog", "configurator-catalog-v2", "1" * 64),
        _row("compatibility_rules", "configurator-rules-v1", "2" * 64),
        _row("price_book", "price-book-unconfigured-v1", "3" * 64),
    ]


@pytest.mark.asyncio
async def test_frozen_assembly_requires_all_three_canonical_reference_publications():
    await _require_canonical_assembly_reference_pins(
        _FakeDB(_canonical_rows()),
        _principal(),
        _configuration(),
    )


@pytest.mark.asyncio
async def test_frozen_assembly_rejects_missing_historical_publication():
    rows = _canonical_rows()
    rows = [row for row in rows if row.kind != "price_book"]

    with pytest.raises(
        AuthorizationRejected,
        match="assembly reference publication is not canonical: price_book",
    ):
        await _require_canonical_assembly_reference_pins(
            _FakeDB(rows),
            _principal(),
            _configuration(),
        )


@pytest.mark.asyncio
async def test_frozen_assembly_rejects_digest_drift_for_same_version_id():
    rows = _canonical_rows()
    rows[0] = _row(
        "catalog",
        "configurator-catalog-v2",
        "f" * 64,
    )

    with pytest.raises(
        AuthorizationRejected,
        match="assembly reference publication digest mismatch: catalog",
    ):
        await _require_canonical_assembly_reference_pins(
            _FakeDB(rows),
            _principal(),
            _configuration(),
        )


@pytest.mark.asyncio
async def test_successor_publications_do_not_invalidate_historical_pins():
    rows = _canonical_rows() + [
        _row("catalog", "configurator-catalog-v3", "a" * 64),
        _row("compatibility_rules", "configurator-rules-v2", "b" * 64),
        _row("price_book", "price-book-v2", "c" * 64),
    ]

    # The helper deliberately resolves the exact frozen version+digest pair.
    # It never requires a completed ConfigurationVersion to point at today's current publication.
    await _require_canonical_assembly_reference_pins(
        _FakeDB(rows),
        _principal(),
        _configuration(),
    )


@pytest.mark.asyncio
async def test_legacy_configuration_without_assembly_remains_unaffected():
    await _require_canonical_assembly_reference_pins(
        _FakeDB([]),
        _principal(),
        {
            "id": "configuration-legacy",
            "itemID": "item-legacy",
            "version": 1,
        },
    )
