from __future__ import annotations

from pathlib import Path
from uuid import UUID

from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.extractors.base import ExtractionContext
from proofcounsel_ingest.extractors.html import HtmlExtractor
from proofcounsel_ingest.identity import compute_source_identity
from proofcounsel_ingest.security.sandbox import SandboxProfile

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "tests" / "fixtures" / "adversarial.html"


def test_active_and_hidden_html_is_excluded() -> None:
    settings = Settings(_env_file=None)
    config = ParserConfig.load(settings.parser_config)
    identity = compute_source_identity(
        HTML,
        UUID("00000000-0000-0000-0000-000000000001"),
        "text/html",
    )
    result = HtmlExtractor().extract(
        HTML,
        identity,
        ExtractionContext(settings=settings, parser_config=config, sandbox_profile=SandboxProfile()),
    )
    text = "\n".join(block.text for block in result.blocks)
    assert "Payment Terms" in text
    assert "Invoices are due" in text
    assert "Currency\tINR" in text
    assert "EXFILTRATE" not in text
    assert "Hidden override" not in text
    assert "Invisible instruction" not in text
    assert "CSS-hidden instruction" not in text
    assert all(block.metadata["untrusted_evidence"] for block in result.blocks)
    assert all(block.locator.dom_path for block in result.blocks)

