"""Ordered OOXML Word extraction with stable part and element locators."""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from proofcounsel_ingest.extractors.base import EvidenceVariant, ExtractionContext, ExtractionResult
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    PageRecord,
    SourceIdentity,
    SourceLocator,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
NS = {"w": W_NS, "w14": W14_NS}


class DocxExtractor:
    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult:
        document = Document(str(source))
        blocks: list[ContentBlock] = []
        derived_artifacts: dict[str, Path] = {}
        self._extract_part(
            document.element.body,
            "word/document.xml",
            identity,
            blocks,
            role="body",
        )
        seen_parts: set[str] = set()
        for section in document.sections:
            for role, container in (("header", section.header), ("footer", section.footer)):
                part_name = str(container.part.partname).lstrip("/")
                if part_name in seen_parts:
                    continue
                seen_parts.add(part_name)
                self._extract_part(container._element, part_name, identity, blocks, role=role)

        with zipfile.ZipFile(source) as archive:
            for part_name, block_type, element_name in (
                ("word/footnotes.xml", BlockType.FOOTNOTE, "footnote"),
                ("word/endnotes.xml", BlockType.FOOTNOTE, "endnote"),
                ("word/comments.xml", BlockType.COMMENT, "comment"),
            ):
                if part_name in archive.namelist():
                    self._extract_annotation_part(
                        archive.read(part_name),
                        part_name,
                        element_name,
                        block_type,
                        identity,
                        blocks,
                    )
            self._extract_media(archive, identity, context, blocks, derived_artifacts)

        blocks = [block.model_copy(update={"ordinal": ordinal}) for ordinal, block in enumerate(blocks)]
        full_text = "\n".join(
            block.text for block in blocks if block.metadata.get("queryable_evidence", True)
        )
        page = PageRecord(
            document_id=identity.document_id,
            page_number=1,
            native_text_coverage=1.0 if full_text else 0.0,
            warnings=[] if full_text else ["DOCX contained no queryable text"],
            quality_metrics={
                "text_density": min(1.0, len(full_text) / 80),
                "locator_coverage": 1.0 if blocks else 0.0,
                "accepted": 1.0 if full_text else 0.0,
            },
        )
        return ExtractionResult(
            format=DocumentFormat.DOCX,
            blocks=blocks,
            pages=[page],
            expected_units=1,
            extracted_units=1 if full_text else 0,
            warnings=page.warnings,
            metadata={
                "quality_profile": "office",
                "extraction_method": "native_ooxml_docx",
                "core_properties": {
                    "title": document.core_properties.title,
                    "subject": document.core_properties.subject,
                    "author": document.core_properties.author,
                },
            },
            evidence_variants=[
                EvidenceVariant(
                    unit=1,
                    method="native_ooxml_docx",
                    text=full_text,
                    text_sha256=_sha256(full_text),
                    confidence=1.0 if full_text else 0.0,
                    selected=bool(full_text),
                )
            ],
            derived_artifacts=derived_artifacts,
        )

    def _extract_part(
        self,
        root: Any,
        part_name: str,
        identity: SourceIdentity,
        blocks: list[ContentBlock],
        *,
        role: str,
    ) -> None:
        paragraph_counter = 0
        table_counter = 0
        textbox_counter = 0
        for child in root.iterchildren():
            if child.tag == qn("w:p"):
                paragraph_counter += 1
                paragraph_id = child.get(f"{{{W14_NS}}}paraId") or f"p-{paragraph_counter}"
                text = _visible_paragraph_text(child)
                style = _paragraph_style(child)
                heading_level = _heading_level(style)
                if text:
                    blocks.append(
                        _block(
                            identity,
                            len(blocks),
                            _paragraph_block_type(role, heading_level),
                            text,
                            SourceLocator(
                                format=DocumentFormat.DOCX,
                                part=part_name,
                                paragraph_id=paragraph_id,
                            ),
                            {
                                "role": role,
                                "style": style,
                                "heading_level": heading_level,
                                "inserted_text_present": bool(_xpath(child, ".//w:ins")),
                                "hyperlink_count": len(_xpath(child, ".//w:hyperlink")),
                            },
                        )
                    )
                deleted_text = " ".join(
                    text.strip()
                    for text in _xpath(child, ".//w:delText/text()")
                    if text.strip()
                )
                if deleted_text:
                    blocks.append(
                        _block(
                            identity,
                            len(blocks),
                            BlockType.COMMENT,
                            deleted_text,
                            SourceLocator(
                                format=DocumentFormat.DOCX,
                                part=part_name,
                                paragraph_id=f"{paragraph_id}:deleted",
                            ),
                            {"role": "tracked_deletion", "queryable_evidence": False},
                        )
                    )
                for textbox in _xpath(child, ".//w:txbxContent"):
                    textbox_counter += 1
                    textbox_text = _all_text(textbox)
                    if textbox_text:
                        blocks.append(
                            _block(
                                identity,
                                len(blocks),
                                BlockType.NOTE,
                                textbox_text,
                                SourceLocator(
                                    format=DocumentFormat.DOCX,
                                    part=part_name,
                                    paragraph_id=f"textbox-{textbox_counter}",
                                ),
                                {"role": "textbox"},
                            )
                        )
            elif child.tag == qn("w:tbl"):
                table_counter += 1
                table_id = f"table-{table_counter}"
                rows = _xpath(child, "./w:tr")
                for row_index, row in enumerate(rows, start=1):
                    cells = _xpath(row, "./w:tc")
                    values = [_all_text(cell) for cell in cells]
                    if not any(values):
                        continue
                    text = "\t".join(values)
                    blocks.append(
                        _block(
                            identity,
                            len(blocks),
                            BlockType.TABLE_ROW,
                            text,
                            SourceLocator(
                                format=DocumentFormat.DOCX,
                                part=part_name,
                                table_id=table_id,
                                cell_range=f"R{row_index}C1:R{row_index}C{len(cells)}",
                            ),
                            {"role": role, "table_id": table_id, "row": row_index},
                        )
                    )

    def _extract_annotation_part(
        self,
        payload: bytes,
        part_name: str,
        element_name: str,
        block_type: BlockType,
        identity: SourceIdentity,
        blocks: list[ContentBlock],
    ) -> None:
        root = etree.fromstring(payload)
        for element in _xpath(root, f"./w:{element_name}"):
            identifier = element.get(f"{{{W_NS}}}id", str(len(blocks)))
            try:
                if int(identifier) < 0:
                    continue
            except ValueError:
                pass
            text = _all_text(element)
            if not text:
                continue
            blocks.append(
                _block(
                    identity,
                    len(blocks),
                    block_type,
                    text,
                    SourceLocator(
                        format=DocumentFormat.DOCX,
                        part=part_name,
                        paragraph_id=f"{element_name}-{identifier}",
                    ),
                    {"role": element_name, "annotation_id": identifier},
                )
            )

    def _extract_media(
        self,
        archive: zipfile.ZipFile,
        identity: SourceIdentity,
        context: ExtractionContext,
        blocks: list[ContentBlock],
        derived_artifacts: dict[str, Path],
    ) -> None:
        for name in sorted(item for item in archive.namelist() if item.startswith("word/media/") and not item.endswith("/")):
            media_name = Path(name).name
            if context.work_dir is not None:
                destination = context.work_dir / "embedded" / "docx" / media_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
            else:
                destination = None
            text = f"[Embedded Word media: {media_name}]"
            blocks.append(
                _block(
                    identity,
                    len(blocks),
                    BlockType.FIGURE,
                    text,
                    SourceLocator(
                        format=DocumentFormat.DOCX,
                        part=name,
                        paragraph_id=f"media-{media_name}",
                    ),
                    {"role": "embedded_media", "queryable_evidence": False},
                )
            )
            if destination is not None:
                derived_artifacts[f"embedded/docx/{media_name}"] = destination


