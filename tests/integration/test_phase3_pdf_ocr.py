from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter

from proofcounsel_ingest.config import Settings
from proofcounsel_ingest.models import IngestionStatus
from proofcounsel_ingest.orchestrator import ingest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
TENANT = UUID("00000000-0000-0000-0000-000000000222")

pytestmark = pytest.mark.integration


def settings_for(root: Path, *, ocr: bool) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        tenant_id=TENANT,
        artifact_root=root,
        parser_config=ROOT / "config" / "parser-defaults.toml",
        ocr_enabled=ocr,
    )


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.slow
def test_native_pdf_uses_poppler_boxes_without_ocr(tmp_path: Path) -> None:
    result = ingest(FIXTURES / "research-paper.pdf", settings=settings_for(tmp_path / "artifacts", ocr=False))
    assert result.status == IngestionStatus.READY
    assert result.artifact_path
    blocks = read_jsonl(result.artifact_path / "blocks.jsonl")
    pages = json.loads((result.artifact_path / "pages.json").read_text())
    assets = json.loads((result.artifact_path / "assets/manifest.json").read_text())

    assert len(pages) == 21
    assert all(not page["ocr_used"] for page in pages)
    assert len(blocks) > 100
    assert all(block["locator"]["page"] >= 1 for block in blocks)
    assert all(block["locator"]["bbox"][2] > block["locator"]["bbox"][0] for block in blocks)
    assert any((block["locator"]["bbox"][3] - block["locator"]["bbox"][1]) < 100 for block in blocks)
    assert len([asset for asset in assets if asset["tier"] == "low"]) == 21
    assert not (result.artifact_path / "derived/ocr/searchable.pdf").exists()


@pytest.mark.slow
def test_scanned_pdf_selective_ocr_produces_confident_located_evidence(tmp_path: Path) -> None:
    result = ingest(FIXTURES / "scanned-page.pdf", settings=settings_for(tmp_path / "artifacts", ocr=True))
    assert result.status == IngestionStatus.READY
    assert result.quality and result.quality.queryable
    assert result.artifact_path
    pages = json.loads((result.artifact_path / "pages.json").read_text())
    variants = read_jsonl(result.artifact_path / "evidence_variants.jsonl")
    assets = json.loads((result.artifact_path / "assets/manifest.json").read_text())

    assert pages[0]["ocr_used"]
    assert pages[0]["ocr_confidence"] >= 0.70
    assert {variant["method"] for variant in variants} == {"native", "ocrmypdf_tesseract"}
    assert any(variant["selected"] and variant["method"] == "ocrmypdf_tesseract" for variant in variants)
    assert (result.artifact_path / "derived/ocr/searchable.pdf").stat().st_size > 0
    assert {asset["tier"] for asset in assets} == {"low", "high"}


@pytest.mark.slow
def test_mixed_pdf_ocrs_only_deficient_page(tmp_path: Path) -> None:
    mixed_pdf = tmp_path / "mixed.pdf"
    writer = PdfWriter()
    writer.add_page(PdfReader(FIXTURES / "research-paper.pdf").pages[0])
    writer.add_page(PdfReader(FIXTURES / "scanned-page.pdf").pages[0])
    with mixed_pdf.open("wb") as handle:
        writer.write(handle)

    result = ingest(mixed_pdf, settings=settings_for(tmp_path / "artifacts", ocr=True))
    assert result.status == IngestionStatus.READY
    assert result.artifact_path
    pages = json.loads((result.artifact_path / "pages.json").read_text())
    variants = read_jsonl(result.artifact_path / "evidence_variants.jsonl")

    assert len(pages) == 2
    assert pages[0]["ocr_used"] is False
    assert pages[1]["ocr_used"] is True
    assert any(variant["unit"] == 1 and variant["method"] == "native" and variant["selected"] for variant in variants)
    assert any(
        variant["unit"] == 2 and variant["method"] == "ocrmypdf_tesseract" and variant["selected"]
        for variant in variants
    )


@pytest.mark.slow
def test_standalone_image_uses_tesseract_boxes_and_confidence(tmp_path: Path) -> None:
    image_path = tmp_path / "notice.png"
    image = Image.new("RGB", (1600, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 56)
    draw.text((60, 80), "Filing deadline: 15 September 2026", font=font, fill="black")
    draw.text((60, 190), "Matter reference: PC-2026-042", font=font, fill="black")
    image.save(image_path)

    result = ingest(image_path, settings=settings_for(tmp_path / "artifacts", ocr=True))
    assert result.status == IngestionStatus.READY
    assert result.artifact_path
    blocks = read_jsonl(result.artifact_path / "blocks.jsonl")
    pages = json.loads((result.artifact_path / "pages.json").read_text())

    assert pages[0]["ocr_confidence"] >= 0.90
    assert any("15 September 2026" in block["text"] for block in blocks)
    assert any("PC-2026-042" in block["text"] for block in blocks)
    assert all(block["locator"]["format"] == "image" for block in blocks)
    assert all(block["metadata"]["coordinate_system"] == "image_pixels" for block in blocks)

