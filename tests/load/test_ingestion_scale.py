from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from pypdf import PdfWriter

from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.models import IngestionStatus
from proofcounsel_ingest.orchestrator import ingest

ROOT = Path(__file__).resolve().parents[2]
TENANT = UUID("00000000-0000-0000-0000-000000001123")


@pytest.mark.load
def test_thousand_page_document_is_rejected_before_parser_when_over_limit(tmp_path: Path) -> None:
    source = tmp_path / "thousand-and-one-pages.pdf"
    writer = PdfWriter()
    for _ in range(1001):
        writer.add_blank_page(width=612, height=792)
    with source.open("wb") as handle:
        writer.write(handle)

    base_config = ParserConfig.load(ROOT / "config" / "parser-defaults.toml")
    parser_config = base_config.model_copy(
        update={"limits": base_config.limits.model_copy(update={"max_pages": 1000})}
    )
    settings = Settings(
        _env_file=None,
        environment="test",
        tenant_id=TENANT,
        artifact_root=tmp_path / "artifacts",
        parser_config=ROOT / "config" / "parser-defaults.toml",
    )

    result = ingest(source, settings=settings, parser_config=parser_config)

    assert result.status == IngestionStatus.QUARANTINED
    assert any("max_pages=1000" in reason for reason in result.validation.reasons)
    assert not (settings.artifact_root / "documents").exists()
