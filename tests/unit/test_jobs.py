from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from proofcounsel_ingest.jobs import JobRecord, SQLiteJobQueue, retry_delay_seconds

TENANT = UUID("00000000-0000-0000-0000-000000001001")


def job(key: str, *, available_at: datetime | None = None) -> JobRecord:
    return JobRecord(
        tenant_id=TENANT,
        kind="ingest",
        payload={"source": "hash"},
        idempotency_key=key,
        available_at=available_at or datetime.now(UTC),
    )


def test_enqueue_is_idempotent_and_lease_claim_is_exclusive(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.sqlite3", TENANT)
    first = queue.enqueue(job("same-key"))
    reused = queue.enqueue(job("same-key"))
    assert reused.job_id == first.job_id

    claimed = queue.claim("worker-a", lease_seconds=60)
    assert claimed and claimed.job_id == first.job_id
    assert queue.claim("worker-b") is None

    queue.complete(first.job_id)
    assert queue.claim("worker-c") is None


def test_expired_lease_can_be_reclaimed_and_failures_dead_letter(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.sqlite3", TENANT)
    queued = queue.enqueue(job("retry-key", available_at=datetime.now(UTC) - timedelta(seconds=1)))
    claimed = queue.claim("worker-a", lease_seconds=0)
    assert claimed and claimed.job_id == queued.job_id
    # A zero-second lease is immediately eligible for recovery.
    recovered = queue.claim("worker-b", lease_seconds=60)
    assert recovered and recovered.job_id == queued.job_id
    queue.fail(queued.job_id, "permanent parser error", retry=False)
    assert queue.claim("worker-c") is None


def test_lease_heartbeat_is_owner_bound_and_cancel_prevents_claim(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "jobs.sqlite3", TENANT)
    queued = queue.enqueue(job("heartbeat-key"))
    claimed = queue.claim("worker-a", lease_seconds=1)
    assert claimed and claimed.job_id == queued.job_id

    with pytest.raises(ValueError, match="not owned"):
        queue.heartbeat(queued.job_id, "worker-b", lease_seconds=60)
    renewed = queue.heartbeat(queued.job_id, "worker-a", lease_seconds=60)
    assert renewed.claimed_by == "worker-a"
    queue.cancel(queued.job_id)
    with pytest.raises(ValueError, match="not running"):
        queue.complete(queued.job_id, worker_id="worker-a")


def test_retry_backoff_is_bounded_and_supports_deterministic_operator_override() -> None:
    assert retry_delay_seconds(1, jitter_ratio=0) == 5
    assert retry_delay_seconds(3, base_seconds=5, max_seconds=20, jitter_ratio=0) == 20
    assert retry_delay_seconds(20, base_seconds=5, max_seconds=20, jitter_ratio=0) == 20
    with pytest.raises(ValueError):
        retry_delay_seconds(0)
