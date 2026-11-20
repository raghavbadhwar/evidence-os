from __future__ import annotations

import zipfile
from pathlib import Path

from PIL import Image

from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.security.file_validation import detect_mime, validate_upload

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
LIMITS = ParserConfig.load(Settings(_env_file=None).parser_config).limits


def test_detects_supported_signatures() -> None:
    assert detect_mime(FIXTURES / "research-paper.pdf") == "application/pdf"
    assert detect_mime(FIXTURES / "structured-contract.docx").endswith("wordprocessingml.document")
    assert detect_mime(FIXTURES / "structured-workbook.xlsx").endswith("spreadsheetml.sheet")
    assert detect_mime(FIXTURES / "structured-deck.pptx").endswith("presentationml.presentation")


def test_pdf_page_limit_is_enforced_before_parsing() -> None:
    limited = LIMITS.model_copy(update={"max_pages": 1})
    result = validate_upload(FIXTURES / "research-paper.pdf", limited)
    assert not result.accepted
    assert result.metadata["pdf_pages"] == 21
    assert "max_pages=1" in " ".join(result.reasons)


def test_extension_signature_mismatch_is_quarantined(tmp_path: Path) -> None:
    disguised = tmp_path / "evidence.txt"
    disguised.write_bytes((FIXTURES / "research-paper.pdf").read_bytes())
    result = validate_upload(disguised, LIMITS)
    assert not result.accepted
    assert "extension/signature mismatch" in " ".join(result.reasons)


def test_ooxml_path_traversal_is_rejected(tmp_path: Path) -> None:
    malicious = tmp_path / "malicious.docx"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("word/document.xml", "<document />")
        archive.writestr("../outside.txt", "escape")
    result = validate_upload(malicious, LIMITS)
    assert not result.accepted
    assert "unsafe path" in " ".join(result.reasons)


def test_malformed_ooxml_archive_is_quarantined(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.docx"
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("mimetype", bytes((255, 254)))
        archive.writestr("word/document.xml", "<document />")
    result = validate_upload(malformed, LIMITS)
    assert not result.accepted
    assert result.detected_mime in {"application/zip", "application/octet-stream"}


def test_ooxml_archive_entry_bomb_is_rejected_before_extraction(tmp_path: Path) -> None:
    bomb = tmp_path / "entry-bomb.docx"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<document />")
        archive.writestr("word/part-1.xml", "<part />")
        archive.writestr("word/part-2.xml", "<part />")
    limited = LIMITS.model_copy(update={"max_archive_entries": 2})

    result = validate_upload(bomb, limited)

    assert not result.accepted
    assert result.metadata["archive_entries"] == 3
    assert "max_archive_entries=2" in " ".join(result.reasons)


def test_ooxml_uncompressed_size_bomb_is_rejected_before_extraction(tmp_path: Path) -> None:
    bomb = tmp_path / "size-bomb.docx"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 512)
    limited = LIMITS.model_copy(update={"max_uncompressed_bytes": 128})

    result = validate_upload(bomb, limited)

    assert not result.accepted
    assert int(result.metadata["archive_uncompressed_bytes"]) > 128
    assert "max_uncompressed_bytes=128" in " ".join(result.reasons)


def test_image_dimensions_are_recorded(tmp_path: Path) -> None:
    image_path = tmp_path / "sample-notice.png"
    Image.new("RGB", (1200, 600), "white").save(image_path)
    result = validate_upload(image_path, LIMITS)
    assert result.accepted
    assert result.metadata["image_width"] == 1200
    assert result.metadata["image_height"] == 600


def test_image_decompression_limit_is_enforced_before_ocr(tmp_path: Path) -> None:
    image_path = tmp_path / "oversized-notice.png"
    Image.new("RGB", (20, 20), "white").save(image_path)
    limited = LIMITS.model_copy(update={"max_image_pixels": 100})

    result = validate_upload(image_path, limited)

    assert not result.accepted
    assert result.metadata["image_pixels"] == 400
    assert "max_image_pixels=100" in " ".join(result.reasons)
