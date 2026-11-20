from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LEGACY_UNIVERSAL = ROOT / "outputs" / "universal_pageindex_ingest.py"
LEGACY_BUILDER = ROOT / "outputs" / "pageindex_first_builder.py"
RESEARCH_PDF = ROOT / "tests" / "fixtures" / "research-paper.pdf"
SCANNED_PDF = ROOT / "tests" / "fixtures" / "scanned-page.pdf"

pytestmark = pytest.mark.legacy


def test_legacy_help_contract() -> None:
    result = subprocess.run(
        [sys.executable, str(LEGACY_UNIVERSAL), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--input" in result.stdout
    assert "--output-dir" in result.stdout


@pytest.mark.slow
def test_legacy_native_pdf_emits_documented_artifacts(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(LEGACY_UNIVERSAL),
            "--input",
            str(RESEARCH_PDF),
            "--output-dir",
            str(tmp_path),
            "--query",
            "attention residual mechanism",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    manifest_path = tmp_path / "research-paper_universal_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert Path(manifest["builder_manifest"]["structure_path"]).exists()
    assert Path(manifest["builder_manifest"]["pages_path"]).exists()
    assert Path(manifest["builder_manifest"]["report_path"]).exists()


def test_legacy_scanned_pdf_must_not_report_completed(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(LEGACY_UNIVERSAL),
            "--input",
            str(SCANNED_PDF),
            "--output-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 10
    manifest = json.loads((tmp_path / "scanned-page_universal_manifest.json").read_text())
    assert manifest["status"] == "needs_ocr"
    assert manifest["queryable"] is False


def test_legacy_cache_must_not_cross_same_named_documents(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "document.pdf"
    second = second_dir / "document.pdf"
    shutil.copyfile(RESEARCH_PDF, first)
    shutil.copyfile(SCANNED_PDF, second)
    output = tmp_path / "output"

    first_run = subprocess.run(
        [sys.executable, str(LEGACY_UNIVERSAL), "--input", str(first), "--output-dir", str(output)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert first_run.returncode == 0, first_run.stderr
    first_manifest = json.loads((output / "document_universal_manifest.json").read_text())

    second_run = subprocess.run(
        [sys.executable, str(LEGACY_UNIVERSAL), "--input", str(second), "--output-dir", str(output)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert second_run.returncode == 10, second_run.stderr
    second_manifest = json.loads((output / "document_universal_manifest.json").read_text())

    assert first_manifest["source_sha256"] != second_manifest["source_sha256"]
    assert first_manifest["artifact_path"] != second_manifest["artifact_path"]


@pytest.mark.slow
def test_legacy_exact_reference_query_is_present_in_smoke_output(tmp_path: Path) -> None:
    text_path = tmp_path / "legal.txt"
    text_path.write_text(
        "ARTICLE 14\nEquality before law and equal protection of the laws.\f"
        "ARTICLE 15\nProhibition of discrimination on grounds of religion, race, caste, sex or place of birth.\f",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(LEGACY_BUILDER),
            "--pdf",
            str(RESEARCH_PDF),
            "--text-path",
            str(text_path),
            "--output-dir",
            str(tmp_path),
            "--numbered-label",
            "Article",
            "--min-leaf-nodes",
            "1",
            "--query",
            "Article 14 equality before law",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    smoke = (tmp_path / "research-paper_query_smoke.csv").read_text()
    assert "Article 14 equality before law" in smoke
    assert "Article 14" in smoke
