from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import (
    MAX_SYNC_RECORDS,
    MAX_SYNC_V1_BATCH_PAYLOAD_BYTES,
    SyncBatch,
    SyncRecord,
)
from app.sync_v2 import (
    MAX_SYNC_V2_BATCH_PAYLOAD_BYTES,
    MAX_SYNC_V2_PAYLOAD_BYTES,
    SyncBatchV2,
    SyncRecordV2,
)
from app import sync_service
from app.sync_service import MAX_SYNC_PULL_RESPONSE_BYTES, pull_page_accepts

NOW = datetime.now(timezone.utc)


def v1_record(index: int) -> SyncRecord:
    return SyncRecord(
        id=f"record-{index}",
        entityType="customer",
        entityID=f"customer-{index}",
        updatedAt=NOW,
        payload=b"e30=",
        clientMutationID=f"mutation-{index}",
    )


def v2_record(index: int) -> SyncRecordV2:
    return SyncRecordV2(
        id=f"record-{index}",
        entityType="customer",
        entityID=f"customer-{index}",
        updatedAt=NOW,
        payload={"name": f"Customer {index}"},
        clientMutationID=f"mutation-{index}",
    )


def test_v1_sync_batch_accepts_client_contract_ceiling():
    batch = SyncBatch(deviceID="device-1", records=[v1_record(i) for i in range(MAX_SYNC_RECORDS)])
    assert len(batch.records) == MAX_SYNC_RECORDS


def test_v1_sync_batch_rejects_more_than_client_contract_ceiling():
    with pytest.raises(ValidationError):
        SyncBatch(deviceID="device-1", records=[v1_record(i) for i in range(MAX_SYNC_RECORDS + 1)])


def test_v2_sync_batch_rejects_more_than_client_contract_ceiling():
    with pytest.raises(ValidationError):
        SyncBatchV2(deviceID="device-1", records=[v2_record(i) for i in range(MAX_SYNC_RECORDS + 1)])


def test_v2_record_rejects_oversized_native_json_payload():
    with pytest.raises(ValidationError):
        SyncRecordV2(
            id="record-large",
            entityType="customer",
            entityID="customer-large",
            updatedAt=NOW,
            payload={"blob": "x" * (MAX_SYNC_V2_PAYLOAD_BYTES + 1)},
            clientMutationID="mutation-large",
        )


def test_v1_sync_batch_rejects_aggregate_payload_over_budget():
    per_record = (MAX_SYNC_V1_BATCH_PAYLOAD_BYTES // 2) + 1
    records = [
        SyncRecord(
            id=f"aggregate-v1-{index}",
            entityType="customer",
            entityID=f"customer-aggregate-v1-{index}",
            updatedAt=NOW,
            payload=b"x" * per_record,
            clientMutationID=f"mutation-aggregate-v1-{index}",
        )
        for index in range(2)
    ]

    with pytest.raises(ValidationError):
        SyncBatch(deviceID="device-1", records=records)


def test_v2_sync_batch_rejects_aggregate_payload_over_budget():
    per_record = (MAX_SYNC_V2_BATCH_PAYLOAD_BYTES // 2) + 1
    records = [
        SyncRecordV2(
            id=f"aggregate-v2-{index}",
            entityType="customer",
            entityID=f"customer-aggregate-v2-{index}",
            updatedAt=NOW,
            payload={"blob": "x" * per_record},
            clientMutationID=f"mutation-aggregate-v2-{index}",
        )
        for index in range(2)
    ]

    with pytest.raises(ValidationError):
        SyncBatchV2(deviceID="device-1", records=records)



def test_pull_page_rejects_record_after_public_record_count_ceiling():
    records = [v1_record(index) for index in range(MAX_SYNC_RECORDS)]
    assert pull_page_accepts(records, v1_record(MAX_SYNC_RECORDS), 999) is False


def test_pull_page_rejects_next_record_before_aggregate_model_budget_is_exceeded():
    payload_size = (MAX_SYNC_V1_BATCH_PAYLOAD_BYTES // 2) + 1
    first = SyncRecord(
        id="pull-large-1",
        entityType="customer",
        entityID="customer-pull-large-1",
        updatedAt=NOW,
        payload=b"x" * payload_size,
        clientMutationID="mutation-pull-large-1",
    )
    second = SyncRecord(
        id="pull-large-2",
        entityType="customer",
        entityID="customer-pull-large-2",
        updatedAt=NOW,
        payload=b"x" * payload_size,
        clientMutationID="mutation-pull-large-2",
    )

    assert pull_page_accepts([], first, 1) is True
    assert pull_page_accepts([first], second, 2) is False


def test_pull_page_rejects_next_record_when_serialized_response_budget_is_exceeded(monkeypatch):
    first = v1_record(1)
    second = v1_record(2)
    one_record_size = len(
        SyncBatch(deviceID="server", cursor="seq:1", records=[first]).model_dump_json().encode("utf-8")
    )
    monkeypatch.setattr(sync_service, "MAX_SYNC_PULL_RESPONSE_BYTES", one_record_size + 1)

    assert pull_page_accepts([], first, 1) is True
    assert pull_page_accepts([first], second, 2) is False
