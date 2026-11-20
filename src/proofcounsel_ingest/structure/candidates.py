"""Deterministic structure candidates from native hierarchy, layout, legal references, and TOCs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, Field

from proofcounsel_ingest.extractors.base import ExtractionResult
from proofcounsel_ingest.models import BlockType, ContentBlock

LEGAL_HEADING = re.compile(
    r"^(article|section|clause|paragraph|schedule|appendix|chapter|part)\s+([0-9ivxlcdm]+[a-z-]*)\b",
    re.IGNORECASE,
)
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+){0,5})\s+\S")
TOC_ENTRY = re.compile(r"^(?P<title>.+?)(?:\.{2,}|\s{3,})\s*(?P<page>\d{1,5})\s*$")
YEAR_ONLY = re.compile(r"^(?:18|19|20|21)\d{2}[a-z]?[.,]?$", re.IGNORECASE)


class StructureCandidate(BaseModel):
    block_id: UUID
    block_ordinal: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=2000)
    level: int = Field(ge=1, le=12)
    method: str
    confidence: float = Field(ge=0, le=1)


def collect_candidates(extraction: ExtractionResult) -> list[StructureCandidate]:
    queryable = sorted(
        (block for block in extraction.blocks if block.metadata.get("queryable_evidence", True)),
        key=lambda block: block.ordinal,
    )
    _mark_toc_candidates(extraction)
    candidates: list[StructureCandidate] = []
    has_pdf_bookmarks = bool(extraction.metadata.get("bookmarks"))
    for block in queryable:
        title = _heading_title(block.text)
        if not title or not _valid_heading(title):
            continue
        heading_level = block.metadata.get("heading_level")
        role = block.metadata.get("role")
        legal = LEGAL_HEADING.match(title)
        numbered = NUMBERED_HEADING.match(title)
        if heading_level is not None:
            method, confidence, level = "native_style", 0.99, int(heading_level)
        elif role == "slide_title":
            method, confidence, level = "native_slide_title", 0.99, 1
        elif role == "sheet_title":
            method, confidence, level = "native_sheet_title", 0.99, 1
        elif legal:
            method, confidence, level = "legal_reference", 0.97, _legal_level(legal.group(1))
        elif numbered and not has_pdf_bookmarks:
            method, confidence, level = "numbered_layout", 0.90, min(numbered.group(1).count(".") + 1, 6)
        elif block.block_type in {BlockType.TITLE, BlockType.HEADING}:
            if has_pdf_bookmarks and not (block.block_type == BlockType.TITLE and block.ordinal <= 2):
                continue
            if not _valid_layout_heading(title):
                continue
            method = "layout_heading"
            confidence = 0.88 if block.block_type == BlockType.TITLE else 0.80
            level = 1 if block.block_type == BlockType.TITLE else 2
        else:
            continue
        candidates.append(
            StructureCandidate(
                block_id=block.block_id,
                block_ordinal=block.ordinal,
                title=title,
                level=level,
                method=method,
                confidence=confidence,
            )
        )

    candidates.extend(_bookmark_candidates(extraction, queryable, {candidate.block_id for candidate in candidates}))
    candidates.extend(_toc_alignment_candidates(extraction, queryable, {candidate.block_id for candidate in candidates}))
    return _deduplicate_candidates(candidates)


def _bookmark_candidates(
    extraction: ExtractionResult,
    blocks: Sequence[ContentBlock],
    occupied: set[UUID],
) -> list[StructureCandidate]:
    by_page: dict[int, list[ContentBlock]] = {}
    for block in blocks:
        if block.locator.page is not None:
            by_page.setdefault(block.locator.page, []).append(block)
    result: list[StructureCandidate] = []
    for bookmark in extraction.metadata.get("bookmarks", []):
        page = bookmark.get("page")
        title = str(bookmark.get("title", "")).strip()
        if not isinstance(page, int) or not _valid_heading(title):
            continue
        page_blocks = by_page.get(page, [])
        if not page_blocks:
            continue
        block = max(page_blocks, key=lambda candidate: _token_overlap(title, candidate.text))
        if block.block_id in occupied:
            continue
        result.append(
            StructureCandidate(
                block_id=block.block_id,
                block_ordinal=block.ordinal,
                title=title,
                level=min(int(bookmark.get("depth", 0)) + 1, 12),
                method="pdf_bookmark",
                confidence=1.0,
            )
        )
        occupied.add(block.block_id)
    return result


def _toc_alignment_candidates(
    extraction: ExtractionResult,
    blocks: Sequence[ContentBlock],
    occupied: set[UUID],
) -> list[StructureCandidate]:
    toc_entries: list[tuple[str, int]] = []
    for block in extraction.blocks:
        if not block.metadata.get("toc_candidate"):
            continue
        for line in block.text.splitlines():
            match = TOC_ENTRY.match(" ".join(line.split()))
            if match and _valid_heading(match.group("title")):
                toc_entries.append((match.group("title").strip(), block.ordinal))
    if not toc_entries:
        return []

    result: list[StructureCandidate] = []
    minimum_ordinal = 0
    for title, toc_ordinal in toc_entries:
        best: tuple[float, ContentBlock] | None = None
        for block in blocks:
            if block.ordinal <= max(toc_ordinal, minimum_ordinal) or block.block_id in occupied:
                continue
            score = _token_overlap(title, block.text)
            if score >= 0.70 and (best is None or score > best[0]):
                best = (score, block)
        if best is None:
            continue
        score, block = best
        result.append(
            StructureCandidate(
                block_id=block.block_id,
                block_ordinal=block.ordinal,
                title=title,
                level=_title_level(title),
                method="toc_alignment",
                confidence=min(0.90, score),
            )
        )
        occupied.add(block.block_id)
        minimum_ordinal = block.ordinal
    return result


def _mark_toc_candidates(extraction: ExtractionResult) -> None:
    updated: list[ContentBlock] = []
    for block in extraction.blocks:
        lines = [" ".join(line.split()) for line in block.text.splitlines() if line.strip()]
        heading = " ".join(lines[:3]).lower()
        entry_count = sum(1 for line in lines if TOC_ENTRY.match(line))
        candidate = "table of contents" in heading or heading == "contents" or entry_count >= 3
        if candidate:
            block = block.model_copy(update={"metadata": {**block.metadata, "toc_candidate": True}})
        updated.append(block)
    extraction.blocks = updated


def _heading_title(text: str) -> str:
    return " ".join(text.splitlines()[0].split()) if text.strip() else ""


def _valid_heading(title: str) -> bool:
    if not title or len(title) > 240 or len(title.split()) > 24:
        return False
    lowered = title.lower()
    if YEAR_ONLY.fullmatch(title) or lowered.startswith("arxiv:") or "http://" in lowered or "https://" in lowered:
        return False
    if re.fullmatch(r"[\W\d_]+", title):
        return False
    if lowered.startswith(("figure ", "fig. ")):
        return False
    return not (title.endswith(".") and len(title.split()) > 10)


def _valid_layout_heading(title: str) -> bool:
    lowered = title.casefold().strip()
    if len(title) > 140 or len(title.split()) > 16:
        return False
    if lowered.startswith(("def ", "class ", "//", "/*")):
        return False
    alphabetic_words = re.findall(r"[A-Za-z]{2,}", title)
    if len(alphabetic_words) < 2:
        return False
    symbol_count = sum(1 for character in title if not (character.isalnum() or character.isspace() or character in "-:,.()"))
    return symbol_count / max(len(title), 1) < 0.15


def _legal_level(kind: str) -> int:
    return {
        "part": 1,
        "chapter": 2,
        "article": 3,
        "section": 3,
        "clause": 4,
        "paragraph": 4,
        "schedule": 2,
        "appendix": 2,
    }.get(kind.lower(), 3)


def _title_level(title: str) -> int:
    numbered = NUMBERED_HEADING.match(title)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 6)
    legal = LEGAL_HEADING.match(title)
    return _legal_level(legal.group(1)) if legal else 2


def _token_overlap(title: str, text: str) -> float:
    title_terms = _terms(title)
    if not title_terms:
        return 0.0
    return len(title_terms & _terms(text)) / len(title_terms)


def _terms(text: str) -> set[str]:
    return {term.lower() for term in re.findall(r"[A-Za-z0-9]{2,}", text)}


def _deduplicate_candidates(candidates: Sequence[StructureCandidate]) -> list[StructureCandidate]:
    by_block: dict[UUID, StructureCandidate] = {}
    for candidate in candidates:
        existing = by_block.get(candidate.block_id)
        if existing is None or candidate.confidence > existing.confidence:
            by_block[candidate.block_id] = candidate
    return sorted(by_block.values(), key=lambda candidate: (candidate.block_ordinal, candidate.level, candidate.title))
