from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from proofcounsel_ingest.artifacts import ArtifactError, ArtifactWriter


def test_artifact_publication_is_atomic_and_reusable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with ArtifactWriter(tmp_path, "tenant/hash/version/config") as writer:
        assert not writer.reused
        writer.write_text("content/evidence.txt", "verified")
        final_path = writer.commit({"status": "ready"})

    assert (final_path / "content/evidence.txt").read_text() == "verified"
    manifest = json.loads((final_path / "artifact_manifest.json").read_text())
    assert manifest["artifact_key"] == "tenant/hash/version/config"

    with ArtifactWriter(tmp_path, "tenant/hash/version/config") as reused:
        assert reused.reused
        assert reused.commit({}) == final_path


def test_failed_writer_does_not_publish_partial_artifact(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with (
        pytest.raises(RuntimeError, match="parser failed"),
        ArtifactWriter(tmp_path, "tenant/hash/version/failure") as writer,
    ):
        writer.write_text("partial.txt", "not queryable")
        raise RuntimeError("parser failed")
    assert not (tmp_path / "documents/tenant/hash/version/failure").exists()
    assert not list((tmp_path / "documents").rglob(".staging-*"))


def test_concurrent_same_key_produces_one_immutable_artifact(tmp_path) -> None:  # type: ignore[no-untyped-def]
    def publish(value: str) -> tuple[bool, str]:
        with ArtifactWriter(tmp_path, "tenant/same/version/config", lock_timeout_seconds=5) as writer:
            if writer.reused:
                return True, (writer.final_path / "value.txt").read_text()
            writer.write_text("value.txt", value)
            writer.commit({"status": "ready"})
            return False, value

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, ["first", "second"]))

    assert sorted(reused for reused, _ in results) == [False, True]
    values = {value for _, value in results}
    assert len(values) == 1


def test_existing_artifact_checksum_is_verified(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with ArtifactWriter(tmp_path, "tenant/hash/version/tamper") as writer:
        writer.write_text("evidence.txt", "original")
        final_path = writer.commit({})
    (final_path / "evidence.txt").write_text("tampered")
    with pytest.raises(ArtifactError, match="checksum mismatch"), ArtifactWriter(
        tmp_path, "tenant/hash/version/tamper"
    ):
        pass
