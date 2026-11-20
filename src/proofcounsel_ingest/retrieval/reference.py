"""Deterministic legal and source-locator reference resolution."""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID

from proofcounsel_ingest.models import ContentBlock, StructureNode
from proofcounsel_ingest.retrieval.repository import SearchRecord

LEGAL_REFERENCE = re.compile(
    r"\b(article|section|clause|paragraph|schedule|appendix|chapter|part|exhibit)\s+"
    r"([0-9ivxlcdm]+(?:[.][0-9]+)*(?:[(][a-z0-9ivxlcdm]+[)])*|[a-z])(?![\w.])",
    re.IGNORECASE,
)
SECTION_SYMBOL_REFERENCE = re.compile(
    r"(?:§|sec[.]?)\s*([0-9]+(?:[.][0-9]+)*(?:[(][a-z0-9ivxlcdm]+[)])*)",
    re.IGNORECASE,
)
PAGE_REFERENCE = re.compile(r"\b(?:physical\s+)?page\s+(\d{1,6})\b", re.IGNORECASE)
SLIDE_REFERENCE = re.compile(r"\bslide\s+(\d{1,5})\b", re.IGNORECASE)
SHEET_CELL_REFERENCE = re.compile(
    r"(?:sheet\s+)?(?:'([^']+)'|\"([^\"]+)\"|([A-Z0-9_.-]+))!"
    r"([A-Z]{1,3}\d+(?::[A-Z]{1,3}\d+)?)",
    re.IGNORECASE,
)
CELL_REFERENCE = re.compile(r"\bcell\s+([A-Z]{1,3}\d+(?::[A-Z]{1,3}\d+)?)\b", re.IGNORECASE)


def resolve_reference(
    query: str,
    *,
    tenant_id: UUID,
    document_id: UUID,
    source_sha256: str,
    blocks: Sequence[ContentBlock],
    nodes: Sequence[StructureNode],
    limit: int = 20,
) -> list[SearchRecord]:
    legal = LEGAL_REFERENCE.search(query)
    symbol = SECTION_SYMBOL_REFERENCE.search(query)
    page = PAGE_REFERENCE.search(query)
    slide = SLIDE_REFERENCE.search(query)
    sheet_cell = SHEET_CELL_REFERENCE.search(query)
    cell = CELL_REFERENCE.search(query) if sheet_cell is None else None
    if not any((legal, symbol, page, slide, sheet_cell, cell)):
        return []

    results: list[SearchRecord] = []
    if legal or symbol:
        ordered_blocks = sorted(blocks, key=lambda block: block.ordinal)
        position_by_id = {block.block_id: position for position, block in enumerate(ordered_blocks)}
        if legal is not None:
            target = f"{legal.group(1)} {legal.group(2)}"
        elif symbol is not None:
            target = f"section {symbol.group(1)}"
        else:  # pragma: no cover - guarded by the surrounding condition
            target = ""
        normalized_target = _normalize_reference(target)
        for node in nodes:
            normalized_title = _normalize_reference(node.title)
            if not (normalized_title == normalized_target or normalized_title.startswith(normalized_target + " ")):
                continue
            start = position_by_id.get(node.start_block_id)
            end = position_by_id.get(node.end_block_id)
            if start is None or end is None or end < start:
                continue
            results.extend(
                _record(tenant_id, document_id, source_sha256, block, node.node_id)
                for block in ordered_blocks[start : end + 1]
                if block.metadata.get("queryable_evidence", True)
            )

    if page:
        page_number = int(page.group(1))
        results.extend(
            _record(tenant_id, document_id, source_sha256, block)
            for block in blocks
            if block.locator.page == page_number and block.metadata.get("queryable_evidence", True)
        )

    if slide:
        slide_number = int(slide.group(1))
        results.extend(
            _record(tenant_id, document_id, source_sha256, block)
            for block in blocks
            if block.locator.slide == slide_number and block.metadata.get("queryable_evidence", True)
        )

    if sheet_cell:
        sheet = next(group for group in sheet_cell.groups()[:3] if group is not None).strip()
        cell_range = sheet_cell.group(4).upper()
        results.extend(
            _record(tenant_id, document_id, source_sha256, block)
            for block in blocks
            if block.locator.part
            and block.locator.part.casefold() == sheet.casefold()
            and block.locator.cell_range
            and _ranges_intersect(block.locator.cell_range.upper(), cell_range)
        )
    else:
        if cell:
            cell_range = cell.group(1).upper()
            results.extend(
                _record(tenant_id, document_id, source_sha256, block)
                for block in blocks
                if block.locator.cell_range and _ranges_intersect(block.locator.cell_range.upper(), cell_range)
            )

    deduplicated: dict[UUID, SearchRecord] = {}
    for result in results:
        deduplicated[result.block_id] = result
    return sorted(deduplicated.values(), key=lambda result: result.ordinal)[:limit]


def _record(
    tenant_id: UUID,
    document_id: UUID,
    source_sha256: str,
    block: ContentBlock,
    node_id: UUID | None = None,
) -> SearchRecord:
    return SearchRecord(
        tenant_id=tenant_id,
        document_id=document_id,
        source_sha256=source_sha256,
        block_id=block.block_id,
        node_id=node_id,
        ordinal=block.ordinal,
        text=block.text,
        raw_score=1.0,
        channel="exact_reference",
    )


def _ranges_intersect(stored: str, requested: str) -> bool:
    stored_bounds = _range_bounds(stored)
    requested_bounds = _range_bounds(requested)
    if stored_bounds is None or requested_bounds is None:
        return stored == requested
    stored_min_col, stored_min_row, stored_max_col, stored_max_row = stored_bounds
    request_min_col, request_min_row, request_max_col, request_max_row = requested_bounds
    return not (
        stored_max_col < request_min_col
        or request_max_col < stored_min_col
        or stored_max_row < request_min_row
        or request_max_row < stored_min_row
    )


def _normalize_reference(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9.()]+", " ", value.casefold()).split())


def _range_bounds(value: str) -> tuple[int, int, int, int] | None:
    cells = re.findall(r"([A-Z]{1,3})(\d+)", value.upper())
    if not cells:
        return None
    first, last = cells[0], cells[-1]
    first_col, first_row = _column_number(first[0]), int(first[1])
    last_col, last_row = _column_number(last[0]), int(last[1])
    return min(first_col, last_col), min(first_row, last_row), max(first_col, last_col), max(first_row, last_row)


def _column_number(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - ord("A") + 1
    return result
