"""S3-compatible object storage adapter with bounded key handling."""

from __future__ import annotations

from typing import Any


class S3Storage:
    """Implement ``StoragePort`` over an S3-compatible client.

    The client is injectable for tests and local S3-compatible deployments.
    Server-side encryption is requested for every upload by default; callers
    can provide a KMS key when their deployment requires customer-managed
    encryption.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        client: Any | None = None,
        endpoint_url: str | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("bucket cannot be empty")
        self.bucket = bucket
        self.prefix = _normalise_key(prefix) if prefix else ""
        self.kms_key_id = kms_key_id
        if client is None:
            try:
                import boto3  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - optional production dependency
                raise RuntimeError("S3 storage requires the optional 'postgres' dependency group") from exc
            client = boto3.client("s3", endpoint_url=endpoint_url)
        self._client = client

    def put_bytes(self, key: str, payload: bytes) -> None:
        parameters: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._key(key),
            "Body": payload,
            "ContentLength": len(payload),
        }
        if self.kms_key_id:
            parameters.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=self.kms_key_id)
        else:
            parameters["ServerSideEncryption"] = "AES256"
        self._client.put_object(**parameters)

    def get_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        body = response["Body"]
        return bytes(body.read())

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if _is_not_found(exc):
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def list_keys(self, prefix: str = "") -> list[str]:
        full_prefix = self._key(prefix) if prefix else self.prefix
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for item in page.get("Contents", []):
                key = str(item["Key"])
                keys.append(key.removeprefix(f"{self.prefix}/") if self.prefix else key)
        return sorted(keys)

    def _key(self, key: str) -> str:
        normalized = _normalise_key(key)
        return f"{self.prefix}/{normalized}" if self.prefix else normalized


def _normalise_key(value: str) -> str:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("storage key must be a relative non-traversing path")
    return normalized


def _is_not_found(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        error_code = str(response.get("Error", {}).get("Code", ""))
        return error_code in {"404", "NoSuchKey", "NotFound"}
    return False
