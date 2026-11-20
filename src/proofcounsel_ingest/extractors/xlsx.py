"""Coordinate-preserving XLSX extraction for values, formulas, comments, charts, and media."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from openpyxl import load_workbook  # type: ignore[import-untyped]
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

from proofcounsel_ingest.extractors.base import EvidenceVariant, ExtractionContext, ExtractionResult
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    PageRecord,
    SourceIdentity,
    SourceLocator,
)


class XlsxExtractor:
    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult:
        formula_book = load_workbook(source, read_only=False, data_only=False)
        value_book = load_workbook(source, read_only=False, data_only=True)
        blocks: list[ContentBlock] = []
        pages: list[PageRecord] = []
        variants: list[EvidenceVariant] = []
        derived_artifacts: dict[str, Path] = {}
        formula_count = 0
        for sheet_index, sheet in enumerate(formula_book.worksheets, start=1):
            if sheet.max_row > context.parser_config.limits.max_spreadsheet_rows:
                raise ValueError(
                    f"sheet {sheet.title!r} exceeds max_spreadsheet_rows="
                    f"{context.parser_config.limits.max_spreadsheet_rows}"
                )
            value_sheet = value_book[sheet.title]
            sheet_start = len(blocks)
            blocks.append(
                _block(
                    identity,
                    len(blocks),
                    BlockType.HEADING,
                    f"Sheet: {sheet.title}",
                    sheet.title,
                    "A1",
                    {
                        "heading_level": 1,
                        "sheet_state": sheet.sheet_state,
                        "role": "sheet_title",
                    },
                )
            )
            for row_index, row in enumerate(sheet.iter_rows(), start=1):
                row_parts: list[str] = []
                populated_coordinates: list[str] = []
                for cell in row:
                    if cell.value is None:
                        continue
                    coordinate = cell.coordinate
                    populated_coordinates.append(coordinate)
                    if cell.data_type == "f" or (isinstance(cell.value, str) and cell.value.startswith("=")):
                        formula_count += 1
                        cached = value_sheet[coordinate].value
                        formula = str(cell.value).removeprefix("=")
                        row_parts.append(
                            f"{coordinate}[formula]={formula}; displayed={cached if cached is not None else '<unavailable>'}"
                        )
                    else:
                        row_parts.append(f"{coordinate}={cell.value}")
                    if cell.comment and cell.comment.text.strip():
                        blocks.append(
                            _block(
                                identity,
                                len(blocks),
                                BlockType.COMMENT,
                                cell.comment.text.strip(),
                                sheet.title,
                                coordinate,
                                {
                                    "role": "cell_comment",
                                    "author": cell.comment.author,
                                    "sheet_state": sheet.sheet_state,
                                },
                            )
                        )
                if not row_parts:
                    continue
                start_coordinate = populated_coordinates[0]
                end_coordinate = populated_coordinates[-1]
                blocks.append(
                    _block(
                        identity,
                        len(blocks),
                        BlockType.TABLE_ROW,
                        "\t".join(row_parts),
                        sheet.title,
                        f"{start_coordinate}:{end_coordinate}",
                        {
                            "role": "sheet_row",
                            "row": row_index,
                            "sheet_state": sheet.sheet_state,
                            "hidden_row": bool(sheet.row_dimensions[row_index].hidden),
                        },
                    )
                )

            for merged_range in sheet.merged_cells.ranges:
                blocks.append(
                    _block(
                        identity,
                        len(blocks),
                        BlockType.NOTE,
                        f"Merged cell range: {merged_range}",
                        sheet.title,
                        str(merged_range),
                        {"role": "merged_range", "queryable_evidence": False},
                    )
                )

            for table in sheet.tables.values():
                blocks.append(
                    _block(
                        identity,
                        len(blocks),
                        BlockType.TABLE,
                        f"Table {table.name}: {table.ref}",
                        sheet.title,
                        str(table.ref),
                        {"role": "native_table", "table_name": table.name},
                    )
                )

            for chart_index, chart in enumerate(sheet._charts, start=1):
                anchor = _anchor_cell(chart)
                title = _chart_title(chart) or f"Chart {chart_index}"
                series = [str(getattr(item, "tx", "")) for item in chart.series]
                blocks.append(
                    _block(
                        identity,
                        len(blocks),
                        BlockType.FIGURE,
                        f"{title}; series={series}",
                        sheet.title,
                        anchor,
                        {"role": "chart", "chart_index": chart_index},
                    )
                )

            for image_index, image in enumerate(sheet._images, start=1):
                anchor = _anchor_cell(image)
                extension = str(getattr(image, "format", "png") or "png").lower()
                name = f"{_safe_name(sheet.title)}-{image_index}.{extension}"
                destination = None
                if context.work_dir is not None:
                    destination = context.work_dir / "embedded" / "xlsx" / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(image._data())
                    derived_artifacts[f"embedded/xlsx/{name}"] = destination
                blocks.append(
                    _block(
                        identity,
                        len(blocks),
                        BlockType.FIGURE,
                        f"[Embedded spreadsheet image: {name}]",
                        sheet.title,
                        anchor,
                        {"role": "embedded_image", "queryable_evidence": False},
                    )
                )

            sheet_blocks = blocks[sheet_start:]
            sheet_text = "\n".join(
                block.text for block in sheet_blocks if block.metadata.get("queryable_evidence", True)
            )
            selected = bool(sheet_text)
            pages.append(
                PageRecord(
                    document_id=identity.document_id,
                    page_number=sheet_index,
                    native_text_coverage=1.0 if selected else 0.0,
                    warnings=[] if selected else ["worksheet contained no queryable values"],
                    quality_metrics={
                        "locator_coverage": 1.0 if sheet_blocks else 0.0,
                        "accepted": 1.0 if selected else 0.0,
                    },
                )
            )
            variants.append(
                EvidenceVariant(
                    unit=sheet_index,
                    method="native_openpyxl",
                    text=sheet_text,
                    text_sha256=_sha256(sheet_text),
                    confidence=1.0 if selected else 0.0,
                    selected=selected,
                )
            )

        named_ranges = [
            {"name": defined.name, "attr_text": defined.attr_text, "hidden": bool(defined.hidden)}
            for defined in formula_book.defined_names.values()
        ]
        return ExtractionResult(
            format=DocumentFormat.XLSX,
            blocks=blocks,
            pages=pages,
            expected_units=len(formula_book.worksheets),
            extracted_units=sum(1 for page in pages if page.native_text_coverage > 0),
            warnings=[warning for page in pages for warning in page.warnings],
            metadata={
                "quality_profile": "office",
                "extraction_method": "native_openpyxl",
                "sheet_names": formula_book.sheetnames,
                "named_ranges": named_ranges,
                "formula_count": formula_count,
            },
            evidence_variants=variants,
            derived_artifacts=derived_artifacts,
        )


def _block(
    identity: SourceIdentity,
    ordinal: int,
    block_type: BlockType,
    text: str,
    sheet: str,
    cell_range: str,
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
            format=DocumentFormat.XLSX,
            part=sheet,
            cell_range=cell_range,
        ),
        text_sha256=_sha256(text),
        metadata={"untrusted_evidence": True, "queryable_evidence": True, **metadata},
    )


def _anchor_cell(item: Any) -> str:
    anchor = getattr(item, "anchor", None)
    marker = getattr(anchor, "_from", None)
    if marker is None:
        return "A1"
    return f"{get_column_letter(int(marker.col) + 1)}{int(marker.row) + 1}"


def _chart_title(chart: Any) -> str:
    try:
        paragraphs = chart.title.tx.rich.p
        return " ".join(run.t for paragraph in paragraphs for run in paragraph.r if run.t)
    except (AttributeError, TypeError):
        return ""


def _safe_name(value: str) -> str:
    safe = "".join(character if character.isalnum() or character in "-_" else "-" for character in value)
    return safe.strip("-") or "sheet"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
