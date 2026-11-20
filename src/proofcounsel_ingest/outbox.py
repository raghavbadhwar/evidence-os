"""Tenant-scoped, idempotent outbox implementations for asynchronous cleanup."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from proofcounsel_ingest.jobs import retry_delay_seconds

OutboxStatus = Literal["pending", "running", "completed", "dead_letter"]


class OutboxEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    aggregate_type: str = Field(min_length=1, max_length=120)
    aggregate_id: UUID
    event_type: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=255)
    status: OutboxStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    claimed_by: str | None = None
    lease_until: datetime | None = None
    last_error: str | None = None


class OutboxPort(Protocol):
    def enqueue(self, event: OutboxEvent) -> OutboxEvent: ...

    def claim(self, worker_id: str, *, lease_seconds: int = 300) -> OutboxEvent | None: ...

    def complete(self, event_id: UUID, *, worker_id: str | None = None) -> None: ...

    def fail(
        self,
        event_id: UUID,
        error: str,
        *,
        retry: bool,
        worker_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None: ...


class SQLiteOutbox:
    def __init__(self, path: Path, tenant_id: UUID) -> None:
        self.path = path.expanduser().resolve()
        self.tenant_id = tenant_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS outbox_events (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    claimed_by TEXT,
                    lease_until TEXT,
                    last_error TEXT,
                    UNIQUE (tenant_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS outbox_claim_idx
                    ON outbox_events (tenant_id, status, available_at, lease_until);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def enqueue(self, event: OutboxEvent) -> OutboxEvent:
        _require_tenant(event.tenant_id, self.tenant_id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox_events (
                    event_id, tenant_id, aggregate_type, aggregate_id, event_type,
                    payload_json, idempotency_key, status, attempts, available_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    str(event.tenant_id),
                    event.aggregate_type,
                    str(event.aggregate_id),
                    event.event_type,
                    json.dumps(event.payload, sort_keys=True, ensure_ascii=False),
                    event.idempotency_key,
                    event.status,
                    event.attempts,
                    event.available_at.isoformat(),
                ),
            )
            stored = connection.execute(
                "SELECT * FROM outbox_events WHERE tenant_id = ? AND idempotency_key = ?",
                (str(self.tenant_id), event.idempotency_key),
            ).fetchone()
        if stored is None:
            raise RuntimeError("outbox idempotency record disappeared after insert")
        return _sqlite_event(stored)

    def claim(self, worker_id: str, *, lease_seconds: int = 300) -> OutboxEvent | None:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM outbox_events
                WHERE tenant_id = ? AND available_at <= ?
                  AND (status = 'pending' OR (status = 'running' AND lease_until < ?))
                ORDER BY available_at, event_id
                LIMIT 1
                """,
                (str(self.tenant_id), now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE outbox_events SET status = 'running', attempts = attempts + 1,
                    claimed_by = ?, lease_until = ?, last_error = NULL
                WHERE tenant_id = ? AND event_id = ?
                """,
                (worker_id, lease_until.isoformat(), str(self.tenant_id), row["event_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM outbox_events WHERE tenant_id = ? AND event_id = ?",
                (str(self.tenant_id), row["event_id"]),
            ).fetchone()
            connection.commit()
        return _sqlite_event(claimed) if claimed is not None else None

    def complete(self, event_id: UUID, *, worker_id: str | None = None) -> None:
        clauses = ["tenant_id = ?", "event_id = ?", "status = 'running'"]
        params: list[object] = [str(self.tenant_id), str(event_id)]
        if worker_id is not None:
            clauses.append("claimed_by = ?")
            params.append(worker_id)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE outbox_events SET status = 'completed', claimed_by = NULL, lease_until = NULL "
                "WHERE " + " AND ".join(clauses),
                params,
            ).rowcount
            if updated != 1:
                raise ValueError("outbox event is not running under the requested worker")

    def fail(
        self,
        event_id: UUID,
        error: str,
        *,
        retry: bool,
        worker_id: str | None = None,
        retry_after_seconds: int = 5,
    ) -> None:
        status = "pending" if retry else "dead_letter"
        clauses = ["tenant_id = ?", "event_id = ?", "status = 'running'"]
        lookup: list[object] = [str(self.tenant_id), str(event_id)]
        if worker_id is not None:
            clauses.append("claimed_by = ?")
            lookup.append(worker_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM outbox_events WHERE " + " AND ".join(clauses), lookup
            ).fetchone()
            if row is None:
                raise ValueError("outbox event is not running under the requested worker")
            delay = (
                retry_after_seconds
                if retry_after_seconds is not None
                else retry_delay_seconds(int(row["attempts"]))
            )
            available_at = datetime.now(UTC) + timedelta(seconds=max(delay, 0))
            params: list[object] = [status, error[:4000], available_at.isoformat(), *lookup]
            updated = connection.execute(
                "UPDATE outbox_events SET status = ?, last_error = ?, available_at = ?, "
                "claimed_by = NULL, lease_until = NULL WHERE " + " AND ".join(clauses),
                params,
            ).rowcount
            if updated != 1:
                raise ValueError("outbox event is not running under the requested worker")


class PostgresOutbox:
    """PostgreSQL outbox using tenant-scoped row locks and ``SKIP LOCKED``."""

    def __init__(self, dsn: str, tenant_id: UUID, *, connect: Any | None = None) -> None:
        self.dsn = dsn
        self.tenant_id = tenant_id
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("PostgreSQL outbox requires the optional 'postgres' dependency group") from exc
            connect = psycopg.connect
        self._connect = connect

    def _connection(self) -> Any:
        return self._connect(self.dsn)

    def enqueue(self, event: OutboxEvent) -> OutboxEvent:
        _require_tenant(event.tenant_id, self.tenant_id)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                _set_scope(cursor, self.tenant_id)
                cursor.execute(
                    """
                    INSERT INTO outbox_events (
                        event_id, tenant_id, aggregate_type, aggregate_id, event_type,
                        payload, idempotency_key, status, attempts, available_at
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    """,
                    (
                        str(event.event_id),
                        str(event.tenant_id),
                        event.aggregate_type,
                        str(event.aggregate_id),
                        event.event_type,
                        json.dumps(event.payload, sort_keys=True, ensure_ascii=False),
                        event.idempotency_key,
                        event.status,
                        event.attempts,
                        event.available_at,
                    ),
                )
                cursor.execute(
                    "SELECT event_id, tenant_id, aggregate_type, aggregate_id, event_type, payload, "
                    "idempotency_key, status, attempts, available_at, claimed_by, lease_until, last_error "
                    "FROM outbox_events WHERE tenant_id = %s AND idempotency_key = %s",
                    (str(self.tenant_id), event.idempotency_key),
                )
                row = cursor.fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("outbox insert did not return a row")
            return _postgres_event(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim(self, worker_id: str, *, lease_seconds: int = 300) -> OutboxEvent | None:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                _set_scope(cursor, self.tenant_id)
                cursor.execute(
                    """
                    SELECT event_id, tenant_id, aggregate_type, aggregate_id, event_type, payload,
                           idempotency_key, status, attempts, available_at, claimed_by, lease_until, last_error
                    FROM outbox_events
                    WHERE tenant_id = %s AND available_at <= now()
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
                    UPDATE outbox_events
                    SET status = 'running', attempts = attempts + 1,
                        claimed_by = %s, lease_until = now() + (%s * interval '1 second')
                    WHERE tenant_id = %s AND event_id = %s
                    RETURNING event_id, tenant_id, aggregate_type, aggregate_id, event_type, payload,
                              idempotency_key, status, attempts, available_at, claimed_by, lease_until, last_error
                    """,
                    (worker_id, lease_seconds, str(self.tenant_id), str(row[0])),
                )
                claimed = cursor.fetchone()
            connection.commit()
            return _postgres_event(claimed) if claimed is not None else None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(self, event_id: UUID, *, worker_id: str | None = None) -> None:
        self._update(event_id, "completed", None, 0, worker_id=worker_id)

    def fail(
        self,
        event_id: UUID,
        error: str,
        *,
        retry: bool,
        worker_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self._update(
            event_id,
            "pending" if retry else "dead_letter",
            error[:4000],
            retry_after_seconds,
            worker_id=worker_id,
        )

    def _update(
        self,
        event_id: UUID,
        status: str,
        error: str | None,
        retry_after_seconds: int | None,
        *,
        worker_id: str | None,
    ) -> None:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                _set_scope(cursor, self.tenant_id)
                where = "tenant_id = %s AND event_id = %s AND status = 'running'"
                lookup: list[Any] = [str(self.tenant_id), str(event_id)]
                if worker_id is not None:
                    where += " AND claimed_by = %s"
                    lookup.append(worker_id)
                cursor.execute("SELECT attempts FROM outbox_events WHERE " + where, lookup)
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("outbox event is not running under the requested worker")
                delay = (
                    retry_after_seconds
                    if retry_after_seconds is not None
                    else retry_delay_seconds(int(row[0]))
                )
                params: list[Any] = [status, error, status, max(delay, 0), *lookup]
                cursor.execute(
                    """
                    UPDATE outbox_events
                    SET status = %s, last_error = %s,
                        available_at = CASE WHEN %s = 'pending'
                            THEN now() + (%s * interval '1 second') ELSE available_at END,
                        claimed_by = NULL, lease_until = NULL
                    WHERE """ + where,
                    params,
                )
                if cursor.rowcount != 1:
                    raise ValueError("outbox event is not running under the requested worker")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _require_tenant(event_tenant: UUID, queue_tenant: UUID) -> None:
    if event_tenant != queue_tenant:
        raise ValueError("outbox event tenant does not match queue scope")


def _set_scope(cursor: Any, tenant_id: UUID) -> None:
    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
    cursor.execute("SELECT set_config('app.actor_role', 'service_worker', true)")


def _sqlite_event(row: sqlite3.Row) -> OutboxEvent:
    return OutboxEvent(
        event_id=UUID(row["event_id"]),
        tenant_id=UUID(row["tenant_id"]),
        aggregate_type=row["aggregate_type"],
        aggregate_id=UUID(row["aggregate_id"]),
        event_type=row["event_type"],
        payload=json.loads(row["payload_json"]),
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        attempts=int(row["attempts"]),
        available_at=datetime.fromisoformat(row["available_at"]),
        claimed_by=row["claimed_by"],
        lease_until=datetime.fromisoformat(row["lease_until"]) if row["lease_until"] else None,
        last_error=row["last_error"],
    )


def _postgres_event(row: Any) -> OutboxEvent:
    return OutboxEvent(
        event_id=UUID(str(row[0])),
        tenant_id=UUID(str(row[1])),
        aggregate_type=str(row[2]),
        aggregate_id=UUID(str(row[3])),
        event_type=str(row[4]),
        payload=row[5] if isinstance(row[5], dict) else json.loads(row[5]),
        idempotency_key=str(row[6]),
        status=row[7],
        attempts=int(row[8]),
        available_at=row[9],
        claimed_by=row[10],
        lease_until=row[11],
        last_error=row[12],
    )
