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


class UntouchedStore:
    provider = "local"

    async def put_verified(self, *_args, **_kwargs):
        raise AssertionError("ownership conflict must be rejected before object storage")


@pytest.mark.asyncio
async def test_ownership_conflict_does_not_touch_object_storage(monkeypatch):
    existing = EvidenceBlob(
        organization_id="org-1",
        document_id="doc-1",
        item_id="item-original",
        filename="original.jpg",
        mime_type="image/jpeg",
        sha256=hashlib.sha256(b"canonical").hexdigest(),
        size_bytes=len(b"canonical"),
        storage_provider="local",
        object_key="blobs/canonical",
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
    monkeypatch.setattr("app.evidence.current_store", lambda: UntouchedStore())
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
    assert not db.committed


# ── VITR-V007: a pending GC row must not outlive the object it points at ──────
#
# Object keys are content-addressed, so re-uploading byte-identical content produces the
# SAME key a superseded version used. That key can still carry a pending GC row from when
# it was superseded. `put_blob` used to stream the object, THEN take the organization
# lock -- so a collector holding that same lock could delete the freshly written object
# before the upload committed its pointer, and the client got a 201 for an object that no
# longer existed.
#
# These tests pin the two halves of the fix, which are only safe together because both
# sides take the same row lock.


class _CancelDB:
    """Records whether a GC row was cancelled, without a database.

    `scalar` is called twice by `put_blob`: once for the Organization (the row lock) and
    once for the EvidenceBlob. The first returns the org so the lock is "acquired", the
    second returns None so the upload takes the create path.
    """

    def __init__(self):
        self.cancelled = False
        self.committed = False
        self._scalars = 0

    async def execute(self, _statement):
        self.cancelled = True

        class _Result:
            rowcount = 1

        return _Result()

    async def get(self, _model, _key):
        return None

    async def scalar(self, *_args, **_kwargs):
        self._scalars += 1
        return _Org() if self._scalars == 1 else None

    def add(self, _model):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


class _Org:
    id = "org-1"


class _StoreGone:
    """The object was collected mid-flight: `exists` reports it absent."""

    provider = "local"

    def __init__(self):
        self.deleted = False

    async def put_verified(self, *_args, **_kwargs):
        return type("Stored", (), {"sha256": "x", "size_bytes": 1})()

    async def exists(self, _key):
        return False

    async def delete(self, _key):
        self.deleted = True


def _principal() -> Principal:
    return Principal(
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


@pytest.mark.asyncio
async def test_pending_gc_row_is_cancelled_before_the_pointer_is_published(monkeypatch):
    """A pending GC row for the key being published must be removed under the lock."""
    db = _CancelDB()
    monkeypatch.setattr("app.evidence.current_store", lambda: _StoreGone())
    monkeypatch.setattr("app.evidence._evidence_metadata", _none_metadata)
    monkeypatch.setattr("app.evidence.require_item_access", _allow_access)

    with pytest.raises(HTTPException):
        await put_blob(
            db, _principal(), "doc-1", "item-1", "f.bin", "application/octet-stream",
            hashlib.sha256(b"payload").hexdigest(), b"payload",
        )

    assert db.cancelled, (
        "put_blob must cancel any pending GC row for the key it is publishing; "
        "otherwise the collector can delete the object it just wrote"
    )


@pytest.mark.asyncio
async def test_upload_refuses_to_publish_a_pointer_to_a_collected_object(monkeypatch):
    """The residual race: the collector deleted it before the upload took the lock.

    Cancelling the row cannot undo a delete that already committed, so the upload must
    verify the object under the lock. Committing the pointer anyway is the silent data
    loss: a 201 for an object every later GET would 404 on.
    """
    db = _CancelDB()
    store = _StoreGone()
    monkeypatch.setattr("app.evidence.current_store", lambda: store)
    monkeypatch.setattr("app.evidence._evidence_metadata", _none_metadata)
    monkeypatch.setattr("app.evidence.require_item_access", _allow_access)

    with pytest.raises(HTTPException) as exc:
        await put_blob(
            db, _principal(), "doc-1", "item-1", "f.bin", "application/octet-stream",
            hashlib.sha256(b"payload").hexdigest(), b"payload",
        )

    assert exc.value.status_code == 503, (
        "an upload whose object was collected mid-flight must fail, not return 201"
    )
    assert not db.committed, "a pointer must never be committed to a missing object"


async def _none_metadata(*_args, **_kwargs):
    return None


async def _allow_access(*_args, **_kwargs):
    return None
