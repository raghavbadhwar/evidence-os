from __future__ import annotations

from dataclasses import dataclass

import pytest

from proofcounsel_ingest.storage.local import LocalStorage
from proofcounsel_ingest.storage.object_store import S3Storage


@dataclass
class NotFoundError(Exception):
    response: dict


class FakeBody:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


class FakePaginator:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def paginate(self, *, Bucket: str, Prefix: str):  # type: ignore[no-untyped-def]
        del Bucket
        yield {"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs):  # type: ignore[no-untyped-def]
        self.objects[kwargs["Key"]] = kwargs["Body"]
        assert kwargs["ServerSideEncryption"] == "AES256"

    def get_object(self, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["Key"] not in self.objects:
            raise NotFoundError({"Error": {"Code": "NoSuchKey"}})
        return {"Body": FakeBody(self.objects[kwargs["Key"]])}

    def head_object(self, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["Key"] not in self.objects:
            raise NotFoundError({"Error": {"Code": "404"}})
        return {}

    def delete_object(self, **kwargs):  # type: ignore[no-untyped-def]
        self.objects.pop(kwargs["Key"], None)

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_objects_v2"
        return FakePaginator(self.objects)


class FailingS3(FakeS3):
    def put_object(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        raise OSError("object store unavailable")


def test_s3_storage_encrypts_and_scopes_keys() -> None:
    client = FakeS3()
    storage = S3Storage("proofcounsel", prefix="tenant-a", client=client)
    storage.put_bytes("documents/doc.txt", b"evidence")

    assert storage.exists("documents/doc.txt")
    assert storage.get_bytes("documents/doc.txt") == b"evidence"
    assert storage.list_keys("documents") == ["documents/doc.txt"]
    storage.delete("documents/doc.txt")
    assert not storage.exists("documents/doc.txt")


def test_s3_storage_propagates_outage_without_claiming_success() -> None:
    storage = S3Storage("proofcounsel", client=FailingS3())
    with pytest.raises(OSError, match="unavailable"):
        storage.put_bytes("documents/doc.txt", b"evidence")


@pytest.mark.parametrize("key", ["../escape", "/absolute", "", "a/../b", "a//b"])
def test_storage_rejects_traversal_keys(key: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        S3Storage("proofcounsel", client=FakeS3()).put_bytes(key, b"bad")
    with pytest.raises(ValueError):
        LocalStorage(tmp_path).put_bytes(key, b"bad")
