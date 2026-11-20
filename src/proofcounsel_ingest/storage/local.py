from __future__ import annotations

import os
import tempfile
from pathlib import Path


class LocalStorage:
    """Filesystem storage with traversal protection and atomic writes."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        normalized_key = _normalise_key(key)
        candidate = (self.root / normalized_key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("storage key escapes configured root")
        return candidate

    def put_bytes(self, key: str, payload: bytes) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list_keys(self, prefix: str = "") -> list[str]:
        prefix_path = self.root if not prefix else self._path(prefix)
        if not prefix_path.exists():
            return []
        paths = [prefix_path] if prefix_path.is_file() else list(prefix_path.rglob("*"))
        return sorted(str(path.relative_to(self.root)) for path in paths if path.is_file())


def _normalise_key(value: str) -> str:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("storage key must be a relative non-traversing path")
    return normalized
