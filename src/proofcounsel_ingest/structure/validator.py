"""Structural integrity and title/source validation."""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, Field

from proofcounsel_ingest.invariants import InvariantViolation, validate_structure_ranges
from proofcounsel_ingest.models import ContentBlock, StructureNode


class StructureValidationReport(BaseModel):
    valid: bool
    node_count: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def validate_structure(
    nodes: Sequence[StructureNode],
    blocks: Sequence[ContentBlock],
    *,
    minimum_confidence: float,
) -> StructureValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    if not nodes:
        errors.append("structure contains no nodes")
        return StructureValidationReport(valid=False, node_count=0, errors=errors)
    try:
        validate_structure_ranges(nodes, blocks)
    except InvariantViolation as exc:
        errors.append(str(exc))

    block_by_id = {block.block_id: block for block in blocks}
    seen: set[tuple[str, object]] = set()
    for node in nodes:
        if node.confidence < minimum_confidence:
            errors.append(f"node {node.node_id} confidence is below threshold")
        key = (node.title.casefold(), node.start_block_id)
        if key in seen:
            errors.append(f"duplicate structure node at source block: {node.title}")
        seen.add(key)
        start = block_by_id.get(node.start_block_id)
        if start is None:
            continue
        overlap = _token_overlap(node.title, start.text)
        if overlap < 0.40 and node.method not in {"pdf_bookmark", "explicit_fallback"}:
            errors.append(f"node title does not agree with source block: {node.title}")
        elif overlap < 0.70 and node.method != "explicit_fallback":
            warnings.append(f"weak title/source agreement: {node.title}")
    return StructureValidationReport(
        valid=not errors,
        node_count=len(nodes),
        errors=errors,
        warnings=warnings,
    )


def _token_overlap(title: str, text: str) -> float:
    title_terms = {term.lower() for term in re.findall(r"[A-Za-z0-9]{2,}", title)}
    text_terms = {term.lower() for term in re.findall(r"[A-Za-z0-9]{2,}", text)}
    return len(title_terms & text_terms) / max(len(title_terms), 1)
