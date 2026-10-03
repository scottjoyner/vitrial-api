import hashlib

import pytest

from app.storage import (
    LocalObjectStore,
    S3ObjectStore,
    StorageDigestMismatch,
    object_key_for,
)


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


class FakeS3Client:
    """A boto3-shaped client: records calls, holds objects, can fail one operation.

    `S3ObjectStore` only ever reaches boto3 through `asyncio.to_thread(self.client.<op>)`,
    so a duck-typed client exercises the real `put_verified` -- multipart assembly,
    digest verification, the publish and the cleanup order -- with no network and no
    `S3_INTEGRATION` gate. The S3 store is what production evidence uses
    (`deploy/compose.production.yml:20`), and before this file the only coverage of it
    lived behind that gate.
    """

    def __init__(self, *, fail: str | None = None):
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.objects: dict[str, bytes] = {}

    def _record(self, op: str, key: str) -> None:
        self.calls.append((op, key))
        if self.fail == op:
            raise RuntimeError(f"injected {op} failure")

    def create_multipart_upload(self, **kwargs):
        self._record("create", kwargs["Key"])
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs):
        self._record("part", kwargs["Key"])
        return {"ETag": "etag-1"}

    def complete_multipart_upload(self, **kwargs):
        self._record("complete", kwargs["Key"])
        self.objects[kwargs["Key"]] = b"assembled"
        return {}

    def copy_object(self, **kwargs):
        # Read the source first, then record, then write: `_record` is where an injected
        # failure fires, so a copy that fails must leave nothing behind -- which is what
        # `test_a_failure_before_the_publish_is_still_reported` asserts.
        payload = self.objects[kwargs["CopySource"]["Key"]]
        self._record("copy", kwargs["Key"])
        self.objects[kwargs["Key"]] = payload
        return {}

    def delete_object(self, **kwargs):
        self._record("delete", kwargs["Key"])
        self.objects.pop(kwargs["Key"], None)
        return {}

    def abort_multipart_upload(self, **kwargs):
        self._record("abort", kwargs["Key"])
        return {}


def s3_store(monkeypatch, *, fail: str | None = None) -> tuple[S3ObjectStore, FakeS3Client]:
    import app.storage as storage_module

    client = FakeS3Client(fail=fail)
    monkeypatch.setattr(storage_module.settings, "s3_bucket", "test-bucket")

    # The service name arrives POSITIONALLY: `boto3.client("s3", endpoint_url=…)`
    # (app/storage.py:139). A `lambda **kwargs` cannot accept that and raises
    # `TypeError: <lambda>() takes 0 positional arguments but 1 was given`, which
    # is exactly what CI reported -- the stub was narrower than the call it stood
    # in for, so the three tests written to pin VITR-V013 never constructed a
    # store at all. The stub's job is to be the real client, so it has to accept
    # the real call shape.
    def _client(service_name: str, **kwargs):
        assert service_name == "s3", f"unexpected service {service_name!r}"
        return client

    monkeypatch.setattr(storage_module.boto3, "client", _client)
    return storage_module.S3ObjectStore(), client


@pytest.mark.asyncio
async def test_a_failed_staging_delete_does_not_fail_a_published_upload(monkeypatch):
    """`put_verified` publishes first and cleans up second; the cleanup is best-effort.

    The upload is durable and digest-verified at its final key the moment `copy_object`
    returns. When the staging delete then failed, it fell into the broad handler, which
    retried the same delete inside a bare `except: pass` and raised `StorageError` --
    which `put_blob` turns into a 503 *before* it adds the EvidenceBlob row and *before*
    it queues a GC row. The client was told the upload failed while the object sat at
    its content-addressed key with nothing referencing it and nothing that would ever
    reclaim it. VITR-V013.
    """
    store, client = s3_store(monkeypatch, fail="delete")
    body = b"evidence-bytes"
    digest = hashlib.sha256(body).hexdigest()
    key = object_key_for("org-1", "doc-1", digest)

    stored = await store.put_verified(key, chunks(body), digest)

    assert stored.sha256 == digest, "the upload succeeded; the caller must be told so"
    assert stored.size_bytes == len(body)
    assert client.objects.get(key) is not None, (
        "the object is published at its final key before the staging copy is removed"
    )
    ops = [op for op, _ in client.calls]
    assert ops.index("copy") < ops.index("delete"), "publish, then clean up"
    assert ops.count("delete") == 1, (
        "a failed staging delete is not retried; the swallowed retry was the tell"
    )


@pytest.mark.asyncio
async def test_a_failure_before_the_publish_is_still_reported(monkeypatch):
    """The other half: the swallow is narrow, and only covers post-publish cleanup.

    Without this, "swallow the cleanup error" reads as "swallow storage errors", and the
    digest and multipart paths would stop failing loudly.
    """
    from app.storage import StorageError

    store, client = s3_store(monkeypatch, fail="copy")
    body = b"evidence-bytes"
    digest = hashlib.sha256(body).hexdigest()
    key = object_key_for("org-1", "doc-1", digest)

    with pytest.raises(StorageError):
        await store.put_verified(key, chunks(body), digest)

    assert key not in client.objects, "a copy that failed must not leave a published object"


@pytest.mark.asyncio
async def test_a_digest_mismatch_is_never_published(monkeypatch):
    """The S3 store verifies before it publishes, like the local one."""
    from app.storage import StorageDigestMismatch

    store, client = s3_store(monkeypatch)
    body = b"canonical"
    digest = hashlib.sha256(body).hexdigest()
    wrong_key = object_key_for("org-1", "doc-1", hashlib.sha256(b"different").hexdigest())

    with pytest.raises(StorageDigestMismatch):
        await store.put_verified(wrong_key, chunks(b"not-different"), digest)

    assert "copy" not in [op for op, _ in client.calls], "nothing is published on a mismatch"
