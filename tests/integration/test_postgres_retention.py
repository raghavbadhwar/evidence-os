from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency
    psycopg = None  # type: ignore[assignment]

from proofcounsel_ingest.lifecycle import PostgresLifecycleRepository, RetentionPolicy
from scripts.migrate import apply_migrations

ROOT = Path(__file__).resolve().parents[2]
ADMIN_DSN = os.environ.get("PROOFCOUNSEL_TEST_POSTGRES_ADMIN_DSN")
pytestmark = pytest.mark.integration


def test_postgres_retention_excludes_holds_and_enqueues_expired_cleanup() -> None:
    if not ADMIN_DSN:
        pytest.skip("set PROOFCOUNSEL_TEST_POSTGRES_ADMIN_DSN for live retention coverage")
    if psycopg is None:
        pytest.skip("install the optional 'postgres' dependency group for live retention coverage")

    apply_migrations(ADMIN_DSN, ROOT / "migrations")
    tenant_id = uuid4()
    old_document = uuid4()
    recent_document = uuid4()
    held_document = uuid4()
    now = datetime.now(UTC)
    _seed(ADMIN_DSN, tenant_id, old_document, recent_document, held_document)
    try:
        repository = PostgresLifecycleRepository(ADMIN_DSN)
        policy = RetentionPolicy(retention_days=1)
        assert repository.expired_document_ids(tenant_id=tenant_id, default_policy=policy, now=now) == [old_document]
        results = repository.expire_documents(
            tenant_id=tenant_id,
            default_policy=policy,
            artifact_paths={old_document: f"documents/{old_document}"},
            now=now,
        )
        assert len(results) == 1
        assert results[0].cleanup_enqueued

        with psycopg.connect(ADMIN_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT document_id, status FROM documents WHERE tenant_id = %s ORDER BY document_id",
                (str(tenant_id),),
            )
            rows = {str(row[0]): row[1] for row in cursor.fetchall()}
            assert rows[str(old_document)] == "deleted"
            assert rows[str(recent_document)] == "ready"
            assert rows[str(held_document)] == "ready"
    finally:
        _cleanup(ADMIN_DSN, tenant_id)


def _seed(admin_dsn: str, tenant_id: UUID, old_document: UUID, recent_document: UUID, held_document: UUID) -> None:
    with psycopg.connect(admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO retention_policies "
            "(policy_id, tenant_id, matter_id, retention_days, legal_hold_default) "
            "VALUES (%s, %s, NULL, 1, false)",
            (str(uuid4()), str(tenant_id)),
        )
        for document_id, created_at, legal_hold in (
            (old_document, "now() - interval '2 days'", False),
            (recent_document, "now()", False),
            (held_document, "now() - interval '2 days'", True),
        ):
            source_hash = hashlib.sha256(str(document_id).encode()).hexdigest()
            cursor.execute(
                f"""
                INSERT INTO documents (
                    document_id, tenant_id, source_sha256, original_filename,
                    detected_mime, byte_size, schema_version, extractor_version,
                    parser_config_hash, legal_hold, status, created_at
                ) VALUES (%s, %s, %s, %s, 'text/plain', 1, '1.0', 'test', %s, %s, 'ready', {created_at})
                """,
                (
                    str(document_id),
                    str(tenant_id),
                    source_hash,
                    f"{document_id}.txt",
                    hashlib.sha256(b"retention-config").hexdigest(),
                    legal_hold,
                ),
            )


def _cleanup(admin_dsn: str, tenant_id: UUID) -> None:
    with psycopg.connect(admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM audit_events WHERE tenant_id = %s", (str(tenant_id),))
        cursor.execute("DELETE FROM outbox_events WHERE tenant_id = %s", (str(tenant_id),))
        cursor.execute("DELETE FROM retention_policies WHERE tenant_id = %s", (str(tenant_id),))
        cursor.execute("DELETE FROM documents WHERE tenant_id = %s", (str(tenant_id),))
        connection.commit()
