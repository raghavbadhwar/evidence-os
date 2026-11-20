"""Layout-preserving PDF extraction with selective, confidence-scored OCR."""

from __future__ import annotations

import hashlib
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

from lxml import etree
from pypdf import PdfReader

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
from proofcounsel_ingest.ocr.ocrmypdf import OcrMyPdfAdapter
from proofcounsel_ingest.security.sandbox import run_sandboxed
from proofcounsel_ingest.telemetry import NullTelemetrySink, timed_stage

PAGES_PATTERN = re.compile(r"^Pages:\s+(\d+)\s*$", re.MULTILINE)
PAGE_TAG = "{http://www.w3.org/1999/xhtml}page"
BLOCK_TAG = "{http://www.w3.org/1999/xhtml}block"
LINE_TAG = "{http://www.w3.org/1999/xhtml}line"
WORD_TAG = "{http://www.w3.org/1999/xhtml}word"


class PdfNativeExtractor:
    def __init__(self, ocr_adapter: OcrMyPdfAdapter | None = None) -> None:
        self.ocr_adapter = ocr_adapter or OcrMyPdfAdapter()

    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult:
        native = _extract_bbox_document(source, identity, context, method="native")
        pdf_metadata = _pdf_metadata(source)
        native.metadata.update(pdf_metadata)
        _append_figure_placeholders(native, identity, pdf_metadata)
        if not native.ocr_required_units or not context.settings.ocr_enabled:
            return native
        if not self.ocr_adapter.available(context):
            native.warnings.append("OCR is enabled but no OCR backend is available")
            return native

        if context.work_dir is None:
            raise RuntimeError("selective OCR requires an extraction work directory")
        ocr_pdf = context.work_dir / "ocr" / "searchable.pdf"
        sink = context.telemetry_sink or NullTelemetrySink()
        with timed_stage(
            sink,
            tenant_id=identity.tenant_id,
            document_id=identity.document_id,
            source_sha256=identity.source_sha256,
            stage="ocr",
            metadata={"requested_pages": len(native.ocr_required_units), "backend": "ocrmypdf"},
        ):
            ocr_result = self.ocr_adapter.process_pdf(
                source,
                native.ocr_required_units,
                ocr_pdf,
                context,
            )
        ocr_extraction = _extract_bbox_document(ocr_pdf, identity, context, method="ocrmypdf_tesseract")
        merged = _merge_native_and_ocr(native, ocr_extraction, ocr_result.page_confidence, context)
        merged.metadata.update(pdf_metadata)
        _append_figure_placeholders(merged, identity, pdf_metadata)
        merged.metadata["ocr_processed_pages"] = ocr_result.processed_pages
        merged.warnings.extend(ocr_result.warnings)
        merged.derived_artifacts["ocr/searchable.pdf"] = ocr_pdf
        return merged


