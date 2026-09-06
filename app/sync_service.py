from __future__ import annotations
import base64
import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.models import SyncChangeLog, SyncEntity, SyncMutation
from app.schemas import SyncBatch, SyncRecord, SyncResult

@dataclass(frozen=True)
class MutationOutcome:
    accepted: bool
    record_id: str

class StaleRevision(Exception):
    pass

class InvalidMutation(Exception):
    pass

def decode_payload(record: SyncRecord) -> dict:
    try:
        raw = record.payload
        try:
            decoded = base64.b64decode(raw, validate=True)
            return json.loads(decoded)
        except Exception:
            return json.loads(raw)
    except Exception as exc:
        raise InvalidMutation(f"invalid payload: {exc}") from exc

async def _next_revision(db: AsyncSession, organization_id: str) -> int:
    value = await db.scalar(
        select(func.coalesce(func.max(SyncEntity.server_revision), 0)).where(
            SyncEntity.organization_id == organization_id
        )
    )
    return int(value or 0) + 1

async def apply_push(db: AsyncSession, principal: Principal, batch: SyncBatch) -> SyncResult:
    if "sync" not in principal.capabilities:
        raise PermissionError("sync capability required")

    accepted: list[str] = []
    rejected: list[str] = []

    for record in batch.records:
        if not record.clientMutationID:
            rejected.append(record.id)
            continue

        prior = await db.scalar(
            select(SyncMutation).where(
                SyncMutation.organization_id == principal.organization_id,
                SyncMutation.client_mutation_id == record.clientMutationID,
            )
        )
        if prior is not None:
            (accepted if prior.result_status == "accepted" else rejected).append(record.id)
            continue

        current = await db.get(
            SyncEntity,
            (principal.organization_id, record.entityType, record.entityID),
        )
        current_revision = current.server_revision if current else None

        if current is not None and record.baseServerRevision != current_revision:
            db.add(SyncMutation(
                organization_id=principal.organization_id,
                client_mutation_id=record.clientMutationID,
                submitted_by_user_id=principal.user_id,
                membership_id=principal.membership_id,
                session_id=principal.session_id,
                authorization_revision=principal.authorization_revision,
                device_id=batch.deviceID,
                entity_type=record.entityType,
                entity_id=record.entityID,
                base_server_revision=record.baseServerRevision,
                result_server_revision=current_revision,
                result_status="rejected",
            ))
            rejected.append(record.id)
            continue

        payload = decode_payload(record)
        revision = await _next_revision(db, principal.organization_id)

        if current is None:
            current = SyncEntity(
                organization_id=principal.organization_id,
                entity_type=record.entityType,
                entity_id=record.entityID,
                server_revision=revision,
                payload_json=payload if record.deletedAt is None else None,
                updated_at=record.updatedAt,
                deleted_at=record.deletedAt,
            )
            db.add(current)
        else:
            current.server_revision = revision
            current.payload_json = payload if record.deletedAt is None else None
            current.updated_at = record.updatedAt
            current.deleted_at = record.deletedAt

        db.add(SyncMutation(
            organization_id=principal.organization_id,
            client_mutation_id=record.clientMutationID,
            submitted_by_user_id=principal.user_id,
            membership_id=principal.membership_id,
            session_id=principal.session_id,
            authorization_revision=principal.authorization_revision,
            device_id=batch.deviceID,
            entity_type=record.entityType,
            entity_id=record.entityID,
            base_server_revision=record.baseServerRevision,
            result_server_revision=revision,
            result_status="accepted",
        ))
        db.add(SyncChangeLog(
            organization_id=principal.organization_id,
            entity_type=record.entityType,
            entity_id=record.entityID,
            server_revision=revision,
            client_mutation_id=record.clientMutationID,
            operation="delete" if record.deletedAt else "upsert",
        ))
        accepted.append(record.id)

    await db.commit()

    seq = await db.scalar(
        select(func.coalesce(func.max(SyncChangeLog.sequence), 0)).where(
            SyncChangeLog.organization_id == principal.organization_id
        )
    )
    return SyncResult(
        acceptedRecordIDs=accepted,
        rejectedRecordIDs=rejected,
        nextCursor=f"seq:{int(seq or 0)}",
    )

async def pull_since(db: AsyncSession, principal: Principal, cursor: str | None) -> SyncBatch:
    if "sync" not in principal.capabilities:
        raise PermissionError("sync capability required")

    start = 0
    if cursor:
        if not cursor.startswith("seq:") or not cursor[4:].isdigit():
            raise InvalidMutation("invalid cursor")
        start = int(cursor[4:])

    changes = (
        await db.scalars(
            select(SyncChangeLog)
            .where(
                SyncChangeLog.organization_id == principal.organization_id,
                SyncChangeLog.sequence > start,
            )
            .order_by(SyncChangeLog.sequence.asc())
            .limit(500)
        )
    ).all()

    records: list[SyncRecord] = []
    max_seq = start
    for change in changes:
        max_seq = max(max_seq, change.sequence)
        entity = await db.get(
            SyncEntity,
            (principal.organization_id, change.entity_type, change.entity_id),
        )
        if not entity:
            continue
        payload = json.dumps(entity.payload_json or {}, separators=(",", ":"), sort_keys=True).encode()
        encoded_payload = base64.b64encode(payload)
        records.append(SyncRecord(
            id=f"server:{change.sequence}",
            entityType=change.entity_type,
            entityID=change.entity_id,
            updatedAt=entity.updated_at,
            payload=encoded_payload,
            serverRevision=entity.server_revision,
            clientMutationID=change.client_mutation_id,
            deletedAt=entity.deleted_at,
        ))

    return SyncBatch(deviceID="server", cursor=f"seq:{max_seq}", records=records)
