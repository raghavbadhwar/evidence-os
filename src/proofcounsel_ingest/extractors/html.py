"""HTML extraction that excludes active and hidden content."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from charset_normalizer import from_bytes
from lxml import etree, html

from proofcounsel_ingest.extractors.base import ExtractionContext, ExtractionResult
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    PageRecord,
    SourceIdentity,
    SourceLocator,
)

REMOVED_TAGS = ("script", "style", "template", "noscript", "head", "svg", "canvas")
BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre", "blockquote"})
HIDDEN_STYLE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE)


class HtmlExtractor:
    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult:
        del context
        payload = source.read_bytes()
        match = from_bytes(payload).best()
        if match is None:
            raise ValueError("HTML encoding could not be determined")
        document = html.fromstring(str(match))
        etree.strip_elements(document, *REMOVED_TAGS, with_tail=False)
        for element in list(document.iter()):
            if _is_hidden(element):
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
        tree = document.getroottree()
        blocks: list[ContentBlock] = []
        for element in document.iter():
            tag = _local_name(element)
            if tag not in BLOCK_TAGS:
                continue
            text = _element_text(element, tag)
            if not text:
                continue
            block_type = _block_type(tag)
            blocks.append(
                ContentBlock(
                    document_id=identity.document_id,
                    ordinal=len(blocks),
                    block_type=block_type,
                    text=text,
                    language="und",
                    confidence=1.0,
                    locator=SourceLocator(format=DocumentFormat.HTML, dom_path=tree.getpath(element)),
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    metadata={"untrusted_evidence": True, "html_tag": tag},
                )
            )
        page = PageRecord(
            document_id=identity.document_id,
            page_number=1,
            native_text_coverage=1.0 if blocks else 0.0,
            warnings=[] if blocks else ["no visible HTML content extracted"],
        )
        return ExtractionResult(
            format=DocumentFormat.HTML,
            blocks=blocks,
            pages=[page],
            expected_units=1,
            extracted_units=1 if blocks else 0,
            warnings=page.warnings,
            metadata={"encoding": match.encoding or "unknown"},
        )


def _is_hidden(element: etree._Element) -> bool:
    if "hidden" in element.attrib:
        return True
    if element.attrib.get("aria-hidden", "").lower() == "true":
        return True
    return bool(HIDDEN_STYLE.search(element.attrib.get("style", "")))


def _local_name(element: etree._Element) -> str:
    return str(element.tag).split("}")[-1].lower() if isinstance(element.tag, str) else ""


def _element_text(element: etree._Element, tag: str) -> str:
    if tag == "tr":
        cells = []
        for child in element:
            if _local_name(child) in {"td", "th"}:
                cell_text = " ".join(_joined_itertext(child).split())
                if cell_text:
                    cells.append(cell_text)
        return "\t".join(cells)
    return " ".join(_joined_itertext(element).split())


def _joined_itertext(element: etree._Element) -> str:
    return "".join(part.decode("utf-8", errors="strict") if isinstance(part, bytes) else part for part in element.itertext())


def _block_type(tag: str) -> BlockType:
    if tag == "h1":
        return BlockType.TITLE
    if tag.startswith("h"):
        return BlockType.HEADING
    if tag == "li":
        return BlockType.LIST_ITEM
    if tag == "tr":
        return BlockType.TABLE_ROW
    if tag == "pre":
        return BlockType.CODE
    return BlockType.PARAGRAPH
