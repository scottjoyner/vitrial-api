import base64
import json
from pathlib import Path

import pytest

from app.idempotency import request_fingerprint
from app.main import app
from app.reference_data import BASELINE_PUBLICATIONS, publication_sha256
from app.schemas import SyncBatch, SyncRecord
from app.sync_service import InvalidMutation, decode_payload
from app.sync_v2 import SyncBatchV2, SyncResultV2, _to_v1_record

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "contracts" / "backend" / "v1"
V2 = ROOT / "contracts" / "backend" / "v2"


def load(root: Path, name: str):
    return json.loads((root / name).read_text(encoding="utf-8"))


def test_v1_contract_remains_base64_bytes_and_unchanged_in_shape():
    fixture = load(V1, "sync-push.request.json")
    batch = SyncBatch.model_validate(fixture)
    assert "protocolVersion" not in fixture
    assert "entitySchemaVersion" not in fixture["records"][0]
    assert isinstance(fixture["records"][0]["payload"], str)
    assert isinstance(json.loads(base64.b64decode(fixture["records"][0]["payload"])), dict)
    assert decode_payload(batch.records[0])


def test_v2_contract_uses_native_json_and_explicit_versions():
    push_fixture = load(V2, "sync-push.request.json")
    pull_fixture = load(V2, "sync-pull.response.json")
    result_fixture = load(V2, "sync-push.response.json")

    push = SyncBatchV2.model_validate(push_fixture)
    pull = SyncBatchV2.model_validate(pull_fixture)
    result = SyncResultV2.model_validate(result_fixture)

    assert push.protocolVersion == 2
    assert push.records[0].entitySchemaVersion == 1
    assert isinstance(push.records[0].payload, dict)
    assert pull.records[0].payload == push.records[0].payload
    assert pull.records[0].clientMutationID == push.records[0].clientMutationID
    assert pull.records[0].serverRevision == 1
    assert result.acceptedRecordIDs == [push.records[0].id]


def test_v2_adapter_canonicalizes_json_without_reinterpreting_v1_bytes():
    v2 = SyncBatchV2.model_validate(load(V2, "sync-push.request.json"))
    adapted = _to_v1_record(v2.records[0])
    assert decode_payload(adapted) == v2.records[0].payload

    canonical = json.dumps(
        v2.records[0].payload,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    v1_wire = SyncRecord(
        id=v2.records[0].id,
        entityType=v2.records[0].entityType,
        entityID=v2.records[0].entityID,
        updatedAt=v2.records[0].updatedAt,
        payload=base64.b64encode(canonical),
        baseServerRevision=v2.records[0].baseServerRevision,
        clientMutationID=v2.records[0].clientMutationID,
        deletedAt=v2.records[0].deletedAt,
    )
    # Same semantic JSON on another protocol is not silently treated as the same wire request.
    assert request_fingerprint(v1_wire) != request_fingerprint(adapted)


def test_v2_rejects_unknown_entity_schema_version():
    fixture = load(V2, "sync-push.request.json")
    fixture["records"][0]["entitySchemaVersion"] = 2
    record = SyncBatchV2.model_validate(fixture).records[0]
    with pytest.raises(InvalidMutation, match="unsupported entity schema version"):
        _to_v1_record(record)


def test_reference_baselines_are_stable_versioned_and_do_not_fabricate_pricing():
    assert {publication.kind for publication in BASELINE_PUBLICATIONS} == {
        "catalog", "compatibility_rules", "price_book"
    }
    for publication in BASELINE_PUBLICATIONS:
        assert len(publication_sha256(publication)) == 64
        assert publication.versionID

    catalog = next(p for p in BASELINE_PUBLICATIONS if p.kind == "catalog")
    price_book = next(p for p in BASELINE_PUBLICATIONS if p.kind == "price_book")
    assert any(entry.id == "al-system-sliding" for entry in catalog.payload.entries)
    assert price_book.payload.priceBookLines == []
    assert price_book.payload.authoritative is False
    assert price_book.payload.metadata["configured"] is False


def test_openapi_exposes_reference_publications_and_explicit_v2_routes():
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/reference-data/manifest",
        "/api/v1/reference-data/current",
        "/api/v1/reference-data/publications",
        "/api/v1/reference-data/publications/{publicationID}",
        "/api/v2/version",
        "/api/v2/sync/push",
        "/api/v2/sync/pull",
    ):
        assert path in paths
