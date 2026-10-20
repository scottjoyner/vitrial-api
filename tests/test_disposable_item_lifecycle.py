"""Network-free, fail-closed checks for SCRUM-49/51 local-only Item canary."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SOURCE))
SPEC = importlib.util.spec_from_file_location(
    "local_item_canary", SOURCE / "verify_disposable_item_lifecycle.py"
)
target = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(target)


def test_item_record_is_v1_base64_and_uniquely_mutated():
    record = target.make_record(
        entity_type="item", entity_id="synthetic-item",
        payload={"name": "Synthetic", "quantity": 1},
        nonce="abcd", sequence="created",
    )
    assert record["id"] == "synthetic-record-created-abcd"
    assert record["clientMutationID"] == "synthetic-mutation-created-abcd"
    assert record["baseServerRevision"] is None
    assert json.loads(base64.b64decode(record["payload"], validate=True)) == {
        "name": "Synthetic", "quantity": 1,
    }
    other = target.make_record(
        entity_type="item", entity_id="synthetic-item",
        payload={}, nonce="abcd", sequence="edit", base_revision=17
    )
    assert other["baseServerRevision"] == 17
    assert other["clientMutationID"] != record["clientMutationID"]


@pytest.mark.parametrize("kind,id_,nonce,sequence", [
    ("", "id", "nonce", "seq"),
    ("item", "", "nonce", "seq"),
    ("item", "id", "", "seq"),
    ("item", "id", "nonce", ""),
    (None, "id", "nonce", "seq"),
])
def test_requires_all_record_identifiers(kind, id_, nonce, sequence):
    with pytest.raises(ValueError):
        target.make_record(
            entity_type=kind, entity_id=id_, payload={},
            nonce=nonce, sequence=sequence,
        )


@pytest.mark.parametrize("row", [
    {"payload": ""}, {"payload": "!invalid-base64!"}, {"payload": None},
    {"payload": "A" * 32769},
    {"payload": base64.b64encode(b"[]").decode()},
    {"payload": base64.b64encode(b"not-json").decode()},
])
def test_bad_item_wire_never_passes(row):
    with pytest.raises((ValueError, json.JSONDecodeError)):
        target.decoded_item(row)


def test_valid_item_wire_round_trip():
    payload = {"id": "synthetic-item", "jobItemDraft": {"components": []}}
    assert target.decoded_item(
        {"payload": base64.b64encode(json.dumps(payload).encode()).decode()}
    ) == payload


def test_pull_rejects_duplicate_or_missing_item_rows(monkeypatch):
    for rows in ([], [{"entityType": "item", "entityID": "i"}] * 2):
        monkeypatch.setattr(target, "exchange", lambda *a, **kw: {"records": rows})
        with pytest.raises(RuntimeError, match="exactly one current Item"):
            target.pull_item("http://127.0.0.1:1234", "fake-bearer", "i")


def test_pull_never_accepts_non_array(monkeypatch):
    monkeypatch.setattr(target, "exchange", lambda *a, **kw: {"records": None})
    with pytest.raises(RuntimeError, match="missing records"):
        target.pull_item("http://127.0.0.1:1234", "fake-bearer", "i")


@pytest.mark.parametrize("accepted,rejected", [
    ([], []), (["wrong-id"], []), ([], ["synthetic-record-a-b"]),
    (["synthetic-record-a-b"], ["synthetic-record-a-b"]),
])
def test_push_requires_exact_acceptance(monkeypatch, accepted, rejected):
    monkeypatch.setattr(target, "exchange", lambda *a, **kw: {
        "acceptedRecordIDs": accepted, "rejectedRecordIDs": rejected,
    })
    with pytest.raises(RuntimeError, match="Item change rejected"):
        target.push_one(
            "http://127.0.0.1:1234", "fake", {
                "id": "synthetic-record-a-b", "payload": "e30=",
            }
        )


def test_push_has_no_real_network_in_test(monkeypatch):
    seen = []
    def fake(*args, **kwargs):
        seen.append((args, kwargs))
        return {"acceptedRecordIDs": ["synthetic-r"], "rejectedRecordIDs": []}
    monkeypatch.setattr(target, "exchange", fake)
    result = target.push_one("http://127.0.0.1:1234", "fake", {"id": "synthetic-r"})
    assert result["acceptedRecordIDs"] == ["synthetic-r"]
    assert seen[0][1]["headers"]["Authorization"] == "Bearer fake"
    assert seen[0][1]["payload"]["records"] == [{"id": "synthetic-r"}]


def test_requires_private_handoff_before_creating_record(tmp_path, monkeypatch):
    f = tmp_path / "bad-handoff.json"
    f.write_text("{}")
    f.chmod(0o600)
    monkeypatch.setattr(target, "exchange", lambda *a, **kw: pytest.fail("unexpected HTTP"))
    with pytest.raises(ValueError):
        target.verify("http://127.0.0.1:34343", f, "synthetic-test-key-xxxx")