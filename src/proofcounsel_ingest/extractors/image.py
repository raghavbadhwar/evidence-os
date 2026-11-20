"""Confidence-scored image OCR with pixel-exact line locators."""

from __future__ import annotations

import csv
import hashlib
import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from proofcounsel_ingest.extractors.base import (
    EvidenceVariant,
    ExtractionContext,
    ExtractionResult,
)
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    PageRecord,
    SourceIdentity,
    SourceLocator,
)
from proofcounsel_ingest.security.sandbox import run_sandboxed
from proofcounsel_ingest.telemetry import NullTelemetrySink, timed_stage


@dataclass
class OcrLine:
    words: list[str] = field(default_factory=list)
    weighted_confidence: float = 0.0
    weight: int = 0
    left: int = 2**31 - 1
    top: int = 2**31 - 1
    right: int = 0
    bottom: int = 0

    def add(self, text: str, confidence: float, left: int, top: int, width: int, height: int) -> None:
        self.words.append(text)
        word_weight = max(len(text), 1)
        self.weighted_confidence += confidence * word_weight
        self.weight += word_weight
        self.left = min(self.left, left)
        self.top = min(self.top, top)
        self.right = max(self.right, left + width)
        self.bottom = max(self.bottom, top + height)

    @property
    def confidence(self) -> float:
        return max(0.0, min(1.0, self.weighted_confidence / max(self.weight, 1) / 100.0))


class ImageExtractor:
    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult:
        tesseract = context.settings.resolved_binary("tesseract_binary", "tesseract")
        if not context.settings.ocr_enabled or not tesseract:
            return _needs_ocr(identity, "image OCR is disabled or Tesseract is unavailable")

        with tempfile.TemporaryDirectory(prefix="proofcounsel-image-ocr-") as temporary:
            normalized_path = Path(temporary) / "normalized.png"
            with Image.open(source) as image:
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                width, height = normalized.size
                normalized.save(normalized_path, format="PNG")
            sink = context.telemetry_sink or NullTelemetrySink()
            with timed_stage(
                sink,
                tenant_id=identity.tenant_id,
                document_id=identity.document_id,
                source_sha256=identity.source_sha256,
                stage="ocr",
                metadata={"backend": "tesseract", "page_count": 1},
            ):
                result = run_sandboxed(
                    [
                        tesseract,
                        str(normalized_path),
                        "stdout",
                        "-l",
                        context.settings.ocr_languages,
                        "tsv",
                    ],
                    cwd=Path(temporary),
                    profile=context.sandbox_profile,
                )
        lines = _parse_tesseract_lines(result.stdout)
        blocks: list[ContentBlock] = []
        for line in lines:
            text = " ".join(line.words).strip()
            if not text or line.right <= line.left or line.bottom <= line.top:
                continue
            blocks.append(
                ContentBlock(
                    document_id=identity.document_id,
                    ordinal=len(blocks),
                    block_type=BlockType.PARAGRAPH,
                    text=text,
                    language=context.settings.ocr_languages,
                    confidence=line.confidence,
                    locator=SourceLocator(
                        format=DocumentFormat.IMAGE,
                        page=1,
                        bbox=(float(line.left), float(line.top), float(line.right), float(line.bottom)),
                    ),
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    metadata={
                        "untrusted_evidence": True,
                        "queryable_evidence": True,
                        "coordinate_system": "image_pixels",
                        "extraction_method": "tesseract_tsv",
                        "image_width": width,
                        "image_height": height,
                    },
                )
            )
        page_text = "\n".join(block.text for block in blocks)
        page_confidence = _weighted_block_confidence(blocks)
        accepted = bool(blocks) and page_confidence >= context.parser_config.quality.minimum_ocr_confidence
        blocks = [
            block.model_copy(
                update={"metadata": {**block.metadata, "queryable_evidence": accepted}}
            )
            for block in blocks
        ]
        return ExtractionResult(
            format=DocumentFormat.IMAGE,
            blocks=blocks,
            pages=[
                PageRecord(
                    document_id=identity.document_id,
                    page_number=1,
                    native_text_coverage=0,
                    ocr_used=True,
                    ocr_confidence=page_confidence,
                    warnings=[] if accepted else ["image OCR evidence below acceptance threshold"],
                    quality_metrics={
                        "ocr_confidence": page_confidence,
                        "locator_coverage": 1.0 if blocks else 0.0,
                        "language_supported": 1.0,
                        "accepted": 1.0 if accepted else 0.0,
                    },
                )
            ],
            expected_units=1,
            extracted_units=1 if blocks else 0,
            ocr_required_units=[] if accepted else [1],
            warnings=[] if accepted else ["image OCR requires review"],
            metadata={
                "image_width": width,
                "image_height": height,
                "extraction_method": "tesseract_tsv",
                "quality_profile": "image",
            },
            evidence_variants=[
                EvidenceVariant(
                    unit=1,
                    method="tesseract_tsv",
                    text=page_text,
                    text_sha256=hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
                    confidence=page_confidence,
                    selected=accepted,
                )
            ],
        )


def _needs_ocr(identity: SourceIdentity, reason: str) -> ExtractionResult:
    return ExtractionResult(
        format=DocumentFormat.IMAGE,
        blocks=[],
        pages=[
            PageRecord(
                document_id=identity.document_id,
                page_number=1,
                native_text_coverage=0,
                warnings=[reason],
            )
        ],
        expected_units=1,
        extracted_units=0,
        ocr_required_units=[1],
        warnings=[reason],
    )


def _parse_tesseract_lines(tsv: str) -> list[OcrLine]:
    lines: dict[tuple[int, int, int, int], OcrLine] = {}
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf") or -1)
            left = int(row.get("left") or 0)
            top = int(row.get("top") or 0)
            width = int(row.get("width") or 0)
            height = int(row.get("height") or 0)
            key = (
                int(row.get("page_num") or 1),
                int(row.get("block_num") or 0),
                int(row.get("par_num") or 0),
                int(row.get("line_num") or 0),
            )
        except ValueError:
            continue
        if confidence < 0 or width <= 0 or height <= 0:
            continue
        lines.setdefault(key, OcrLine()).add(text, confidence, left, top, width, height)
    return list(lines.values())


def _weighted_block_confidence(blocks: list[ContentBlock]) -> float:
    total = sum(len(block.text) for block in blocks)
    if total == 0:
        return 0.0
    return sum(block.confidence * len(block.text) for block in blocks) / total
