"""Deterministic graders for the local ProofCounsel benchmark."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from proofcounsel_ingest.models import ContentBlock, RetrievalHit


def hit_matches(hit: RetrievalHit, expected_texts: Sequence[str]) -> bool:
    haystack = " ".join(hit.text.casefold().split())
    return any(" ".join(expected.casefold().split()) in haystack for expected in expected_texts)


def recall_at_k(hits: Sequence[RetrievalHit], expected_texts: Sequence[str], k: int) -> float:
    return 1.0 if any(hit_matches(hit, expected_texts) for hit in hits[:k]) else 0.0


def first_matching_rank(hits: Sequence[RetrievalHit], expected_texts: Sequence[str]) -> int | None:
    for hit in hits:
        if hit_matches(hit, expected_texts):
            return hit.rank
    return None


def exact_citation_precision(
    citations: Sequence[dict[str, Any]],
    blocks: Sequence[ContentBlock],
) -> float:
    if not citations:
        return 0.0
    by_id = {str(block.block_id): block for block in blocks}
    verified = 0
    for citation in citations:
        block = by_id.get(str(citation.get("block_id")))
        if block is None:
            continue
        if citation.get("locator") == block.locator.model_dump(mode="json"):
            verified += 1
    return verified / len(citations)
