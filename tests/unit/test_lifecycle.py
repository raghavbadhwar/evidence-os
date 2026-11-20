from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest

from proofcounsel_ingest.lifecycle import (
    LifecyclePolicy,
    export_local_artifact,
    process_artifact_cleanup,
    revoke_and_delete_local,
)
from proofcounsel_ingest.outbox import SQLiteOutbox
from proofcounsel_ingest.retrieval.repository import SQLiteRetrievalRepository

TENANT = UUID("00000000-0000-0000-0000-000000001003")


def test_legal_hold_revokes_query_access_but_retains_artifact(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "documents" / "tenant" / "document"
    artifact.mkdir(parents=True)
    (artifact / "marker").write_text("evidence")
    repository = SQLiteRetrievalRepository(root / "_indexes" / "retrieval.sqlite3")
    result = revoke_and_delete_local(
        repository=repository,
        tenant_id=TENANT,
        document_id=uuid4(),
        artifact_path=artifact,
        artifact_root=root,
        policy=LifecyclePolicy(legal_hold=True),
    )
    assert result.query_access_revoked
    assert not result.artifacts_deleted
    assert artifact.exists()


def test_local_delete_refuses_paths_outside_artifact_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    repository = SQLiteRetrievalRepository(root / "_indexes" / "retrieval.sqlite3")
    with pytest.raises(ValueError, match="invalid artifact path"):
        revoke_and_delete_local(
            repository=repository,
            tenant_id=TENANT,
            document_id=uuid4(),
            artifact_path=outside,
            artifact_root=root,
        )


def test_delete_revokes_first_and_enqueues_idempotent_artifact_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "documents" / "tenant" / "document"
    artifact.mkdir(parents=True)
    (artifact / "marker").write_text("evidence")
    repository = SQLiteRetrievalRepository(root / "_indexes" / "retrieval.sqlite3")
    outbox = SQLiteOutbox(root / "_indexes" / "outbox.sqlite3", TENANT)
    document_id = uuid4()

    result = revoke_and_delete_local(
        repository=repository,
        tenant_id=TENANT,
        document_id=document_id,
        artifact_path=artifact,
        artifact_root=root,
        outbox=outbox,
    )
    assert result.query_access_revoked
    assert result.cleanup_enqueued
    assert not result.artifacts_deleted
    assert artifact.exists()

    claimed = outbox.claim("cleanup-worker")
    assert claimed
    process_artifact_cleanup(claimed, artifact_root=root)
    outbox.complete(claimed.event_id, worker_id="cleanup-worker")
    assert not artifact.exists()

    reused = outbox.enqueue(claimed)
    assert reused.event_id == claimed.event_id
    assert reused.status == "completed"


def test_export_contains_hash_manifest_and_refuses_traversal(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "documents" / "tenant" / "document"
    artifact.mkdir(parents=True)
    (artifact / "ingestion_manifest.json").write_text('{"status":"ready"}\n')
    (artifact / "nested").mkdir()
    (artifact / "nested" / "blocks.jsonl").write_text('{"text":"evidence"}\n')
    document_id = uuid4()
    result = export_local_artifact(
        document_id=document_id,
        artifact_path=artifact,
        artifact_root=root,
        destination=tmp_path / "exports" / "document.zip",
    )
    assert result.file_count == 2
    with ZipFile(result.destination) as archive:
        names = set(archive.namelist())
        assert names == {"ingestion_manifest.json", "nested/blocks.jsonl", "export_manifest.json"}
        manifest = archive.read("export_manifest.json").decode()
        assert str(document_id) in manifest

    with pytest.raises(ValueError, match="invalid artifact path"):
        export_local_artifact(
            document_id=document_id,
            artifact_path=tmp_path / "outside",
            artifact_root=root,
            destination=tmp_path / "outside.zip",
        )
