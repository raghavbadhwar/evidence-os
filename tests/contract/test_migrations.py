from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_migrations_are_ordered_and_cover_production_contracts() -> None:
    migrations = sorted((ROOT / "migrations").glob("*.sql"))
    assert [path.name for path in migrations] == [
        "0001_documents_and_runs.sql",
        "0002_pages_blocks_assets.sql",
        "0003_structure_and_indexes.sql",
        "0004_retrieval_and_citations.sql",
        "0005_acl_retention_audit.sql",
        "0006_rls.sql",
        "0007_tenant_scoped_foreign_keys.sql",
        "0008_outbox_leases.sql",
        "0009_rls_hardening.sql",
    ]
    sql = "\n".join(path.read_text(encoding="utf-8") for path in migrations)
    for table in (
        "documents",
        "ingestion_runs",
        "document_pages",
        "content_blocks",
        "structure_nodes",
        "index_versions",
        "retrieval_events",
        "verified_citations",
        "document_acl",
        "outbox_events",
        "audit_events",
        "jobs",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "app.current_tenant_id" in sql
    assert "app.current_matter_id" in sql
    assert "jobs_service_policy" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "content_blocks_parent_tenant_fk" in sql
    assert "verified_citations_block_tenant_fk" in sql
    assert "ALTER POLICY documents_acl_policy" in sql
    assert "status <> 'deleted'" in sql
    assert "outbox_events_tenant_idempotency_uq" in sql
