from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from proofcounsel_ingest.lifecycle import LifecyclePolicy, PostgresLifecycleRepository, RetentionPolicy


class FakeCursor:
    def __init__(self, rows: list[tuple | None]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple | None]] = []
        self.rowcount = 1

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple | None = None) -> None:
        self.executed.append((query, params))

    def fetchone(self):  # type: ignore[no-untyped-def]
        return self.rows.pop(0) if self.rows else None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor_value = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


def test_postgres_lifecycle_tombstones_and_enqueues_cleanup() -> None:
    cursor = FakeCursor(
        [
            (False, "ready"),
            (1,),
            None,
        ]
    )
    connection = FakeConnection(cursor)
    repository = PostgresLifecycleRepository(
        "postgresql://test",
        connect=lambda _dsn: connection,
    )
    document_id = uuid4()
    result = repository.revoke_and_enqueue(
        tenant_id=UUID("00000000-0000-0000-0000-000000001004"),
        document_id=document_id,
        artifact_relative_path="documents/doc",
    )

    assert result.query_access_revoked
    assert result.cleanup_enqueued
    assert not result.blocked_by_legal_hold
    assert connection.commits == 1
    assert any("INSERT INTO outbox_events" in query for query, _params in cursor.executed)


def test_postgres_lifecycle_rejects_cleanup_traversal_before_connecting() -> None:
    repository = PostgresLifecycleRepository("postgresql://test", connect=lambda _dsn: None)
    with pytest.raises(ValueError, match="relative"):
        repository.revoke_and_enqueue(
            tenant_id=UUID("00000000-0000-0000-0000-000000001004"),
            document_id=uuid4(),
            artifact_relative_path="../outside",
            policy=LifecyclePolicy(),
        )


def test_retention_policy_requires_positive_days() -> None:
    assert RetentionPolicy(retention_days=30).retention_days == 30
    with pytest.raises(ValueError):
        RetentionPolicy(retention_days=0)