def _extract_bbox_document(
    source: Path,
    identity: SourceIdentity,
    context: ExtractionContext,
    *,
    method: str,
) -> ExtractionResult:
    pdftotext = context.settings.resolved_binary("pdftotext_binary", "pdftotext")
    pdfinfo = context.settings.resolved_binary("pdfinfo_binary", "pdfinfo")
    if not pdftotext or not pdfinfo:
        raise RuntimeError("PDF extraction requires pdftotext and pdfinfo")

    info_result = run_sandboxed(
        [pdfinfo, str(source.resolve())],
        cwd=source.parent,
        profile=context.sandbox_profile,
    )
    page_count = _parse_page_count(info_result.stdout)
    with tempfile.TemporaryDirectory(prefix="proofcounsel-bbox-") as temporary:
        temporary_path = Path(temporary)
        bbox_path = temporary_path / "layout.xhtml"
        run_sandboxed(
            [pdftotext, "-bbox-layout", str(source.resolve()), str(bbox_path)],
            cwd=temporary_path,
            profile=context.sandbox_profile,
        )
        blocks, page_records, variants, parsed_pages = _parse_bbox_layout(
            bbox_path,
            identity,
            context,
            method=method,
        )
    page_count = max(page_count, parsed_pages)
    page_by_number = {page.page_number: page for page in page_records}
    for page_number in range(1, page_count + 1):
        if page_number not in page_by_number:
            page_by_number[page_number] = PageRecord(
                document_id=identity.document_id,
                page_number=page_number,
                native_text_coverage=0,
                warnings=["page missing from Poppler layout output"],
            )
    page_records = [page_by_number[page] for page in sorted(page_by_number)]
    minimum_chars = context.parser_config.quality.minimum_native_chars_per_page
    ocr_required = [
        page.page_number
        for page in page_records
        if page.quality_metrics.get("accepted", 0.0) < 1.0
        or sum(len(block.text) for block in blocks if block.locator.page == page.page_number) < minimum_chars
    ]
    if page_count and len(ocr_required) == page_count:
        quality_profile = "image_only_pdf"
    elif ocr_required:
        quality_profile = "mixed_pdf"
    else:
        quality_profile = "born_digital_pdf"
    return ExtractionResult(
        format=DocumentFormat.PDF,
        blocks=blocks,
        pages=page_records,
        expected_units=page_count,
        extracted_units=sum(1 for page in page_records if page.native_text_coverage > 0),
        ocr_required_units=ocr_required,
        warnings=[] if not ocr_required else [f"{len(ocr_required)} page(s) require OCR"],
        metadata={
            "bbox_precision": "poppler_block",
            "extraction_method": method,
            "quality_profile": quality_profile,
        },
        evidence_variants=variants,
    )


def _parse_bbox_layout(
    path: Path,
    identity: SourceIdentity,
    context: ExtractionContext,
    *,
    method: str,
) -> tuple[list[ContentBlock], list[PageRecord], list[EvidenceVariant], int]:
    blocks: list[ContentBlock] = []
    pages: list[PageRecord] = []
    variants: list[EvidenceVariant] = []
    parsed_pages = 0
    minimum_chars = context.parser_config.quality.minimum_native_chars_per_page
    printable_minimum = context.parser_config.quality.minimum_native_printable_ratio
    for _, page_element in etree.iterparse(str(path), events=("end",), tag=PAGE_TAG, recover=True):
        parsed_pages += 1
        page_number = parsed_pages
        width = max(float(page_element.attrib.get("width", "1")), 1.0)
        height = max(float(page_element.attrib.get("height", "1")), 1.0)
        page_blocks: list[ContentBlock] = []
        page_text_parts: list[str] = []
        for block_index, block_element in enumerate(page_element.iter(BLOCK_TAG), start=1):
            text, max_line_height, table_likelihood = _block_text(block_element)
            if not text:
                continue
            bbox = _bbox(block_element, width, height)
            printable_ratio = _printable_ratio(text)
            replacement_ratio = _unicode_replacement_ratio(text)
            duplicate_ratio = _duplicate_glyph_ratio(text)
            confidence = min(
                1.0,
                printable_ratio,
                1.0 - replacement_ratio,
                1.0 - duplicate_ratio,
            )
            block_type = _classify_block(
                text,
                bbox,
                max_line_height,
                table_likelihood,
                page_number,
                height,
            )
            page_blocks.append(
                ContentBlock(
                    document_id=identity.document_id,
                    ordinal=len(blocks) + len(page_blocks),
                    block_type=block_type,
                    text=text,
                    language=_detect_language(text),
                    confidence=confidence,
                    locator=SourceLocator(format=DocumentFormat.PDF, page=page_number, bbox=bbox),
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    metadata={
                        "untrusted_evidence": True,
                        "queryable_evidence": True,
                        "coordinate_system": "pdf_points",
                        "extraction_method": method,
                        "layout_block": block_index,
                        "max_line_height": max_line_height,
                        "table_likelihood": table_likelihood,
                    },
                )
            )
            page_text_parts.append(text)
        page_text = "\n\n".join(page_text_parts)
        printable_ratio = _printable_ratio(page_text)
        replacement_ratio = _unicode_replacement_ratio(page_text)
        duplicate_ratio = _duplicate_glyph_ratio(page_text)
        language_supported = 1.0 if _detect_language(page_text) != "und" else 0.0
        density = min(1.0, len(page_text) / max(minimum_chars, 1))
        locator_coverage = 1.0 if page_blocks else 0.0
        trustworthy = (
            len(page_text) >= minimum_chars
            and printable_ratio >= printable_minimum
            and replacement_ratio <= context.parser_config.quality.maximum_unicode_replacement_ratio
            and duplicate_ratio <= context.parser_config.quality.maximum_duplicate_glyph_ratio
            and locator_coverage == 1.0
        )
        blocks.extend(page_blocks)
        variants.append(
            EvidenceVariant(
                unit=page_number,
                method=method,
                text=page_text,
                text_sha256=hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
                confidence=min(density, printable_ratio),
                selected=trustworthy,
            )
        )
        pages.append(
            PageRecord(
                document_id=identity.document_id,
                page_number=page_number,
                native_text_coverage=density,
                ocr_used=method != "native",
                warnings=[] if trustworthy else [f"{method} text below quality threshold"],
                quality_metrics={
                    "text_density": density,
                    "printable_ratio": printable_ratio,
                    "unicode_replacement_ratio": replacement_ratio,
                    "duplicate_glyph_ratio": duplicate_ratio,
                    "locator_coverage": locator_coverage,
                    "language_supported": language_supported,
                    "accepted": 1.0 if trustworthy else 0.0,
                },
            )
        )
        page_element.clear()
        parent = page_element.getparent()
        if parent is not None:
            while page_element.getprevious() is not None:
                del parent[0]
    return blocks, pages, variants, parsed_pages


