from __future__ import annotations

import base64
import hashlib
import json

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.schemas import SyncRecord


class SyncMutationFingerprint(Base):
    """Immutable request identity paired with a clientMutationID.

    SyncMutation intentionally stores the result/audit envelope. This companion table lets the
    server distinguish a legitimate replay from accidental or malicious reuse of the same
    clientMutationID for different bytes without changing the V1 wire contract.
    """

    __tablename__ = "sync_mutation_fingerprints"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    client_mutation_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


def request_fingerprint(record: SyncRecord) -> str:
    payload = record.payload
    if isinstance(payload, str):
        payload_bytes = payload.encode()
    else:
        payload_bytes = bytes(payload)
    material = {
        "entityType": record.entityType,
        "entityID": record.entityID,
        "baseServerRevision": record.baseServerRevision,
        "updatedAt": record.updatedAt.isoformat(),
        "deletedAt": record.deletedAt.isoformat() if record.deletedAt is not None else None,
        "payload": base64.b64encode(payload_bytes).decode("ascii"),
    }
    canonical = json.dumps(material, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()
