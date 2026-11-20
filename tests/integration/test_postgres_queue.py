from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency
    psycopg = None  # type: ignore[assignment]

from proofcounsel_ingest.jobs import JobRecord, PostgresJobQueue
from scripts.migrate import apply_migrations

ROOT = Path(__file__).resolve().parents[2]
ADMIN_DSN = os.environ.get("PROOFCOUNSEL_TEST_POSTGRES_ADMIN_DSN")
pytestmark = pytest.mark.integration


def test_postgres_queue_claims_retries_and_completes_idempotently() -> None:
    if not ADMIN_DSN:
        pytest.skip("set PROOFCOUNSEL_TEST_POSTGRES_ADMIN_DSN for live queue coverage")
    if psycopg is None:
        pytest.skip("install the optional 'postgres' dependency group for live queue coverage")

    apply_migrations(ADMIN_DSN, ROOT / "migrations")
    tenant_id = uuid4()
    job = JobRecord(
        tenant_id=tenant_id,
        kind="extract",
        payload={"document_id": str(uuid4()), "source_sha256": "a" * 64},
        idempotency_key=f"extract:{uuid4()}",
    )
    queue = PostgresJobQueue(ADMIN_DSN, tenant_id)
    try:
        first = queue.enqueue(job)
        reused = queue.enqueue(job.model_copy(update={"job_id": uuid4()}))
        assert reused.job_id == first.job_id

        claimed = queue.claim("worker-a", lease_seconds=30)
        assert claimed and claimed.attempts == 1
        renewed = queue.heartbeat(claimed.job_id, "worker-a", lease_seconds=30)
        assert renewed.claimed_by == "worker-a"
        queue.fail(claimed.job_id, "temporary outage", retry=True, worker_id="worker-a", retry_after_seconds=0)

        retried = queue.claim("worker-b", lease_seconds=30)
        assert retried and retried.attempts == 2
        queue.complete(retried.job_id, worker_id="worker-b")
        assert queue.claim("worker-c") is None
    finally:
        with psycopg.connect(ADMIN_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM jobs WHERE tenant_id = %s", (str(tenant_id),))
            connection.commit()
