"""Slide/shape-preserving PPTX extraction with notes, charts, comments, and media."""

from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from proofcounsel_ingest.extractors.base import EvidenceVariant, ExtractionContext, ExtractionResult
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    PageRecord,
    SourceIdentity,
    SourceLocator,
)

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class PptxExtractor:
    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult:
        presentation = Presentation(str(source))
        blocks: list[ContentBlock] = []
        pages: list[PageRecord] = []
        variants: list[EvidenceVariant] = []
        derived_artifacts: dict[str, Path] = {}
        comments = _comments_by_slide(source)
        for slide_number, slide in enumerate(presentation.slides, start=1):
            slide_start = len(blocks)
            title_shape = slide.shapes.title
            for shape in slide.shapes:
                self._extract_shape(
                    shape,
                    slide_number,
                    str(shape.shape_id),
                    title_shape is not None and shape.shape_id == title_shape.shape_id,
                    identity,
                    context,
                    blocks,
                    derived_artifacts,
                )
            notes_frame = slide.notes_slide.notes_text_frame
            if notes_frame is not None and notes_frame.text.strip():
                text = notes_frame.text.strip()
                blocks.append(
                    _block(
                        identity,
                        len(blocks),
                        BlockType.NOTE,
                        text,
                        slide_number,
                        "speaker-notes",
                        {"role": "speaker_notes"},
                    )
                )
            for comment_index, comment in enumerate(comments.get(slide_number, []), start=1):
                blocks.append(
                    _block(
                        identity,
                        len(blocks),
                        BlockType.COMMENT,
                        comment["text"],
                        slide_number,
                        f"comment-{comment_index}",
                        {"role": "comment", **comment["metadata"]},
                    )
                )
            slide_blocks = blocks[slide_start:]
            slide_text = "\n".join(
                block.text for block in slide_blocks if block.metadata.get("queryable_evidence", True)
            )
            selected = bool(slide_text)
            pages.append(
                PageRecord(
                    document_id=identity.document_id,
                    page_number=slide_number,
                    native_text_coverage=1.0 if selected else 0.0,
                    warnings=[] if selected else ["slide contained no queryable text"],
                    quality_metrics={
                        "locator_coverage": 1.0 if slide_blocks else 0.0,
                        "accepted": 1.0 if selected else 0.0,
                    },
                )
            )
            variants.append(
                EvidenceVariant(
                    unit=slide_number,
                    method="native_python_pptx",
                    text=slide_text,
                    text_sha256=_sha256(slide_text),
                    confidence=1.0 if selected else 0.0,
                    selected=selected,
                )
            )
        return ExtractionResult(
            format=DocumentFormat.PPTX,
            blocks=blocks,
            pages=pages,
            expected_units=len(presentation.slides),
            extracted_units=sum(1 for page in pages if page.native_text_coverage > 0),
            warnings=[warning for page in pages for warning in page.warnings],
            metadata={
                "quality_profile": "office",
                "extraction_method": "native_python_pptx",
                "slide_width": int(presentation.slide_width or 0),
                "slide_height": int(presentation.slide_height or 0),
                "core_properties": {
                    "title": presentation.core_properties.title,
                    "subject": presentation.core_properties.subject,
                    "author": presentation.core_properties.author,
                },
            },
            evidence_variants=variants,
            derived_artifacts=derived_artifacts,
        )

    def _extract_shape(
        self,
        shape: Any,
        slide_number: int,
        shape_path: str,
        is_title: bool,
        identity: SourceIdentity,
        context: ExtractionContext,
        blocks: list[ContentBlock],
        derived_artifacts: dict[str, Path],
    ) -> None:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for child in shape.shapes:
                self._extract_shape(
                    child,
                    slide_number,
                    f"{shape_path}/{child.shape_id}",
                    False,
                    identity,
                    context,
                    blocks,
                    derived_artifacts,
                )
            return

        if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
            text = shape.text_frame.text.strip()
            blocks.append(
                _block(
                    identity,
                    len(blocks),
                    BlockType.TITLE if is_title else BlockType.PARAGRAPH,
                    text,
                    slide_number,
                    shape_path,
                    {
                        "role": "slide_title" if is_title else "text_shape",
                        "heading_level": 1 if is_title else None,
                        "shape_name": shape.name,
                        "left": int(shape.left),
                        "top": int(shape.top),
                        "width": int(shape.width),
                        "height": int(shape.height),
                    },
                )
            )

        if getattr(shape, "has_table", False):
            for row_index, row in enumerate(shape.table.rows, start=1):
                values = [cell.text.strip() for cell in row.cells]
                if not any(values):
                    continue
                blocks.append(
                    _block(
                        identity,
                        len(blocks),
                        BlockType.TABLE_ROW,
                        "\t".join(values),
                        slide_number,
                        f"{shape_path}:table:row-{row_index}",
                        {"role": "table_row", "row": row_index, "shape_name": shape.name},
                    )
                )

        if getattr(shape, "has_chart", False):
            series_names = [str(series.name) for series in shape.chart.series]
            text = f"Chart {shape.name}; series={series_names}"
            blocks.append(
                _block(
                    identity,
                    len(blocks),
                    BlockType.FIGURE,
                    text,
                    slide_number,
                    f"{shape_path}:chart",
                    {"role": "chart", "series": series_names},
                )
            )

        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            extension = shape.image.ext
            name = f"slide-{slide_number}-shape-{shape_path.replace('/', '-')}.{extension}"
            if context.work_dir is not None:
                destination = context.work_dir / "embedded" / "pptx" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(shape.image.blob)
                derived_artifacts[f"embedded/pptx/{name}"] = destination
            blocks.append(
                _block(
                    identity,
                    len(blocks),
                    BlockType.FIGURE,
                    f"[Embedded slide image: {name}]",
                    slide_number,
                    f"{shape_path}:picture",
                    {"role": "picture", "queryable_evidence": False, "shape_name": shape.name},
                )
            )