def _visible_paragraph_text(element: Any) -> str:
    pieces: list[str] = []
    for node in element.iter():
        if node.tag == qn("w:txbxContent"):
            continue
        if any(ancestor.tag in {qn("w:del"), qn("w:txbxContent")} for ancestor in node.iterancestors()):
            continue
        if node.tag == qn("w:t") and node.text:
            pieces.append(node.text)
        elif node.tag == qn("w:tab"):
            pieces.append("\t")
        elif node.tag in {qn("w:br"), qn("w:cr")}:
            pieces.append("\n")
    return _normalize_text("".join(pieces))


def _all_text(element: Any) -> str:
    return _normalize_text(" ".join(str(text) for text in _xpath(element, ".//w:t/text()")))


def _paragraph_style(element: Any) -> str:
    values = _xpath(element, "./w:pPr/w:pStyle/@w:val")
    return str(values[0]) if values else ""


def _heading_level(style: str) -> int | None:
    match = re.search(r"heading\s*([1-9])", style, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _paragraph_block_type(role: str, heading_level: int | None) -> BlockType:
    if role == "header":
        return BlockType.HEADER
    if role == "footer":
        return BlockType.FOOTER
    if heading_level == 1:
        return BlockType.TITLE
    if heading_level is not None:
        return BlockType.HEADING
    return BlockType.PARAGRAPH


def _block(
    identity: SourceIdentity,
    ordinal: int,
    block_type: BlockType,
    text: str,
    locator: SourceLocator,
    metadata: dict[str, Any],
) -> ContentBlock:
    return ContentBlock(
        document_id=identity.document_id,
        ordinal=ordinal,
        block_type=block_type,
        text=text,
        language="und",
        confidence=1.0,
        locator=locator,
        text_sha256=_sha256(text),
        metadata={"untrusted_evidence": True, "queryable_evidence": True, **metadata},
    )


def _normalize_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.replace("\r", "\n").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _xpath(element: Any, expression: str) -> list[Any]:
    try:
        return list(element.xpath(expression, namespaces=NS))
    except TypeError:
        return list(element.xpath(expression))
