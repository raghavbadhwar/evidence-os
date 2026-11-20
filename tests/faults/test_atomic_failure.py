from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest

from proofcounsel_ingest.artifacts import ArtifactError, ArtifactWriter
from proofcounsel_ingest.config import Settings
from proofcounsel_ingest.lifecycle import process_artifact_cleanup, revoke_and_delete_local
from proofcounsel_ingest.models import IngestionStatus
from proofcounsel_ingest.orchestrator import ingest
from proofcounsel_ingest.outbox import SQLiteOutbox
from proofcounsel_ingest.retrieval.repository import SQLiteRetrievalRepository

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "adversarial.html"
TENANT = UUID("00000000-0000-0000-0000-000000001121")


def test_publication_failure_revokes_staged_index_and_removes_staging(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fail_commit(self, metadata):  # type: ignore[no-untyped-def]
        del self, metadata
        raise ArtifactError("controlled publication failure")

    monkeypatch.setattr(ArtifactWriter, "commit", fail_commit)
    settings = Settings(
        environment="test",
        tenant_id=TENANT,
        artifact_root=tmp_path / "artifacts",
        parser_config=ROOT / "config" / "parser-defaults.toml",
    )
    result = ingest(FIXTURE, settings=settings)

    assert result.status == IngestionStatus.FAILED
    repository = SQLiteRetrievalRepository(settings.artifact_root / "_indexes" / "retrieval.sqlite3")
    assert not repository.has_index(
        tenant_id=TENANT,
        document_id=result.identity.document_id,
        source_sha256=result.identity.source_sha256,
        config_hash=result.run.parser_config_hash,
    )
    assert not list((settings.artifact_root / "documents").rglob(".staging-*"))
    assert not list((settings.artifact_root / "documents").rglob("artifact_manifest.json"))


@pytest.mark.integration
def test_database_failure_does_not_leave_a_queryable_filesystem_artifact(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        tenant_id=TENANT,
        artifact_root=tmp_path / "artifacts",
        database_url="postgresql://127.0.0.1:1/proofcounsel?connect_timeout=1",
        parser_config=ROOT / "config" / "parser-defaults.toml",
    )
    result = ingest(FIXTURE, settings=settings)
    assert result.status == IngestionStatus.FAILED
    assert not list((settings.artifact_root / "documents").rglob("artifact_manifest.json"))


def test_concurrent_deletion_is_idempotent_and_cleanup_is_verified(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "documents" / "tenant" / "document"
    artifact.mkdir(parents=True)
    (artifact / "marker").write_text("evidence")
    repository = SQLiteRetrievalRepository(root / "_indexes" / "retrieval.sqlite3")
    outbox = SQLiteOutbox(root / "_indexes" / "outbox.sqlite3", TENANT)
    document_id = UUID("00000000-0000-0000-0000-000000001122")

    def delete_once(_index: int):  # type: ignore[no-untyped-def]
        return revoke_and_delete_local(
            repository=repository,
            tenant_id=TENANT,
            document_id=document_id,
            artifact_path=artifact,
            artifact_root=root,
            outbox=outbox,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(delete_once, range(4)))
    assert all(result.query_access_revoked and result.cleanup_enqueued for result in results)

    claimed = outbox.claim("cleanup-worker")
    assert claimed
    process_artifact_cleanup(claimed, artifact_root=root)
    outbox.complete(claimed.event_id, worker_id="cleanup-worker")
    assert not artifact.exists()
    assert outbox.claim("second-cleanup-worker") is None
