import hashlib

import pytest

from app.storage import LocalObjectStore, StorageDigestMismatch, object_key_for


async def chunks(*values: bytes):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_local_store_streams_and_verifies_content(tmp_path):
    store = LocalObjectStore(tmp_path)
    body = b"alpha" + b"beta" + b"gamma"
    digest = hashlib.sha256(body).hexdigest()
    key = object_key_for("org-1", "doc-1", digest)

    stored = await store.put_verified(key, chunks(b"alpha", b"beta", b"gamma"), digest)

    assert stored.sha256 == digest
    assert stored.size_bytes == len(body)
    assert await store.exists(key)
    downloaded = b"".join([part async for part in store.stream(key)])
    assert downloaded == body


@pytest.mark.asyncio
async def test_digest_mismatch_never_publishes_candidate(tmp_path):
    store = LocalObjectStore(tmp_path)
    body = b"canonical"
    digest = hashlib.sha256(body).hexdigest()
    key = object_key_for("org-1", "doc-1", digest)
    await store.put_verified(key, chunks(body), digest)

    wrong_digest = hashlib.sha256(b"different").hexdigest()
    wrong_key = object_key_for("org-1", "doc-1", wrong_digest)
    with pytest.raises(StorageDigestMismatch):
        await store.put_verified(wrong_key, chunks(b"not-different"), wrong_digest)

    assert await store.exists(key)
    assert not await store.exists(wrong_key)
    assert b"".join([part async for part in store.stream(key)]) == body


def test_object_keys_are_content_addressed_and_identifier_safe():
    digest = "a" * 64
    key = object_key_for("../../org", "../document/with/slashes", digest)
    assert ".." not in key
    assert key.endswith("/" + digest)
    assert "../../org" not in key
    assert "../document" not in key
