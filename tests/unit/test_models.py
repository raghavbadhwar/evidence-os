from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from proofcounsel_ingest.invariants import InvariantViolation, validate_queryable, validate_structure_ranges
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    IndexVersion,
    IngestionRun,
    IngestionStatus,
    QualityReport,
    SourceLocator,
    StructureNode,
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def pdf_locator(page: int = 1) -> SourceLocator:
    return SourceLocator(format=DocumentFormat.PDF, page=page, bbox=(0, 0, 100, 100))


def test_pdf_locator_requires_page_and_bbox() -> None:
    with pytest.raises(ValidationError, match="page and bbox"):
        SourceLocator(format=DocumentFormat.PDF, page=1)


def test_text_locator_rejects_inverted_lines() -> None:
    with pytest.raises(ValidationError, match="ordered line range"):
        SourceLocator(format=DocumentFormat.TEXT, line_start=10, line_end=2)


def test_failed_run_requires_failure_record() -> None:
    with pytest.raises(ValidationError, match="failure details"):
        IngestionRun(
            document_id=uuid4(),
            status=IngestionStatus.FAILED,
            extractor_version="test",
            parser_config_hash="0" * 64,
            finished_at=datetime.now(UTC),
        )


def test_structure_range_rejects_missing_block() -> None:
    document_id = uuid4()
    block = ContentBlock(
        document_id=document_id,
        ordinal=0,
        block_type=BlockType.PARAGRAPH,
        text="evidence",
        confidence=1,
        locator=pdf_locator(),
        text_sha256=digest("evidence"),
    )
    node = StructureNode(
        document_id=document_id,
        title="Missing range",
        kind="section",
        ordinal=0,
        start_block_id=block.block_id,
        end_block_id=uuid4(),
        method="test",
        confidence=1,
        verified=True,
    )
    with pytest.raises(InvariantViolation, match="missing block"):
        validate_structure_ranges([node], [block])


def test_ready_requires_blocks_indexes_structure_and_full_locators() -> None:
    quality = QualityReport(
        page_coverage=1,
        nonempty_block_count=0,
        locator_coverage=1,
        language_coverage=1,
        structure_validity=1,
        indexes_persisted=False,
        queryable=True,
    )
    with pytest.raises(InvariantViolation, match="content block"):
        validate_queryable(
            status=IngestionStatus.READY,
            quality=quality,
            blocks=[],
            nodes=[],
            index_versions=[],
            minimum_page_coverage=0.995,
        )


def test_valid_ready_evidence_passes_invariants() -> None:
    document_id = uuid4()
    source_hash = "1" * 64
    block = ContentBlock(
        document_id=document_id,
        ordinal=0,
        block_type=BlockType.PARAGRAPH,
        text="verified evidence",
        confidence=1,
        locator=pdf_locator(),
        text_sha256=digest("verified evidence"),
    )
    node = StructureNode(
        document_id=document_id,
        title="Evidence",
        kind="fallback",
        ordinal=0,
        start_block_id=block.block_id,
        end_block_id=block.block_id,
        method="explicit_fallback",
        confidence=1,
        verified=True,
    )
    quality = QualityReport(
        page_coverage=1,
        nonempty_block_count=1,
        locator_coverage=1,
        language_coverage=1,
        structure_validity=1,
        indexes_persisted=True,
        queryable=True,
    )
    index = IndexVersion(
        document_id=document_id,
        source_sha256=source_hash,
        index_type="lexical",
        implementation="test",
        config_hash="2" * 64,
    )
    validate_queryable(
        status=IngestionStatus.READY,
        quality=quality,
        blocks=[block],
        nodes=[node],
        index_versions=[index],
        minimum_page_coverage=0.995,
    )