def _block(
    identity: SourceIdentity,
    ordinal: int,
    block_type: BlockType,
    text: str,
    slide: int,
    shape_id: str,
    metadata: dict[str, Any],
) -> ContentBlock:
    return ContentBlock(
        document_id=identity.document_id,
        ordinal=ordinal,
        block_type=block_type,
        text=text,
        language="und",
        confidence=1.0,
        locator=SourceLocator(
            format=DocumentFormat.PPTX,
            slide=slide,
            shape_id=shape_id,
        ),
        text_sha256=_sha256(text),
        metadata={"untrusted_evidence": True, "queryable_evidence": True, **metadata},
    )


def _comments_by_slide(source: Path) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        for rel_name in sorted(name for name in names if re.match(r"ppt/slides/_rels/slide\d+\.xml\.rels$", name)):
            slide_match = re.search(r"slide(\d+)\.xml\.rels$", rel_name)
            if not slide_match:
                continue
            slide_number = int(slide_match.group(1))
            root = etree.fromstring(archive.read(rel_name))
            for relationship in root.findall(f"{{{REL_NS}}}Relationship"):
                rel_type = relationship.get("Type", "")
                if not rel_type.endswith("/comments"):
                    continue
                target = relationship.get("Target", "")
                comment_name = posixpath.normpath(posixpath.join("ppt/slides", target))
                if comment_name not in names:
                    continue
                comment_root = etree.fromstring(archive.read(comment_name))
                raw_comments = comment_root.xpath("//*[local-name()='cm']")
                if not isinstance(raw_comments, list):
                    continue
                for raw_comment in raw_comments:
                    if not isinstance(raw_comment, etree._Element):
                        continue
                    text_parts = raw_comment.xpath(".//*[local-name()='text']/text()")
                    if not isinstance(text_parts, list):
                        continue
                    text = " ".join(
                        part.decode("utf-8", errors="strict") if isinstance(part, bytes) else str(part)
                        for part in text_parts
                    )
                    if text.strip():
                        result.setdefault(slide_number, []).append(
                            {
                                "text": text.strip(),
                                "metadata": {
                                    "author_id": raw_comment.get("authorId"),
                                    "created_at": raw_comment.get("dt"),
                                },
                            }
                        )
    return result


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