def _merge_native_and_ocr(
    native: ExtractionResult,
    ocr: ExtractionResult,
    page_confidence: dict[int, float],
    context: ExtractionContext,
) -> ExtractionResult:
    selected_pages = set(native.ocr_required_units)
    native_by_page = _blocks_by_page(native.blocks)
    ocr_by_page = _blocks_by_page(ocr.blocks)
    native_page_records = {page.page_number: page for page in native.pages}
    ocr_page_records = {page.page_number: page for page in ocr.pages}
    blocks: list[ContentBlock] = []
    pages: list[PageRecord] = []
    unresolved: list[int] = []
    minimum_chars = context.parser_config.quality.minimum_native_chars_per_page
    minimum_confidence = context.parser_config.quality.minimum_ocr_confidence
    for page_number in range(1, native.expected_units + 1):
        if page_number not in selected_pages:
            page_blocks = native_by_page.get(page_number, [])
            blocks.extend(page_blocks)
            pages.append(native_page_records[page_number])
            continue
        page_blocks = ocr_by_page.get(page_number, [])
        confidence = page_confidence.get(page_number, 0.0)
        text_length = sum(len(block.text) for block in page_blocks)
        accepted = bool(page_blocks) and text_length >= minimum_chars and confidence >= minimum_confidence
        if not accepted:
            unresolved.append(page_number)
        for block in page_blocks:
            native_hashes = [candidate.text_sha256 for candidate in native_by_page.get(page_number, [])]
            blocks.append(
                block.model_copy(
                    update={
                        "confidence": min(block.confidence, confidence),
                        "metadata": {
                            **block.metadata,
                            "queryable_evidence": accepted,
                            "ocr_confidence": confidence,
                            "native_variant_sha256": native_hashes,
                        },
                    }
                )
            )
        base_page = ocr_page_records.get(page_number) or native_page_records[page_number]
        pages.append(
            base_page.model_copy(
                update={
                    "ocr_used": True,
                    "ocr_confidence": confidence,
                    "warnings": [] if accepted else ["OCR evidence below acceptance threshold"],
                    "quality_metrics": {
                        **base_page.quality_metrics,
                        "ocr_confidence": confidence,
                        "accepted": 1.0 if accepted else 0.0,
                    },
                }
            )
        )
    blocks = [block.model_copy(update={"ordinal": ordinal}) for ordinal, block in enumerate(blocks)]
    variants = []
    for variant in native.evidence_variants:
        variants.append(variant.model_copy(update={"selected": variant.unit not in selected_pages}))
    for variant in ocr.evidence_variants:
        if variant.unit in selected_pages:
            variants.append(
                variant.model_copy(
                    update={
                        "confidence": page_confidence.get(variant.unit, variant.confidence),
                        "selected": variant.unit not in unresolved,
                    }
                )
            )
    return ExtractionResult(
        format=DocumentFormat.PDF,
        blocks=blocks,
        pages=pages,
        expected_units=native.expected_units,
        extracted_units=sum(1 for page in pages if page.native_text_coverage > 0 or page.ocr_used),
        ocr_required_units=unresolved,
        warnings=[] if not unresolved else [f"OCR quality unresolved on pages: {unresolved}"],
        metadata={
            **native.metadata,
            "bbox_precision": "poppler_block",
            "ocr_applied": True,
            "quality_profile": "mixed_pdf"
            if len(selected_pages) < native.expected_units
            else "image_only_pdf",
        },
        evidence_variants=variants,
    )


