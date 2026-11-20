"""Bounded stage-job execution on top of the lease-based queue ports."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from proofcounsel_ingest.jobs import JobQueue, JobRecord
from proofcounsel_ingest.telemetry import NullTelemetrySink, TelemetrySink, timed_stage

StageHandler = Callable[[JobRecord], None]
WorkerStatus = Literal["completed", "retried", "dead_letter", "cancelled"]


class RetryableStageError(RuntimeError):
    """A stage failure that should be retried while the attempt budget remains."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class StageExecutionResult(BaseModel):
    job_id: UUID
    status: WorkerStatus
    attempts: int = Field(ge=1)
    error: str | None = None


class StageWorker:
    """Execute one queued stage at a time with lease renewal.

    Queue operations commit before a handler starts. Handler code therefore
    never runs inside the queue transaction, and an expired lease can be
    recovered by another worker after a process termination.
    """

    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, StageHandler],
        *,
        worker_id: str,
        lease_seconds: int = 300,
        heartbeat_interval_seconds: float | None = None,
        max_attempts: int = 5,
        telemetry_sink: TelemetrySink | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id cannot be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        interval = heartbeat_interval_seconds or max(1.0, lease_seconds / 3)
        if interval <= 0 or interval >= lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease")
        self.queue = queue
        self.handlers = dict(handlers)
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = interval
        self.max_attempts = max_attempts
        self.telemetry_sink = telemetry_sink or NullTelemetrySink()

    def run_once(self) -> StageExecutionResult | None:
        job = self.queue.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        handler = self.handlers.get(job.kind)
        if handler is None:
            return self._fail(job, f"no handler registered for job kind {job.kind!r}", retry=False)

        try:
            self._execute_with_heartbeats(job, handler)
        except RetryableStageError as exc:
            return self._fail(
                job,
                str(exc) or type(exc).__name__,
                retry=job.attempts < self.max_attempts,
                retry_after_seconds=exc.retry_after_seconds,
            )
        except Exception as exc:
            return self._fail(job, str(exc) or type(exc).__name__, retry=False)

        try:
            self.queue.complete(job.job_id, worker_id=self.worker_id)
        except ValueError as exc:
            if "not running" in str(exc):
                return StageExecutionResult(
                    job_id=job.job_id,
                    status="cancelled",
                    attempts=job.attempts,
                    error="job was cancelled or lease ownership changed before completion",
                )
            raise
        return StageExecutionResult(job_id=job.job_id, status="completed", attempts=job.attempts)

    def run_until_idle(self, *, max_jobs: int | None = None) -> list[StageExecutionResult]:
        if max_jobs is not None and max_jobs <= 0:
            raise ValueError("max_jobs must be positive when provided")
        results: list[StageExecutionResult] = []
        while max_jobs is None or len(results) < max_jobs:
            result = self.run_once()
            if result is None:
                return results
            results.append(result)
        return results

    def _execute_with_heartbeats(self, job: JobRecord, handler: StageHandler) -> None:
        stop_heartbeat = threading.Event()

        def heartbeat_loop() -> None:
            while not stop_heartbeat.wait(self.heartbeat_interval_seconds):
                try:
                    self.queue.heartbeat(job.job_id, self.worker_id, lease_seconds=self.lease_seconds)
                except ValueError:
                    # The completion path will report a cancellation or lease loss.
                    return

        heartbeat_thread = threading.Thread(target=heartbeat_loop, name=f"heartbeat-{job.job_id}", daemon=True)
        heartbeat_thread.start()
        try:
            with timed_stage(
                self.telemetry_sink,
                tenant_id=job.tenant_id,
                document_id=_payload_uuid(job, "document_id"),
                source_sha256=_payload_hash(job),
                stage="queue",
                metadata={"job_kind": job.kind, "attempt": job.attempts},
            ), ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"stage-{job.kind}") as executor:
                future: Future[None] = executor.submit(handler, job)
                while True:
                    try:
                        future.result(timeout=self.heartbeat_interval_seconds)
                        return
                    except TimeoutError:
                        continue
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=self.heartbeat_interval_seconds)

    def _fail(
        self,
        job: JobRecord,
        error: str,
        *,
        retry: bool,
        retry_after_seconds: int | None = None,
    ) -> StageExecutionResult:
        self.queue.fail(
            job.job_id,
            error,
            retry=retry,
            worker_id=self.worker_id,
            retry_after_seconds=retry_after_seconds,
        )
        return StageExecutionResult(
            job_id=job.job_id,
            status="retried" if retry else "dead_letter",
            attempts=job.attempts,
            error=error,
        )


def _payload_uuid(job: JobRecord, key: str) -> UUID | None:
    value = job.payload.get(key)
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _payload_hash(job: JobRecord) -> str | None:
    value = job.payload.get("source_sha256")
    if isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value):
        return value
    return None
