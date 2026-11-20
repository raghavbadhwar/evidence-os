from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from proofcounsel_ingest.config import Settings
from proofcounsel_ingest.extractors.router import ExtractorRouter
from proofcounsel_ingest.models import IngestionStatus
from proofcounsel_ingest.orchestrator import ingest
from proofcounsel_ingest.telemetry import JsonlTelemetrySink

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
TENANT = UUID("00000000-0000-0000-0000-000000000111")


def settings_for(root: Path, *, environment: str = "test") -> Settings:
    return Settings(
        _env_file=None,
        environment=environment,
        tenant_id=TENANT,
        artifact_root=root,
        parser_config=ROOT / "config" / "parser-defaults.toml",
    )


def test_visible_html_becomes_queryable_and_reuses_immutable_artifact(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "artifacts")
    first = ingest(FIXTURES / "adversarial.html", settings=settings)
    second = ingest(FIXTURES / "adversarial.html", settings=settings)

    assert first.status == IngestionStatus.READY
    assert first.quality and first.quality.queryable
    assert first.artifact_path and (first.artifact_path / "ingestion_manifest.json").exists()
    assert second.status == IngestionStatus.READY
    assert second.reused
    assert first.artifact_path == second.artifact_path
    assert first.run.run_id != second.run.run_id
    assert len(list((settings.artifact_root / "_runs" / str(TENANT)).glob("*.json"))) == 2


@pytest.mark.integration
def test_scanned_pdf_stops_at_needs_ocr_without_queryable_structure(tmp_path: Path) -> None:
    result = ingest(FIXTURES / "scanned-page.pdf", settings=settings_for(tmp_path / "artifacts"))
    assert result.status == IngestionStatus.NEEDS_OCR
    assert result.exit_code == 10
    assert result.quality and not result.quality.queryable
    assert result.artifact_path
    assert (result.artifact_path / "structure.json").read_text().strip() == "[]"
    assert not (result.artifact_path / "indexes/lexical.json").exists()


@pytest.mark.integration
def test_same_filename_different_content_uses_different_artifact_keys(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "document.pdf"
    second = second_dir / "document.pdf"
    shutil.copyfile(FIXTURES / "research-paper.pdf", first)
    shutil.copyfile(FIXTURES / "scanned-page.pdf", second)
    settings = settings_for(tmp_path / "artifacts")

    first_result = ingest(first, settings=settings)
    second_result = ingest(second, settings=settings)

    assert first_result.identity.source_sha256 != second_result.identity.source_sha256
    assert first_result.artifact_key != second_result.artifact_key
    assert first_result.artifact_path != second_result.artifact_path


def test_production_rejects_unconfigured_malware_scanner(tmp_path: Path) -> None:
    result = ingest(
        FIXTURES / "adversarial.html",
        settings=settings_for(tmp_path / "artifacts", environment="production"),
    )
    assert result.status == IngestionStatus.QUARANTINED
    assert "configured malware scanner" in " ".join(result.warnings)
    assert result.artifact_path is None


def test_extractor_crash_does_not_publish_partial_artifact(tmp_path: Path) -> None:
    class FailingExtractor:
        def extract(self, source, identity, context):  # type: ignore[no-untyped-def]
            del source, identity, context
            raise RuntimeError("controlled extractor failure")

    router = ExtractorRouter()
    router.register("text/html", FailingExtractor())
    settings = settings_for(tmp_path / "artifacts")
    result = ingest(FIXTURES / "adversarial.html", settings=settings, router=router)

    assert result.status == IngestionStatus.FAILED
    assert result.artifact_path is None
    assert not list((settings.artifact_root / "documents").rglob("artifact_manifest.json"))
    assert not list((settings.artifact_root / "documents").rglob(".staging-*"))


def test_ingestion_emits_redacted_stage_telemetry(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "artifacts")
    sink = JsonlTelemetrySink(settings.artifact_root / "telemetry.jsonl")
    result = ingest(FIXTURES / "adversarial.html", settings=settings, telemetry_sink=sink)
    assert result.status == IngestionStatus.READY
    events = [json.loads(line) for line in (settings.artifact_root / "telemetry.jsonl").read_text().splitlines()]
    stages = {event["stage"] for event in events}
    assert {"validation", "malware_scan", "extraction", "assets", "structure", "indexing", "publication"} <= stages
    assert all("Invoices are due" not in json.dumps(event) for event in events)
