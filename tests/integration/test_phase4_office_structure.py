from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from proofcounsel_ingest.config import Settings
from proofcounsel_ingest.models import IngestionStatus
from proofcounsel_ingest.orchestrator import ingest
from proofcounsel_ingest.pageindex.adapter import search_tree, validate_pageindex_payload

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
TENANT = UUID("00000000-0000-0000-0000-000000000333")

pytestmark = pytest.mark.integration


def settings_for(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        tenant_id=TENANT,
        artifact_root=root,
        parser_config=ROOT / "config" / "parser-defaults.toml",
    )


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_docx_preserves_body_order_annotations_and_media(tmp_path: Path) -> None:
    result = ingest(FIXTURES / "structured-contract.docx", settings=settings_for(tmp_path / "artifacts"))
    assert result.status == IngestionStatus.READY
    assert result.artifact_path
    blocks = read_jsonl(result.artifact_path / "blocks.jsonl")
    texts = [block["text"] for block in blocks]
    alpha = next(index for index, text in enumerate(texts) if text.startswith("Alpha paragraph"))
    table = next(index for index, text in enumerate(texts) if text == "Service\tPrice")
    omega = next(index for index, text in enumerate(texts) if text.startswith("Omega paragraph"))

    assert alpha < table < omega
    assert any(block["metadata"].get("role") == "header" for block in blocks)
    assert any(block["metadata"].get("role") == "footer" for block in blocks)
    assert any("Reviewer comment" in block["text"] and block["block_type"] == "comment" for block in blocks)
    assert any("Footnote evidence" in block["text"] and block["block_type"] == "footnote" for block in blocks)
    assert any("Endnote evidence" in block["text"] and block["block_type"] == "footnote" for block in blocks)
    assert any("Text box evidence" in block["text"] and block["block_type"] == "note" for block in blocks)
    assert any(
        "Superseded tracked-change" in block["text"] and not block["metadata"]["queryable_evidence"]
        for block in blocks
    )
    assert any(block["metadata"].get("role") == "embedded_media" for block in blocks)
    assert list((result.artifact_path / "derived/embedded/docx").glob("*"))
    assert all(block["locator"]["format"] == "docx" for block in blocks)

    structure = json.loads((result.artifact_path / "structure.json").read_text())
    assert [node["title"] for node in structure] == ["Services Agreement", "Termination", "Data Protection"]
    assert all(node["verified"] for node in structure)


def test_xlsx_preserves_formulas_values_coordinates_hidden_state_and_assets(tmp_path: Path) -> None:
    result = ingest(FIXTURES / "structured-workbook.xlsx", settings=settings_for(tmp_path / "artifacts"))
    assert result.status == IngestionStatus.READY
    assert result.artifact_path
    blocks = read_jsonl(result.artifact_path / "blocks.jsonl")

    formula = next(block for block in blocks if "D2[formula]=B2*C2" in block["text"])
    budget = next(block for block in blocks if "B5=35000" in block["text"])
    assert formula["locator"]["part"] == "Pricing"
    assert "D2" in formula["locator"]["cell_range"]
    assert budget["locator"]["cell_range"].startswith("A5")
    assert any("Updated from INR 50,000" in block["text"] for block in blocks)
    assert any(block["metadata"].get("role") == "merged_range" for block in blocks)
    assert any(block["metadata"].get("sheet_state") == "hidden" for block in blocks)
    assert any(block["metadata"].get("role") == "chart" for block in blocks)
    assert any(block["metadata"].get("role") == "embedded_image" for block in blocks)
    assert list((result.artifact_path / "derived/embedded/xlsx").glob("*"))
    assert all(block["locator"]["format"] == "xlsx" for block in blocks)

    structure = json.loads((result.artifact_path / "structure.json").read_text())
    assert [node["title"] for node in structure] == ["Sheet: Pricing", "Sheet: Internal"]


