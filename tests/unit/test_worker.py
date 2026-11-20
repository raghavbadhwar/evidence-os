from __future__ import annotations

import time
from pathlib import Path
from uuid import UUID

from proofcounsel_ingest.jobs import JobRecord, SQLiteJobQueue
from proofcounsel_ingest.telemetry import JsonlTelemetrySink
from proofcounsel_ingest.worker import RetryableStageError, StageWorker

TENANT = UUID("00000000-0000-0000-0000-000000001003")


def _job(key: str, kind: str = "extract") -> JobRecord:
    return JobRecord(tenant_id=TENANT, kind=kind, idempotency_key=key)


def test_stage_worker_completes_handlers_and_drains_the_queue(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.sqlite3", TENANT)
    queued = _job("one")
    queued.payload["document_id"] = str(UUID("00000000-0000-0000-0000-000000001008"))
    queue.enqueue(queued)
    queue.enqueue(_job("two"))
    handled: list[str] = []
    worker = StageWorker(
        queue,
        {"extract": lambda record: handled.append(record.idempotency_key)},
        worker_id="worker-a",
        lease_seconds=2,
        heartbeat_interval_seconds=0.1,
        telemetry_sink=JsonlTelemetrySink(tmp_path / "telemetry.jsonl"),
    )

    results = worker.run_until_idle()

    assert [result.status for result in results] == ["completed", "completed"]
    assert handled == ["one", "two"]
    assert worker.run_once() is None
    assert '"stage": "queue"' in (tmp_path / "telemetry.jsonl").read_text()


def test_stage_worker_retries_bounded_failures_then_dead_letters(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.sqlite3", TENANT)
    queue.enqueue(_job("retry"))
    calls = 0

    def flaky(_record: JobRecord) -> None:
        nonlocal calls
        calls += 1
        raise RetryableStageError("temporary provider outage", retry_after_seconds=0)

    worker = StageWorker(
        queue,
        {"extract": flaky},
        worker_id="worker-a",
        lease_seconds=2,
        heartbeat_interval_seconds=0.1,
        max_attempts=2,
    )

    first = worker.run_once()
    second = worker.run_once()
    assert first and first.status == "retried"
    assert second and second.status == "dead_letter"
    assert calls == 2


def test_stage_worker_renews_a_long_handler_lease(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.sqlite3", TENANT)
    queue.enqueue(_job("long"))
    worker = StageWorker(
        queue,
        {"extract": lambda _record: time.sleep(0.2)},
        worker_id="worker-a",
        lease_seconds=1,
        heartbeat_interval_seconds=0.05,
    )

    result = worker.run_once()

    assert result and result.status == "completed"