def _blocks_by_page(blocks: Iterable[ContentBlock]) -> dict[int, list[ContentBlock]]:
    result: dict[int, list[ContentBlock]] = {}
    for block in blocks:
        if block.locator.page is not None:
            result.setdefault(block.locator.page, []).append(block)
    return result


def _block_text(element: etree._Element) -> tuple[str, float, int]:
    lines: list[str] = []
    max_line_height = 0.0
    table_likelihood = 0
    for line in element.iter(LINE_TAG):
        word_elements = list(line.iter(WORD_TAG))
        words = [_element_text(word) for word in word_elements]
        words = [word for word in words if word]
        if words:
            lines.append(" ".join(words))
        coordinates = []
        for word in word_elements:
            with suppress(KeyError, ValueError):
                coordinates.append((float(word.attrib["xMin"]), float(word.attrib["xMax"])))
        gaps = [coordinates[index + 1][0] - coordinates[index][1] for index in range(len(coordinates) - 1)]
        if len(words) >= 3 and any(gap >= 30 for gap in gaps):
            table_likelihood += 1
        with suppress(ValueError):
            max_line_height = max(
                max_line_height,
                float(line.attrib.get("yMax", "0")) - float(line.attrib.get("yMin", "0")),
            )
    return "\n".join(lines).strip(), max_line_height, table_likelihood


def _element_text(element: etree._Element) -> str:
    return "".join(
        item.decode("utf-8", errors="strict") if isinstance(item, bytes) else item for item in element.itertext()
    ).strip()