def test_pptx_preserves_titles_tables_notes_shapes_and_images(tmp_path: Path) -> None:
    result = ingest(FIXTURES / "structured-deck.pptx", settings=settings_for(tmp_path / "artifacts"))
    assert result.status == IngestionStatus.READY
    assert result.artifact_path
    blocks = read_jsonl(result.artifact_path / "blocks.jsonl")

    assert any(block["text"] == "ProofCounsel Architecture" and block["block_type"] == "title" for block in blocks)
    assert any(block["text"] == "Reliability Gates" and block["block_type"] == "title" for block in blocks)
    assert any(block["text"] == "Citation precision\t99%" for block in blocks)
    assert any("Speaker note" in block["text"] and block["block_type"] == "note" for block in blocks)
    assert any(block["metadata"].get("role") == "picture" for block in blocks)
    assert list((result.artifact_path / "derived/embedded/pptx").glob("*"))
    assert all(block["locator"]["format"] == "pptx" for block in blocks)
    assert all(block["locator"]["slide"] >= 1 for block in blocks)

    structure = json.loads((result.artifact_path / "structure.json").read_text())
    assert [node["title"] for node in structure] == ["ProofCounsel Architecture", "Reliability Gates"]


@pytest.mark.slow
def test_research_pdf_uses_verified_bookmarks_without_false_math_or_year_nodes(tmp_path: Path) -> None:
    result = ingest(FIXTURES / "research-paper.pdf", settings=settings_for(tmp_path / "artifacts"))
    assert result.status == IngestionStatus.READY
    assert result.artifact_path
    structure = json.loads((result.artifact_path / "structure.json").read_text())
    titles = [node["title"] for node in structure]

    assert len(structure) >= 20
    assert "Introduction" in titles
    assert "Ablation Study" in titles
    assert "Related Work" in titles
    assert not any(title.isdigit() and len(title) == 4 for title in titles)
    assert not any("arxiv:" in title.casefold() for title in titles)
    assert not any(title.startswith(("ϕ", "M i", "Q j", "P l")) for title in titles)
    assert all(node["verified"] for node in structure)

    pageindex = json.loads((result.artifact_path / "pageindex.json").read_text())
    validate_pageindex_payload(pageindex)
    matches = search_tree("ablation study", pageindex, limit=3)
    assert matches and matches[0]["title"] == "Ablation Study"


def test_structured_text_formats_have_bounded_exact_locators(tmp_path: Path) -> None:
    json_path = tmp_path / "records.json"
    json_path.write_text('{\n  "budget": 35000,\n  "deadline": "2026-09-15"\n}\n', encoding="utf-8")
    csv_path = tmp_path / "clients.csv"
    csv_path.write_text("client,currency\nAcme,INR\n", encoding="utf-8")
    xml_path = tmp_path / "record.xml"
    xml_path.write_text("<record>\n  <deadline>2026-09-15</deadline>\n</record>\n", encoding="utf-8")

    for source in (json_path, csv_path, xml_path):
        result = ingest(source, settings=settings_for(tmp_path / f"artifacts-{source.suffix[1:]}"))
        assert result.status == IngestionStatus.READY
        assert result.artifact_path
        blocks = read_jsonl(result.artifact_path / "blocks.jsonl")
        assert blocks
        assert all(block["locator"]["line_start"] >= 1 for block in blocks)
        assert all(block["locator"]["line_end"] >= block["locator"]["line_start"] for block in blocks)
        assert all(len(block["text"]) <= 12_100 for block in blocks)


def test_tika_fallback_is_isolated_and_never_claims_exact_citations(tmp_path: Path) -> None:
    rtf_path = tmp_path / "legacy.rtf"
    rtf_path.write_text(
        r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Times New Roman;}}"
        r"\f0\fs24 ProofCounsel legacy contract.\par Filing deadline 15 September 2026.\par}",
        encoding="ascii",
    )
    result = ingest(rtf_path, settings=settings_for(tmp_path / "artifacts-rtf"))
    assert result.status == IngestionStatus.PARTIAL
    assert result.exit_code == 11
    assert result.quality and not result.quality.queryable
    assert result.artifact_path
    blocks = read_jsonl(result.artifact_path / "blocks.jsonl")
    assert blocks
    assert all(not block["metadata"]["queryable_evidence"] for block in blocks)
    assert all("locator_warning" in block["metadata"] for block in blocks)
    assert not (result.artifact_path / "pageindex.json").exists()
