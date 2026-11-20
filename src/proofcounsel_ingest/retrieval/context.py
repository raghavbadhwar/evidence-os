"""Deduplicated, source-ordered context assembly with a hard token budget."""

from __future__ import annotations

import math
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, Field

from proofcounsel_ingest.models import ContentBlock, RetrievalHit


class ContextBlock(BaseModel):
    block: ContentBlock
    retrieval_rank: int = Field(ge=1)
    source: str


class ContextBundle(BaseModel):
    blocks: list[ContextBlock]
    estimated_tokens: int = Field(ge=0)
    truncated: bool = False


def assemble_context(
    hits: Sequence[RetrievalHit],
    document_blocks: Sequence[ContentBlock],
    *,
    max_blocks: int = 8,
    max_tokens: int = 8000,
    include_neighbors: bool = True,
) -> ContextBundle:
    ordered = sorted(document_blocks, key=lambda block: block.ordinal)
    position_by_id = {block.block_id: index for index, block in enumerate(ordered)}
    selected: list[tuple[ContentBlock, int, str]] = []
    seen: set[UUID] = set()
    for hit in hits:
        position = position_by_id.get(hit.block_id)
        if position is None:
            continue
        positions = [position]
        if include_neighbors:
            positions = [candidate for candidate in (position - 1, position, position + 1) if 0 <= candidate < len(ordered)]
        for candidate_position in positions:
            block = ordered[candidate_position]
            if block.block_id in seen or not block.metadata.get("queryable_evidence", True):
                continue
            seen.add(block.block_id)
            source = "hit" if candidate_position == position else "neighbor"
            selected.append((block, hit.rank, source))

    selected.sort(key=lambda item: (item[1], item[0].ordinal))
    context_blocks: list[ContextBlock] = []
    estimated_tokens = 0
    truncated = False
    for block, rank, source in selected:
        block_tokens = max(1, math.ceil(len(block.text) / 4))
        if len(context_blocks) >= max_blocks or estimated_tokens + block_tokens > max_tokens:
            truncated = True
            continue
        context_blocks.append(ContextBlock(block=block, retrieval_rank=rank, source=source))
        estimated_tokens += block_tokens
    context_blocks.sort(key=lambda item: (str(item.block.document_id), item.block.ordinal))
    return ContextBundle(blocks=context_blocks, estimated_tokens=estimated_tokens, truncated=truncated)
