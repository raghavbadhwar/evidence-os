from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from proofcounsel_ingest.telemetry import JsonlTelemetrySink, OpenTelemetrySink, TelemetryEvent, timed_stage

TENANT = UUID("00000000-0000-0000-0000-000000001002")


def test_telemetry_hashes_document_content_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlTelemetrySink(path)
    event = TelemetryEvent(
        tenant_id=TENANT,
        stage="retrieval",
        status="ok",
        elapsed_ms=1.5,
        metadata={"query": "confidential question", "hits": 3},
    )
    sink.emit(event)
    payload = json.loads(path.read_text())
    assert payload["metadata"]["hits"] == 3
    assert payload["metadata"]["query"] != "confidential question"
    assert len(payload["metadata"]["query"]) == 64


def test_timed_stage_records_errors_without_swallowing_them(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlTelemetrySink(path)
    with pytest.raises(RuntimeError, match="boom"), timed_stage(
        sink, tenant_id=TENANT, document_id=None, source_sha256=None, stage="parse"
    ):
        raise RuntimeError("boom")
    payload = json.loads(path.read_text())
    assert payload["stage"] == "parse"
    assert payload["status"] == "error"


def test_opentelemetry_sink_emits_stage_and_event_spans() -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sink = OpenTelemetrySink(provider.get_tracer("proofcounsel-test"))
    with timed_stage(
        sink,
        tenant_id=TENANT,
        document_id=None,
        source_sha256=None,
        stage="parse",
        metadata={"query": "private text"},
    ):
        pass
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {"proofcounsel.parse", "proofcounsel.event.parse"}
