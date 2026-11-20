"""Lease-based job queues for local tests and PostgreSQL workers."""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

JobStatus = Literal["pending", "running", "completed", "dead_letter", "cancelled"]


class JobRecord(BaseModel):
    job_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    kind: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=255)
    status: JobStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    claimed_by: str | None = None
    lease_until: datetime | None = None
    last_error: str | None = None


class JobQueue(Protocol):
    def enqueue(self, job: JobRecord) -> JobRecord: ...

    def claim(self, worker_id: str, *, lease_seconds: int = 300) -> JobRecord | None: ...

    def complete(self, job_id: UUID, *, worker_id: str | None = None) -> None: ...

    def fail(
        self,
        job_id: UUID,
        error: str,
        *,
        retry: bool,
        worker_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None: ...

    def heartbeat(self, job_id: UUID, worker_id: str, *, lease_seconds: int = 300) -> JobRecord: ...

    def cancel(self, job_id: UUID) -> None: ...


class SQLiteJobQueue:
    def __init__(self, path: Path, tenant_id: UUID) -> None:
        self.path = path.expanduser().resolve()
        self.tenant_id = tenant_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    claimed_by TEXT,
                    lease_until TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_claim_idx
                    ON jobs (status, available_at, lease_until);
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_tenant_idempotency_uq
                    ON jobs (tenant_id, idempotency_key);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def enqueue(self, job: JobRecord) -> JobRecord:
        if job.tenant_id != self.tenant_id:
            raise ValueError("job tenant does not match queue scope")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs (job_id, tenant_id, kind, payload_json, idempotency_key,
                                  status, attempts, available_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job.job_id),
                    str(job.tenant_id),
                    job.kind,
                    json.dumps(job.payload, sort_keys=True, ensure_ascii=False),
                    job.idempotency_key,
                    job.status,
                    job.attempts,
                    job.available_at.isoformat(),
                ),
            )
            stored = connection.execute(
                "SELECT * FROM jobs WHERE tenant_id = ? AND idempotency_key = ?",
                (str(self.tenant_id), job.idempotency_key),
            ).fetchone()
        if stored is None:
            raise RuntimeError("job idempotency record disappeared after insert")
        return _job_from_row(stored)

    def claim(self, worker_id: str, *, lease_seconds: int = 300) -> JobRecord | None:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE tenant_id = ? AND available_at <= ?
                  AND (status = 'pending' OR (status = 'running' AND lease_until < ?))
                ORDER BY available_at, rowid
                LIMIT 1
                """,
                (str(self.tenant_id), now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs SET status = 'running', attempts = attempts + 1,
                    claimed_by = ?, lease_until = ?, last_error = NULL
                WHERE job_id = ?
                """,
                (worker_id, lease_until.isoformat(), row["job_id"]),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            connection.commit()
        return _job_from_row(updated) if updated is not None else None

    def complete(self, job_id: UUID, *, worker_id: str | None = None) -> None:
        with self._connect() as connection:
            clauses = ["tenant_id = ?", "job_id = ?", "status = 'running'"]
            parameters: list[object] = [str(self.tenant_id), str(job_id)]
            if worker_id is not None:
                clauses.append("claimed_by = ?")
                parameters.append(worker_id)
            updated = connection.execute(
                "UPDATE jobs SET status = 'completed', claimed_by = NULL, lease_until = NULL WHERE "
                + " AND ".join(clauses),
                parameters,
            ).rowcount
            if updated != 1:
                raise ValueError("job is not running under the requested worker")

    def fail(
        self,
        job_id: UUID,
        error: str,
        *,
        retry: bool,
        worker_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        status = "pending" if retry else "dead_letter"
        with self._connect() as connection:
            clauses = ["tenant_id = ?", "job_id = ?", "status = 'running'"]
            lookup: list[object] = [str(self.tenant_id), str(job_id)]
            if worker_id is not None:
                clauses.append("claimed_by = ?")
                lookup.append(worker_id)
            row = connection.execute(
                "SELECT attempts FROM jobs WHERE " + " AND ".join(clauses), lookup
            ).fetchone()
            if row is None:
                raise ValueError("job is not running under the requested worker")
            delay = (
                retry_after_seconds
                if retry_after_seconds is not None
                else retry_delay_seconds(int(row["attempts"]))
            )
            available_at = datetime.now(UTC) + timedelta(seconds=max(delay, 0))
            parameters: list[object] = [status, error[:4000], available_at.isoformat(), *lookup]
            updated = connection.execute(
                "UPDATE jobs SET status = ?, claimed_by = NULL, lease_until = NULL, last_error = ?, "
                "available_at = ? WHERE " + " AND ".join(clauses),
                parameters,
            ).rowcount
            if updated != 1:
                raise ValueError("job is not running under the requested worker")

    def heartbeat(self, job_id: UUID, worker_id: str, *, lease_seconds: int = 300) -> JobRecord:
        lease_until = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET lease_until = ? WHERE tenant_id = ? AND job_id = ? "
                "AND status = 'running' AND claimed_by = ?",
                (lease_until.isoformat(), str(self.tenant_id), str(job_id), worker_id),
            ).rowcount
            if updated != 1:
                raise ValueError("job lease is not owned by the requested worker")
            row = connection.execute(
                "SELECT * FROM jobs WHERE tenant_id = ? AND job_id = ?",
                (str(self.tenant_id), str(job_id)),
            ).fetchone()
        if row is None:
            raise ValueError("job disappeared while renewing its lease")
        return _job_from_row(row)

    def cancel(self, job_id: UUID) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', claimed_by = NULL, lease_until = NULL "
                "WHERE tenant_id = ? AND job_id = ? AND status IN ('pending', 'running')",
                (str(self.tenant_id), str(job_id)),
            )


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=UUID(row["job_id"]),
        tenant_id=UUID(row["tenant_id"]),
        kind=row["kind"],
        payload=json.loads(row["payload_json"]),
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        attempts=int(row["attempts"]),
        available_at=datetime.fromisoformat(row["available_at"]),
        claimed_by=row["claimed_by"],
        lease_until=datetime.fromisoformat(row["lease_until"]) if row["lease_until"] else None,
        last_error=row["last_error"],
    )


