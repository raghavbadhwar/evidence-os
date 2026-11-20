"""Tamper-evident audit metadata without storing document text in the log."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import portalocker
from pydantic import BaseModel, Field


class AuditRecord(BaseModel):
    audit_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    actor_id: UUID | None = None
    actor_role: str = "system"
    action_type: str = Field(min_length=1, max_length=120)
    aggregate_type: str = Field(min_length=1, max_length=120)
    aggregate_id: UUID
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HashChainedAuditLog:
    def __init__(self, path: Path, tenant_id: UUID) -> None:
        self.path = path.expanduser().resolve()
        self.tenant_id = tenant_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        action_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        payload: Mapping[str, Any],
        actor_id: UUID | None = None,
        actor_role: str = "system",
    ) -> AuditRecord:
        payload_hash = _sha256_json(payload)
        with portalocker.Lock(str(self.path), mode="a+", timeout=30) as handle:
            previous_hash = _last_event_hash(handle)
            record = AuditRecord(
                tenant_id=self.tenant_id,
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=action_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload_sha256=payload_hash,
                previous_hash=previous_hash,
                event_hash="0" * 64,
            )
            unsigned = record.model_dump(mode="json")
            unsigned.pop("event_hash")
            record = record.model_copy(update={"event_hash": _sha256_json(unsigned)})
            handle.seek(0, 2)
            handle.write(json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def verify(self) -> None:
        if not self.path.exists():
            return
        previous_hash: str | None = None
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                raw = json.loads(line)
                record = AuditRecord.model_validate(raw)
                if record.tenant_id != self.tenant_id or record.previous_hash != previous_hash:
                    raise ValueError(f"audit chain linkage failed at line {line_number}")
                unsigned = record.model_dump(mode="json")
                unsigned.pop("event_hash")
                if _sha256_json(unsigned) != record.event_hash:
                    raise ValueError(f"audit event hash failed at line {line_number}")
                previous_hash = record.event_hash


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _last_event_hash(handle: Any) -> str | None:
    """Read only the tail of the audit log instead of rescanning its history."""
    handle.seek(0, os.SEEK_END)
    file_size = handle.tell()
    if file_size == 0:
        return None
    window = 8192
    while True:
        handle.seek(max(0, file_size - window))
        lines = handle.read().splitlines()
        if lines and (len(lines) > 1 or file_size <= window):
            event_hash = json.loads(lines[-1]).get("event_hash")
            return str(event_hash) if isinstance(event_hash, str) else None
        window = min(file_size, window * 2)
