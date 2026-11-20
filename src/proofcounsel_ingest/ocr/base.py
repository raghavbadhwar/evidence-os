from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from proofcounsel_ingest.extractors.base import ExtractionContext


class OcrResult(BaseModel):
    output_pdf: Path
    processed_pages: list[int]
    page_confidence: dict[int, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class OcrPort(Protocol):
    def process_pdf(
        self,
        source: Path,
        pages: list[int],
        output_pdf: Path,
        context: ExtractionContext,
    ) -> OcrResult: ...