class PostgresJobQueue:
    """PostgreSQL worker queue using row locks and ``SKIP LOCKED``."""

    def __init__(
        self,
        dsn: str,
        tenant_id: UUID,
        *,
        connect: Any | None = None,
    ) -> None:
        self.dsn = dsn
        self.tenant_id = tenant_id
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("PostgreSQL jobs require the optional 'postgres' dependency group") from exc
            connect = psycopg.connect
        self._connect = connect

    def _connection(self) -> Any:
        return self._connect(self.dsn)

    def enqueue(self, job: JobRecord) -> JobRecord:
        if job.tenant_id != self.tenant_id:
            raise ValueError("job tenant does not match queue scope")
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                _set_job_scope(cursor, self.tenant_id)
                cursor.execute(
                    """
                    INSERT INTO jobs (job_id, tenant_id, kind, payload, idempotency_key, status, attempts, available_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    """,
                    (
                        str(job.job_id),
                        str(job.tenant_id),
                        job.kind,
                        json.dumps(job.payload, sort_keys=True, ensure_ascii=False),
                        job.idempotency_key,
                        job.status,
                        job.attempts,
                        job.available_at,
                    ),
                )
                cursor.execute(
                    "SELECT * FROM jobs WHERE tenant_id = %s AND idempotency_key = %s",
                    (str(self.tenant_id), job.idempotency_key),
                )
                row = cursor.fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("job insert did not return a row")
            return _postgres_job_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim(self, worker_id: str, *, lease_seconds: int = 300) -> JobRecord | None:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                _set_job_scope(cursor, self.tenant_id)
                cursor.execute(
                    """
                    SELECT * FROM jobs
                    WHERE tenant_id = %s
                      AND available_at <= now()
                      AND (status = 'pending' OR (status = 'running' AND lease_until < now()))
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (str(self.tenant_id),),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.commit()
                    return None
                cursor.execute(
                    """
                    UPDATE jobs
                    SET status = 'running', attempts = attempts + 1,
                        claimed_by = %s, lease_until = now() + (%s * interval '1 second'),
                        last_error = NULL, updated_at = now()
                    WHERE job_id = %s
                    RETURNING *
                    """,
                    (worker_id, lease_seconds, str(row[0])),
                )
                claimed = cursor.fetchone()
            connection.commit()
            return _postgres_job_from_row(claimed) if claimed is not None else None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(self, job_id: UUID, *, worker_id: str | None = None) -> None:
        self._update(job_id, "completed", None, 0, worker_id=worker_id)

    def fail(
        self,
        job_id: UUID,
        error: str,
        *,
        retry: bool,
        worker_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self._update(
            job_id,
            "pending" if retry else "dead_letter",
            error[:4000],
            retry_after_seconds,
            worker_id=worker_id,
        )

    def heartbeat(self, job_id: UUID, worker_id: str, *, lease_seconds: int = 300) -> JobRecord:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                _set_job_scope(cursor, self.tenant_id)
                cursor.execute(
                    "UPDATE jobs SET lease_until = now() + (%s * interval '1 second'), updated_at = now() "
                    "WHERE tenant_id = %s AND job_id = %s AND status = 'running' AND claimed_by = %s "
                    "RETURNING *",
                    (lease_seconds, str(self.tenant_id), str(job_id), worker_id),
                )
                row = cursor.fetchone()
            connection.commit()
            if row is None:
                raise ValueError("job lease is not owned by the requested worker")
            return _postgres_job_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cancel(self, job_id: UUID) -> None:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                _set_job_scope(cursor, self.tenant_id)
                cursor.execute(
                    "UPDATE jobs SET status = 'cancelled', claimed_by = NULL, lease_until = NULL, updated_at = now() "
                    "WHERE tenant_id = %s AND job_id = %s AND status IN ('pending', 'running')",
                    (str(self.tenant_id), str(job_id)),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _update(
        self,
        job_id: UUID,
        status: str,
        error: str | None,
        retry_after_seconds: int | None,
        *,
        worker_id: str | None = None,
    ) -> None:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                _set_job_scope(cursor, self.tenant_id)
                where = "tenant_id = %s AND job_id = %s AND status = 'running'"
                lookup: list[Any] = [str(self.tenant_id), str(job_id)]
                if worker_id is not None:
                    where += " AND claimed_by = %s"
                    lookup.append(worker_id)
                cursor.execute("SELECT attempts FROM jobs WHERE " + where, lookup)
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("job is not running under the requested worker")
                delay = (
                    retry_after_seconds
                    if retry_after_seconds is not None
                    else retry_delay_seconds(int(row[0]))
                )
                parameters: list[Any] = [status, error, status, max(delay, 0), *lookup]
                cursor.execute(
                    """
                    UPDATE jobs
                    SET status = %s, claimed_by = NULL, lease_until = NULL,
                        last_error = %s,
                        available_at = CASE WHEN %s = 'pending'
                            THEN now() + (%s * interval '1 second') ELSE available_at END,
                        updated_at = now()
                    WHERE """ + where,
                    parameters,
                )
                if cursor.rowcount != 1:
                    raise ValueError("job is not running under the requested worker")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _set_job_scope(cursor: Any, tenant_id: UUID) -> None:
    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
    cursor.execute("SELECT set_config('app.actor_role', 'service_worker', true)")


def _postgres_job_from_row(row: Any) -> JobRecord:
    return JobRecord(
        job_id=UUID(str(row[0])),
        tenant_id=UUID(str(row[1])),
        kind=str(row[2]),
        payload=row[3] if isinstance(row[3], dict) else json.loads(row[3]),
        idempotency_key=str(row[4]),
        status=cast(JobStatus, str(row[5])),
        attempts=int(row[6]),
        available_at=row[7],
        claimed_by=row[8],
        lease_until=row[9],
        last_error=row[10],
    )


def retry_delay_seconds(
    attempts: int,
    *,
    base_seconds: int = 5,
    max_seconds: int = 300,
    jitter_ratio: float = 0.2,
) -> float:
    """Return bounded exponential backoff with worker-desynchronizing jitter."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if base_seconds <= 0 or max_seconds < base_seconds or not 0 <= jitter_ratio <= 1:
        raise ValueError("invalid retry backoff configuration")
    exponential = min(max_seconds, base_seconds * (2 ** min(attempts - 1, 10)))
    jitter = random.SystemRandom().uniform(1 - jitter_ratio, 1 + jitter_ratio)
    candidate = round(float(exponential) * jitter, 3)
    return float(min(float(max_seconds), candidate))
