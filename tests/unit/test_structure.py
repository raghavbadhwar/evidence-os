from __future__ import annotations

import hashlib
from uuid import uuid4

from proofcounsel_ingest.extractors.base import ExtractionResult
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    PageRecord,
    SourceLocator,
)
from proofcounsel_ingest.structure.tree import build_verified_tree


def block(document_id, ordinal: int, text: str, block_type: BlockType = BlockType.PARAGRAPH):  # type: ignore[no-untyped-def]
    return ContentBlock(
        document_id=document_id,
        ordinal=ordinal,
        block_type=block_type,
        text=text,
        confidence=1,
        locator=SourceLocator(format=DocumentFormat.PDF, page=ordinal + 1, bbox=(0, 0, 100, 100)),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        metadata={"queryable_evidence": True},
    )


def test_toc_candidates_remain_in_corpus_and_are_not_destructively_filtered() -> None:
    document_id = uuid4()
    blocks = [
        block(
            document_id,
            0,
            "Contents\nIntroduction ........ 2\nMethods ........ 3\nResults ........ 4",
        ),
        block(document_id, 1, "Introduction", BlockType.HEADING),
        block(document_id, 2, "Methods", BlockType.HEADING),
        block(document_id, 3, "Results", BlockType.HEADING),
    ]
    extraction = ExtractionResult(
        format=DocumentFormat.PDF,
        blocks=blocks,
        pages=[PageRecord(document_id=document_id, page_number=1, native_text_coverage=1)],
        expected_units=4,
        extracted_units=4,
    )
    original_ids = [item.block_id for item in extraction.blocks]
    nodes, report = build_verified_tree(extraction, minimum_confidence=0.6)

    assert report.valid
    assert nodes
    assert [item.block_id for item in extraction.blocks] == original_ids
    assert extraction.blocks[0].metadata["toc_candidate"] is True
    assert extraction.blocks[0].metadata["queryable_evidence"] is True


def test_unreliable_structure_falls_back_explicitly() -> None:
    document_id = uuid4()
    evidence = block(document_id, 0, "ordinary evidence paragraph")
    extraction = ExtractionResult(
        format=DocumentFormat.PDF,
        blocks=[evidence],
        pages=[PageRecord(document_id=document_id, page_number=1, native_text_coverage=1)],
        expected_units=1,
        extracted_units=1,
    )
    nodes, report = build_verified_tree(extraction, minimum_confidence=0.6)
    assert report.valid
    assert len(nodes) == 1
    assert nodes[0].method == "explicit_fallback"
    assert nodes[0].verified

