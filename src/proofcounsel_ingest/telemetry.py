"""Redacted, stage-level telemetry for ingestion and retrieval."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TelemetryEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tenant_id: UUID
    document_id: UUID | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stage: str = Field(min_length=1, max_length=80)
    status: str = Field(min_length=1, max_length=40)
    elapsed_ms: float = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    provider_cost: float = Field(default=0, ge=0)
    metadata: dict[str, int | float | str | bool | None] = Field(default_factory=dict)


class TelemetrySink(Protocol):
    def emit(self, event: TelemetryEvent) -> None: ...


class NullTelemetrySink:
    def emit(self, event: TelemetryEvent) -> None:
        del event


class JsonlTelemetrySink:
    """Append-only local sink with bounded, text-free event payloads."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: TelemetryEvent) -> None:
        payload = event.model_dump(mode="json")
        payload["metadata"] = redact_metadata(event.metadata)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())


class OpenTelemetrySink:
    """Optional span sink; importing the SDK is deferred to production setup."""

    def __init__(self, tracer: Any | None = None, *, service_name: str = "proofcounsel") -> None:
        if tracer is None:
            try:
                from opentelemetry import trace
            except ImportError as exc:  # pragma: no cover - optional production dependency
                raise RuntimeError("OpenTelemetry support requires the postgres dependency group") from exc
            tracer = trace.get_tracer(service_name)
        self._tracer = tracer

    @contextmanager
    def stage(self, stage: str, metadata: Mapping[str, Any] | None = None) -> Iterator[None]:
        with self._tracer.start_as_current_span(f"proofcounsel.{stage}") as span:
            for key, value in redact_metadata(metadata or {}).items():
                if value is not None:
                    span.set_attribute(f"proofcounsel.{key}", value)
            yield

    def emit(self, event: TelemetryEvent) -> None:
        with self._tracer.start_as_current_span(f"proofcounsel.event.{event.stage}") as span:
            span.set_attribute("proofcounsel.status", event.status)
            span.set_attribute("proofcounsel.elapsed_ms", event.elapsed_ms)
            span.set_attribute("proofcounsel.input_tokens", event.input_tokens)
            span.set_attribute("proofcounsel.output_tokens", event.output_tokens)
            span.set_attribute("proofcounsel.provider_cost", event.provider_cost)
            for key, value in redact_metadata(event.metadata).items():
                if value is not None:
                    span.set_attribute(f"proofcounsel.{key}", value)


@contextmanager
def timed_stage(
    sink: TelemetrySink,
    *,
    tenant_id: UUID,
    document_id: UUID | None,
    source_sha256: str | None,
    stage: str,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    started = time.perf_counter()
    status = "ok"
    try:
        with stage_span(sink, stage, metadata):
            yield
    except Exception:
        status = "error"
        raise
    finally:
        sink.emit(
            TelemetryEvent(
                tenant_id=tenant_id,
                document_id=document_id,
                source_sha256=source_sha256,
                stage=stage,
                status=status,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                metadata=dict(metadata or {}),
            )
        )


@contextmanager
def stage_span(
    sink: TelemetrySink,
    stage: str,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """Open an optional OTel span without emitting a duplicate JSONL event."""
    stage_factory = getattr(sink, "stage", None)
    with stage_factory(stage, metadata) if stage_factory else nullcontext():
        yield


def redact_metadata(values: Mapping[str, Any]) -> dict[str, int | float | str | bool | None]:
    """Keep operational dimensions while hashing likely document/query content."""
    safe: dict[str, int | float | str | bool | None] = {}
    for key, value in values.items():
        normalized_key = str(key).casefold()
        if any(marker in normalized_key for marker in ("text", "query", "answer", "content", "prompt")):
            safe[str(key)] = hash_value(value)
        elif isinstance(value, (int, float, str, bool)) or value is None:
            safe[str(key)] = value
        else:
            safe[str(key)] = hash_value(value)
    return safe


def hash_value(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
