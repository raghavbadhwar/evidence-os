from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency
    psycopg = None  # type: ignore[assignment]

from proofcounsel_ingest.lifecycle import PostgresLifecycleRepository
from scripts.migrate import apply_migrations

ROOT = Path(__file__).resolve().parents[2]
ADMIN_DSN = os.environ.get("PROOFCOUNSEL_TEST_POSTGRES_ADMIN_DSN")
pytestmark = pytest.mark.integration


def test_postgres_lifecycle_tombstone_outbox_hold_and_audit() -> None:
    if not ADMIN_DSN:
        pytest.skip("set PROOFCOUNSEL_TEST_POSTGRES_ADMIN_DSN for live lifecycle coverage")
    if psycopg is None:
        pytest.skip("install the optional 'postgres' dependency group for live lifecycle coverage")

    tenant_id = uuid4()
    regular_document = uuid4()
    held_document = uuid4()
    _seed_documents(ADMIN_DSN, tenant_id, regular_document, held_document)
    try:
        repository = PostgresLifecycleRepository(ADMIN_DSN)
        regular = repository.revoke_and_enqueue(
            tenant_id=tenant_id,
            document_id=regular_document,
            artifact_relative_path=f"documents/{regular_document}",
        )
        held = repository.revoke_and_enqueue(
            tenant_id=tenant_id,
            document_id=held_document,
            artifact_relative_path=f"documents/{held_document}",
        )

        assert regular.query_access_revoked
        assert regular.cleanup_enqueued
        assert not regular.artifacts_deleted
        assert held.blocked_by_legal_hold
        assert not held.cleanup_enqueued
        assert not held.artifacts_deleted

        with psycopg.connect(ADMIN_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, legal_hold FROM documents WHERE tenant_id = %s ORDER BY legal_hold",
                (str(tenant_id),),
            )
            assert cursor.fetchall() == [("deleted", False), ("deleted", True)]
            cursor.execute(
                "SELECT count(*) FROM outbox_events WHERE tenant_id = %s",
                (str(tenant_id),),
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                "SELECT count(*), count(payload_sha256), count(event_hash) "
                "FROM audit_events WHERE tenant_id = %s",
                (str(tenant_id),),
            )
            assert cursor.fetchone() == (2, 2, 2)
    finally:
        _cleanup(ADMIN_DSN, tenant_id)


def _seed_documents(admin_dsn: str, tenant_id: UUID, regular_document: UUID, held_document: UUID) -> None:
    apply_migrations(admin_dsn, ROOT / "migrations")
    with psycopg.connect(admin_dsn) as connection, connection.cursor() as cursor:
        for document_id, legal_hold in ((regular_document, False), (held_document, True)):
            source_hash = hashlib.sha256(str(document_id).encode()).hexdigest()
            cursor.execute(
                """
                INSERT INTO documents (
                    document_id, tenant_id, source_sha256, original_filename,
                    detected_mime, byte_size, schema_version, extractor_version,
                    parser_config_hash, legal_hold, status
                ) VALUES (%s, %s, %s, %s, 'text/plain', 1, '1.0', 'test', %s, %s, 'ready')
                """,
                (
                    str(document_id),
                    str(tenant_id),
                    source_hash,
                    f"{document_id}.txt",
                    hashlib.sha256(b"test-config").hexdigest(),
                    legal_hold,
                ),
            )


def _cleanup(admin_dsn: str, tenant_id: UUID) -> None:
    with psycopg.connect(admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM audit_events WHERE tenant_id = %s", (str(tenant_id),))
        cursor.execute("DELETE FROM outbox_events WHERE tenant_id = %s", (str(tenant_id),))
        cursor.execute("DELETE FROM documents WHERE tenant_id = %s", (str(tenant_id),))
        connection.commit()
