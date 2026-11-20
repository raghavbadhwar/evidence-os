from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.models import IngestionStatus
from proofcounsel_ingest.orchestrator import ingest
from proofcounsel_ingest.retrieval.repository import SQLiteRetrievalRepository
from proofcounsel_ingest.retrieval.service import retrieve_document
from proofcounsel_ingest.telemetry import JsonlTelemetrySink

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "adversarial.html"
TENANT = UUID("00000000-0000-0000-0000-000000000504")


def settings_for(root: Path, tenant_id: UUID = TENANT) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        tenant_id=tenant_id,
        artifact_root=root,
        parser_config=ROOT / "config" / "parser-defaults.toml",
    )


@pytest.mark.integration
def test_ingest_query_reuses_persistent_index_and_repairs_deleted_index(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "artifacts")
    first = ingest(FIXTURE, settings=settings)
    assert first.status == IngestionStatus.READY
    repository = SQLiteRetrievalRepository(settings.artifact_root / "_indexes" / "retrieval.sqlite3")
    assert repository.index_build_count(tenant_id=TENANT, document_id=first.identity.document_id) == 1

    for _ in range(2):
        result, context = retrieve_document(
            "When are invoices due?",
            tenant_id=TENANT,
            document_id=first.identity.document_id,
            source_sha256=first.identity.source_sha256,
            settings=settings,
        )
        assert result.hits
        assert any("fifth day" in block.block.text for block in context.blocks)
    assert repository.index_build_count(tenant_id=TENANT, document_id=first.identity.document_id) == 1

    repository.delete_document(tenant_id=TENANT, document_id=first.identity.document_id)
    repaired = ingest(FIXTURE, settings=settings)
    assert repaired.reused and repaired.status == IngestionStatus.READY
    assert repository.index_build_count(tenant_id=TENANT, document_id=first.identity.document_id) == 2
    assert repository.has_index(
        tenant_id=TENANT,
        document_id=first.identity.document_id,
        source_sha256=first.identity.source_sha256,
        config_hash=first.run.parser_config_hash,
    )


@pytest.mark.integration
def test_retrieval_cannot_cross_tenant_boundary(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    first = ingest(FIXTURE, settings=settings_for(root, TENANT))
    other_tenant = UUID("00000000-0000-0000-0000-000000000505")
    other = ingest(FIXTURE, settings=settings_for(root, other_tenant))
    assert first.status == other.status == IngestionStatus.READY
    assert first.identity.document_id != other.identity.document_id

    with pytest.raises(ValueError, match="not indexed"):
        retrieve_document(
            "When are invoices due?",
            tenant_id=other_tenant,
            document_id=first.identity.document_id,
            source_sha256=first.identity.source_sha256,
            settings=settings_for(root, other_tenant),
        )


@pytest.mark.integration
def test_retrieval_telemetry_contains_counts_but_not_query_text(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    settings = settings_for(root)
    first = ingest(FIXTURE, settings=settings)
    sink = JsonlTelemetrySink(root / "telemetry.jsonl")
    retrieve_document(
        "When are invoices due?",
        tenant_id=TENANT,
        document_id=first.identity.document_id,
        source_sha256=first.identity.source_sha256,
        settings=settings,
        telemetry_sink=sink,
    )
    payload = json.loads((root / "telemetry.jsonl").read_text().splitlines()[0])
    assert payload["metadata"]["exact_count"] >= 0
    assert payload["metadata"]["query"] != "When are invoices due?"


@pytest.mark.integration
def test_semantic_descriptor_is_persisted_and_deterministic_routes_stay_model_free(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    settings = Settings(
        _env_file=None,
        environment="test",
        tenant_id=TENANT,
        artifact_root=root,
        parser_config=ROOT / "config" / "parser-defaults.toml",
        semantic_retrieval_enabled=True,
        semantic_index_model="embed-v1",
        semantic_index_dimensions=768,
        semantic_index_version="2026-01",
    )
    result = ingest(FIXTURE, settings=settings)
    assert result.status == IngestionStatus.READY
    assert result.artifact_path
    versions = json.loads((result.artifact_path / "index_versions.json").read_text())
    assert versions[0]["descriptor"]["semantic"] == {
        "model": "embed-v1",
        "dimensions": 768,
        "version": "2026-01",
    }
    parser_config = ParserConfig.load(ROOT / "config" / "parser-defaults.toml")
    parser_config = parser_config.model_copy(
        update={
            "retrieval": parser_config.retrieval.model_copy(update={"semantic_fallback_threshold": 0.0})
        }
    )
    retrieval, _context = retrieve_document(
        "When are invoices due?",
        tenant_id=TENANT,
        document_id=result.identity.document_id,
        source_sha256=result.identity.source_sha256,
        settings=settings,
        parser_config=parser_config,
    )
    assert retrieval.hits
    assert not retrieval.telemetry.semantic_attempted
