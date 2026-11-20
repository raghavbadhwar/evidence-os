"""Atomic, immutable artifact publication and run audit records."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import portalocker


class ArtifactError(RuntimeError):
    pass


class ArtifactWriter:
    """Publish one content-addressed artifact directory as an atomic unit."""

    def __init__(self, root: Path, key: str, lock_timeout_seconds: float = 30.0) -> None:
        self.root = root.expanduser().resolve()
        self.key = _validate_key(key)
        self.final_path = (self.root / "documents" / self.key).resolve()
        if self.root not in self.final_path.parents:
            raise ValueError("artifact key escapes configured root")
        lock_name = hashlib.sha256(self.key.encode("utf-8")).hexdigest() + ".lock"
        self.lock_path = self.root / "_locks" / lock_name
        self.lock_timeout_seconds = lock_timeout_seconds
        self.staging_path: Path | None = None
        self.reused = False
        self._committed = False
        self._lock: portalocker.Lock | None = None

    def __enter__(self) -> Self:
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = portalocker.Lock(str(self.lock_path), mode="a+", timeout=self.lock_timeout_seconds)
        self._lock.acquire()
        if self.final_path.exists():
            self._validate_existing()
            self.reused = True
            return self
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        self.staging_path = Path(
            tempfile.mkdtemp(prefix=".staging-", dir=self.final_path.parent)
        ).resolve()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.staging_path is not None and self.staging_path.exists() and not self._committed:
            shutil.rmtree(self.staging_path, ignore_errors=True)
        if self._lock is not None:
            self._lock.release()

    def write_bytes(self, relative_path: str, payload: bytes) -> Path:
        destination = self._staging_destination(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return destination

    def write_text(self, relative_path: str, text: str) -> Path:
        return self.write_bytes(relative_path, text.encode("utf-8"))

    def write_json(self, relative_path: str, payload: Any) -> Path:
        return self.write_text(relative_path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def write_jsonl(self, relative_path: str, rows: Iterable[Mapping[str, Any]]) -> Path:
        content = "".join(json.dumps(dict(row), sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
        return self.write_text(relative_path, content)

    def read_existing_json(self, relative_path: str) -> Any:
        if not self.reused:
            raise ArtifactError("artifact has not been reused")
        return json.loads((self.final_path / _validate_relative(relative_path)).read_text(encoding="utf-8"))

    def read_existing_jsonl(self, relative_path: str) -> list[Any]:
        if not self.reused:
            raise ArtifactError("artifact has not been reused")
        path = self.final_path / _validate_relative(relative_path)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def commit(self, metadata: Mapping[str, Any]) -> Path:
        if self.reused:
            self._committed = True
            return self.final_path
        if self.staging_path is None:
            raise ArtifactError("artifact writer is not active")
        checksums = _checksums(self.staging_path)
        manifest = {
            "artifact_key": self.key,
            "files": checksums,
            "metadata": dict(metadata),
        }
        self.write_json("artifact_manifest.json", manifest)
        _fsync_directory(self.staging_path)
        if self.final_path.exists():
            raise ArtifactError("artifact appeared while an exclusive key lock was held")
        os.replace(self.staging_path, self.final_path)
        _fsync_directory(self.final_path.parent)
        self._committed = True
        self.staging_path = None
        return self.final_path

    def _staging_destination(self, relative_path: str) -> Path:
        if self.reused:
            raise ArtifactError("cannot write into a reused immutable artifact")
        if self.staging_path is None:
            raise ArtifactError("artifact writer is not active")
        destination = (self.staging_path / _validate_relative(relative_path)).resolve()
        if self.staging_path not in destination.parents:
            raise ValueError("artifact path escapes staging directory")
        return destination

    def _validate_existing(self) -> None:
        manifest_path = self.final_path / "artifact_manifest.json"
        if not manifest_path.is_file():
            raise ArtifactError(f"existing artifact is missing its manifest: {self.final_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("artifact_key") != self.key:
            raise ArtifactError("existing artifact key does not match requested key")
        expected = manifest.get("files", {})
        for relative_path, expected_hash in expected.items():
            path = self.final_path / _validate_relative(relative_path)
            if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected_hash:
                raise ArtifactError(f"artifact checksum mismatch: {relative_path}")


def write_run_record(root: Path, tenant_id: UUID, run_id: UUID, payload: Mapping[str, Any]) -> Path:
    directory = root.expanduser().resolve() / "_runs" / str(tenant_id)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{run_id}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{run_id}.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        _fsync_directory(directory)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return destination


def cleanup_stale_staging(root: Path) -> int:
    count = 0
    documents = root.expanduser().resolve() / "documents"
    if not documents.exists():
        return count
    for path in documents.rglob(".staging-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            count += 1
    return count


def _validate_key(key: str) -> str:
    path = Path(key)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("artifact key must be a non-empty relative path")
    return path.as_posix()


def _validate_relative(path: str) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("artifact member path must be a non-empty relative path")
    return relative


def _checksums(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "artifact_manifest.json":
            result[path.relative_to(root).as_posix()] = _sha256_bytes(path.read_bytes())
    return result


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
