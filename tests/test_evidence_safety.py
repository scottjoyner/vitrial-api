import hashlib

import pytest
from fastapi import HTTPException

from app.auth import Principal
from app.evidence import put_blob
from app.models import EvidenceBlob


class ExistingBlobDB:
    def __init__(self, blob: EvidenceBlob):
        self.blob = blob
        self.committed = False

    async def get(self, _model, _key):
        return self.blob

    def add(self, _model):
        raise AssertionError("ownership conflict must not add a replacement model")

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_ownership_conflict_does_not_replace_existing_blob(tmp_path, monkeypatch):
    target = tmp_path / "canonical-blob"
    target.write_bytes(b"canonical")

    existing = EvidenceBlob(
        organization_id="org-1",
        document_id="doc-1",
        item_id="item-original",
        filename="original.jpg",
        mime_type="image/jpeg",
        sha256=hashlib.sha256(b"canonical").hexdigest(),
        size_bytes=len(b"canonical"),
        object_key=str(target),
    )
    db = ExistingBlobDB(existing)
    principal = Principal(
        user_id="user-1",
        organization_id="org-1",
        membership_id="membership-1",
        session_id="session-1",
        authorization_revision=1,
        capabilities=frozenset({"sync", "item.evidence.manage"}),
        customer_ids=frozenset(),
        project_ids=frozenset(),
        all_customers=True,
        all_projects=True,
    )

    monkeypatch.setattr("app.evidence.object_path", lambda _principal, _document_id: target)
    replacement = b"replacement"

    with pytest.raises(HTTPException) as exc:
        await put_blob(
            db,
            principal,
            "doc-1",
            "item-different",
            "replacement.jpg",
            "image/jpeg",
            hashlib.sha256(replacement).hexdigest(),
            replacement,
        )

    assert exc.value.status_code == 409
    assert target.read_bytes() == b"canonical"
    assert not db.committed
