from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.models import BlockType, ContentBlock, DocumentFormat, SourceLocator, StructureNode
from proofcounsel_ingest.orchestrator import ingest
from proofcounsel_ingest.retrieval.hybrid import HybridRetriever
from proofcounsel_ingest.retrieval.repository import SQLiteRetrievalRepository

TENANT = UUID("00000000-0000-0000-0000-000000001111")
SOURCE_HASH = "a" * 64


def _block(document_id: UUID, ordinal: int) -> ContentBlock:
    text = f"Clause {ordinal}: ordinary evidence with target marker {ordinal}."
    return ContentBlock(
        document_id=document_id,
        ordinal=ordinal,
        block_type=BlockType.HEADING if ordinal == 0 else BlockType.PARAGRAPH,
        text=text,
        confidence=1,
        locator=SourceLocator(format=DocumentFormat.TEXT, line_start=ordinal + 1, line_end=ordinal + 1),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


@pytest.mark.load
def test_thousand_block_index_is_built_once_and_retrieved_without_rebuild(tmp_path: Path) -> None:
    repository = SQLiteRetrievalRepository(tmp_path / "retrieval.sqlite3")
    document_id = uuid4()
    blocks = [_block(document_id, ordinal) for ordinal in range(1000)]
    node = StructureNode(
        document_id=document_id,
        title="Evidence Index",
        kind="section",
        ordinal=0,
        start_block_id=blocks[0].block_id,
        end_block_id=blocks[-1].block_id,
        method="synthetic-load",
        confidence=1,
        verified=True,
    )
    repository.index_document(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="b" * 64,
        blocks=blocks,
        nodes=[node],
    )
    retriever = HybridRetriever(repository)
    result = retriever.retrieve(
        "target marker 777",
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config=ParserConfig.load(Path("config/parser-defaults.toml")).retrieval,
    )
    assert any("target marker 777" in hit.text for hit in result.hits)
    assert repository.index_build_count(tenant_id=TENANT, document_id=document_id) == 1


@pytest.mark.integration
def test_concurrent_duplicate_ingestion_publishes_one_index(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        tenant_id=TENANT,
        artifact_root=tmp_path / "artifacts",
        parser_config=Path("config/parser-defaults.toml").resolve(),
    )
    source = Path("tests/fixtures/adversarial.html").resolve()
    with ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(lambda _: ingest(source, settings=settings), range(3)))
    assert all(outcome.status.value == "ready" for outcome in outcomes)
    repository = SQLiteRetrievalRepository(settings.artifact_root / "_indexes" / "retrieval.sqlite3")
    assert repository.index_build_count(tenant_id=TENANT, document_id=outcomes[0].identity.document_id) == 1