def _bbox(element: etree._Element, page_width: float, page_height: float) -> tuple[float, float, float, float]:
    try:
        x0 = max(0.0, float(element.attrib["xMin"]))
        y0 = max(0.0, float(element.attrib["yMin"]))
        x1 = min(page_width, float(element.attrib["xMax"]))
        y1 = min(page_height, float(element.attrib["yMax"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("Poppler block is missing a valid bounding box") from exc
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Poppler block bounding box has no area")
    return x0, y0, x1, y1


def _classify_block(
    text: str,
    bbox: tuple[float, float, float, float],
    max_line_height: float,
    table_likelihood: int,
    page_number: int,
    page_height: float,
) -> BlockType:
    first_line = text.splitlines()[0]
    words = first_line.split()
    if page_number == 1 and bbox[1] < page_height * 0.3 and max_line_height >= 14 and len(words) <= 20:
        return BlockType.TITLE
    if table_likelihood >= 2:
        return BlockType.TABLE
    if max_line_height >= 12 and len(words) <= 20:
        return BlockType.HEADING
    return BlockType.PARAGRAPH


def _parse_page_count(output: str) -> int:
    match = PAGES_PATTERN.search(output)
    return int(match.group(1)) if match else 0


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    printable = sum(1 for character in text if character.isprintable() or character in "\n\t")
    return printable / len(text)


def _unicode_replacement_ratio(text: str) -> float:
    return text.count("\ufffd") / max(len(text), 1)


def _duplicate_glyph_ratio(text: str) -> float:
    duplicate_characters = sum(len(match.group(0)) for match in re.finditer(r"([^\s])\1{4,}", text))
    return min(1.0, duplicate_characters / max(len(text), 1))


def _detect_language(text: str) -> str:
    alphabetic = [character for character in text if character.isalpha()]
    if not alphabetic:
        return "und"
    latin = sum(1 for character in alphabetic if "LATIN" in unicodedata.name(character, ""))
    return "en" if latin / len(alphabetic) >= 0.8 else "und"


def _pdf_metadata(source: Path) -> dict[str, Any]:
    try:
        reader = PdfReader(str(source), strict=False)
        metadata = {str(key).lstrip("/"): str(value) for key, value in (reader.metadata or {}).items()}
        bookmarks = _flatten_outline(reader, reader.outline)
        page_dimensions: dict[str, list[float]] = {}
        embedded_image_pages: dict[str, int] = {}
        for page_number, page in enumerate(reader.pages, start=1):
            page_dimensions[str(page_number)] = [float(page.mediabox.width), float(page.mediabox.height)]
            image_count = _count_image_resources(page)
            if image_count:
                embedded_image_pages[str(page_number)] = image_count
        return {
            "pdf_metadata": metadata,
            "bookmarks": bookmarks,
            "encrypted": reader.is_encrypted,
            "page_dimensions": page_dimensions,
            "embedded_image_pages": embedded_image_pages,
        }
    except Exception as exc:
        return {"pdf_metadata_warning": f"pypdf metadata unavailable: {type(exc).__name__}: {exc}"}


def _flatten_outline(reader: PdfReader, items: list[Any], depth: int = 0) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, list):
            result.extend(_flatten_outline(reader, item, depth + 1))
            continue
        title = getattr(item, "title", None)
        if not title:
            continue
        try:
            page_index = reader.get_destination_page_number(item)
            page = page_index + 1 if page_index is not None else None
        except Exception:
            page = None
        result.append({"title": str(title), "page": page, "depth": depth})
    return result


def _count_image_resources(page: Any) -> int:
    try:
        resources = page.get("/Resources")
        if resources is None:
            return 0
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return 0
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        count = 0
        for value in xobjects.values():
            value = value.get_object() if hasattr(value, "get_object") else value
            if str(value.get("/Subtype")) == "/Image":
                count += 1
        return count
    except Exception:
        return 0


def _append_figure_placeholders(
    extraction: ExtractionResult,
    identity: SourceIdentity,
    metadata: dict[str, Any],
) -> None:
    image_pages = metadata.get("embedded_image_pages", {})
    page_dimensions = metadata.get("page_dimensions", {})
    existing_pages = {
        block.locator.page
        for block in extraction.blocks
        if block.block_type == BlockType.FIGURE and block.locator.page is not None
    }
    for page_text, image_count in sorted(image_pages.items(), key=lambda item: int(item[0])):
        page_number = int(page_text)
        if page_number in existing_pages:
            continue
        dimensions = page_dimensions.get(page_text, [1.0, 1.0])
        width, height = max(float(dimensions[0]), 1.0), max(float(dimensions[1]), 1.0)
        text = f"[Embedded image placeholder: {image_count} image(s) on page {page_number}]"
        extraction.blocks.append(
            ContentBlock(
                document_id=identity.document_id,
                ordinal=len(extraction.blocks),
                block_type=BlockType.FIGURE,
                text=text,
                language="und",
                confidence=1.0,
                locator=SourceLocator(
                    format=DocumentFormat.PDF,
                    page=page_number,
                    bbox=(0.0, 0.0, width, height),
                ),
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                metadata={
                    "untrusted_evidence": True,
                    "queryable_evidence": False,
                    "coordinate_system": "pdf_points_full_page",
                    "extraction_method": "pdf_resource_placeholder",
                    "embedded_image_count": image_count,
                },
            )
        )
