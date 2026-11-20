from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from proofcounsel_ingest.config import ParserConfig
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    RetrievalHit,
    SourceLocator,
    StructureNode,
)
from proofcounsel_ingest.pageindex.adapter import ReasoningBudget
from proofcounsel_ingest.retrieval.context import assemble_context
from proofcounsel_ingest.retrieval.hybrid import HybridRetriever
from proofcounsel_ingest.retrieval.reference import resolve_reference
from proofcounsel_ingest.retrieval.repository import SearchRecord, SQLiteRetrievalRepository
from proofcounsel_ingest.retrieval.semantic import SemanticIndexDescriptor, require_compatible_index

ROOT = Path(__file__).resolve().parents[2]
TENANT = UUID("00000000-0000-0000-0000-000000000501")
SOURCE_HASH = "5" * 64


def block(document_id: UUID, ordinal: int, text: str) -> ContentBlock:
    return ContentBlock(
        document_id=document_id,
        ordinal=ordinal,
        block_type=BlockType.HEADING if ordinal == 0 else BlockType.PARAGRAPH,
        text=text,
        confidence=1,
        locator=SourceLocator(format=DocumentFormat.TEXT, line_start=ordinal + 1, line_end=ordinal + 1),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        metadata={"queryable_evidence": True, "untrusted_evidence": True},
    )


def corpus() -> tuple[UUID, list[ContentBlock], list[StructureNode]]:
    document_id = uuid4()
    blocks = [
        block(document_id, 0, "Section 4.2(a) Payment Terms"),
        block(document_id, 1, "Invoices are payable in INR before the fifth day of each month."),
        block(document_id, 2, "Late fees require written notice."),
    ]
    nodes = [
        StructureNode(
            document_id=document_id,
            title="Section 4.2(a) Payment Terms",
            kind="section",
            ordinal=0,
            start_block_id=blocks[0].block_id,
            end_block_id=blocks[2].block_id,
            method="test",
            confidence=1,
            verified=True,
        )
    ]
    return document_id, blocks, nodes


def indexed_repository(tmp_path: Path) -> tuple[SQLiteRetrievalRepository, UUID, list[ContentBlock], list[StructureNode]]:
    repository = SQLiteRetrievalRepository(tmp_path / "retrieval.sqlite3")
    document_id, blocks, nodes = corpus()
    repository.index_document(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
        blocks=blocks,
        nodes=nodes,
    )
    return repository, document_id, blocks, nodes


def test_sqlite_repository_reuses_a_thread_connection_and_closes_it(tmp_path: Path) -> None:
    repository = SQLiteRetrievalRepository(tmp_path / "retrieval.sqlite3")

    first = repository._connect()
    second = repository._connect()

    assert first is second
    repository.close()

    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")


def test_sqlite_wal_mode_is_initialized_once_and_persisted(tmp_path: Path) -> None:
    repository = SQLiteRetrievalRepository(tmp_path / "retrieval.sqlite3")

    with repository._connection() as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.casefold() == "wal"


def test_sqlite_document_content_cache_reuses_one_snapshot(tmp_path: Path) -> None:
    repository, document_id, blocks, nodes = indexed_repository(tmp_path)

    assert repository.get_blocks(tenant_id=TENANT, document_id=document_id) == blocks
    assert repository.get_nodes(tenant_id=TENANT, document_id=document_id) == nodes
    assert repository.get_blocks(tenant_id=TENANT, document_id=document_id) == blocks

    assert list(repository._document_content_cache) == [(str(TENANT), str(document_id))]


def test_sqlite_sparse_index_is_reused_for_document_queries(tmp_path: Path) -> None:
    repository, document_id, blocks, _nodes = indexed_repository(tmp_path)

    first = repository.search_blocks(
        tenant_id=TENANT,
        document_id=document_id,
        query="payable invoices",
    )
    cached = repository._document_search_cache[(str(TENANT), str(document_id))]
    second = repository.search_blocks(
        tenant_id=TENANT,
        document_id=document_id,
        query="written notices",
    )

    assert first and first[0].block_id == blocks[1].block_id
    assert second and second[0].block_id == blocks[2].block_id
    assert repository._document_search_cache[(str(TENANT), str(document_id))] is cached


def test_sparse_retrieval_normalizes_inflected_legal_terms(tmp_path: Path) -> None:
    repository = SQLiteRetrievalRepository(tmp_path / "retrieval.sqlite3")
    document_id = uuid4()
    evidence = block(
        document_id,
        0,
        "Termination is permitted when the supplier materially breaches the agreement.",
    )
    node = StructureNode(
        document_id=document_id,
        title="Termination Rights",
        kind="section",
        ordinal=0,
        start_block_id=evidence.block_id,
        end_block_id=evidence.block_id,
        method="test",
        confidence=1,
        verified=True,
    )
    repository.index_document(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="7" * 64,
        blocks=[evidence],
        nodes=[node],
    )

    results = repository.search_blocks(
        tenant_id=TENANT,
        document_id=document_id,
        query="When may the supplier terminate after breaching the agreement?",
    )
    typo_results = repository.search_blocks(
        tenant_id=TENANT,
        document_id=document_id,
        query="terminaton",
    )
    unrelated_results = repository.search_blocks(
        tenant_id=TENANT,
        document_id=document_id,
        query="tenant",
    )

    assert results and results[0].block_id == evidence.block_id
    assert typo_results and typo_results[0].block_id == evidence.block_id
    assert unrelated_results == []


def test_sqlite_document_scoped_search_does_not_scan_global_fts(tmp_path: Path) -> None:
    repository, document_id, blocks, _nodes = indexed_repository(tmp_path)
    with repository._connection() as connection:
        connection.execute("DELETE FROM block_fts")
        connection.execute("DELETE FROM node_fts")

    block_hits = repository.search_blocks(
        tenant_id=TENANT,
        document_id=document_id,
        query="payable INR",
    )
    node_hits = repository.search_nodes(
        tenant_id=TENANT,
        document_id=document_id,
        query="payment terms",
    )

    assert block_hits and block_hits[0].block_id == blocks[1].block_id
    assert node_hits and node_hits[0].block_id == blocks[0].block_id
    assert repository.search_blocks(tenant_id=TENANT, query="payable INR") == []


def test_sqlite_cache_is_invalidated_when_document_is_deleted(tmp_path: Path) -> None:
    repository, document_id, blocks, _nodes = indexed_repository(tmp_path)
    assert repository.get_blocks(tenant_id=TENANT, document_id=document_id) == blocks
    assert repository.has_index(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
    )

    repository.delete_document(tenant_id=TENANT, document_id=document_id)

    assert repository.get_blocks(tenant_id=TENANT, document_id=document_id) == []
    assert not repository.has_index(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
    )


def test_sqlite_repository_closes_worker_thread_connections(tmp_path: Path) -> None:
    repository = SQLiteRetrievalRepository(tmp_path / "retrieval.sqlite3")
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker_connection = executor.submit(repository._connect).result()

    repository.close()

    with pytest.raises(sqlite3.ProgrammingError):
        worker_connection.execute("SELECT 1")


def test_sqlite_fts_is_persistent_tenant_scoped_and_does_not_rebuild_on_query(tmp_path: Path) -> None:
    repository, document_id, blocks, _nodes = indexed_repository(tmp_path)
    assert repository.index_build_count(tenant_id=TENANT, document_id=document_id) == 1

    reopened = SQLiteRetrievalRepository(repository.path)
    first = reopened.search_blocks(tenant_id=TENANT, document_id=document_id, query="payable INR")
    second = reopened.search_blocks(tenant_id=TENANT, document_id=document_id, query="written notice")

    assert first and first[0].block_id == blocks[1].block_id
    assert second and second[0].block_id == blocks[2].block_id
    assert reopened.index_build_count(tenant_id=TENANT, document_id=document_id) == 1
    assert reopened.search_blocks(tenant_id=uuid4(), document_id=document_id, query="payable") == []


def test_immutable_index_descriptor_cannot_change_for_the_same_version(tmp_path: Path) -> None:
    repository, document_id, blocks, nodes = indexed_repository(tmp_path)

    with pytest.raises(ValueError, match="different descriptor"):
        repository.index_document(
            tenant_id=TENANT,
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            config_hash="6" * 64,
            blocks=blocks,
            nodes=nodes,
            index_descriptor={"semantic": {"model": "embed-v2", "dimensions": 1024, "version": "2026-02"}},
        )


def test_index_descriptor_is_read_back_only_for_a_ready_matching_version(tmp_path: Path) -> None:
    repository = SQLiteRetrievalRepository(tmp_path / "retrieval.sqlite3")
    document_id, blocks, nodes = corpus()
    descriptor = {"semantic": {"model": "embed-v1", "dimensions": 768, "version": "2026-01"}}
    repository.index_document(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
        blocks=blocks,
        nodes=nodes,
        publish_ready=False,
        index_descriptor=descriptor,
    )
    assert repository.get_index_descriptor(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
    ) is None
    repository.mark_ready(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
    )
    assert repository.get_index_descriptor(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
    ) == descriptor


def test_index_is_not_queryable_until_explicitly_published(tmp_path: Path) -> None:
    repository = SQLiteRetrievalRepository(tmp_path / "retrieval.sqlite3")
    document_id, blocks, nodes = corpus()
    repository.index_document(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
        blocks=blocks,
        nodes=nodes,
        publish_ready=False,
    )

    assert not repository.has_index(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
    )
    assert repository.search_blocks(tenant_id=TENANT, document_id=document_id, query="payable") == []

    repository.mark_ready(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
    )
    assert repository.has_index(
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config_hash="6" * 64,
    )
    assert repository.search_blocks(tenant_id=TENANT, document_id=document_id, query="payable")


def test_exact_legal_reference_returns_full_verified_node_range_without_model_calls(tmp_path: Path) -> None:
    repository, document_id, blocks, _nodes = indexed_repository(tmp_path)

    class ForbiddenSemantic:
        descriptor = None

        def search(self, query: str, *, limit: int) -> list[SearchRecord]:
            del query, limit
            raise AssertionError("semantic search must not run for an exact reference")

    class ForbiddenReranker:
        def rerank(self, query: str, candidates: list[SearchRecord], *, limit: int):  # type: ignore[no-untyped-def]
            del query, candidates, limit
            raise AssertionError("reranker must not run for an exact reference")

    result = HybridRetriever(
        repository,
        semantic=ForbiddenSemantic(),
        reranker=ForbiddenReranker(),
    ).retrieve(
        "What does Section 4.2(a) require?",
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config=ParserConfig.load(ROOT / "config" / "parser-defaults.toml").retrieval,
        semantic_enabled=True,
    )

    assert [hit.block_id for hit in result.hits] == [item.block_id for item in blocks]
    assert all(hit.channel == "exact_reference" for hit in result.hits)
    assert result.telemetry.tree_count == 0
    assert result.telemetry.lexical_count == 0
    assert not result.telemetry.semantic_attempted
    assert result.telemetry.reranker is None


def test_tree_routing_expands_a_matching_node_into_its_body(tmp_path: Path) -> None:
    repository, document_id, blocks, _nodes = indexed_repository(tmp_path)
    result = HybridRetriever(repository).retrieve(
        "Payment Terms",
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config=ParserConfig.load(ROOT / "config" / "parser-defaults.toml").retrieval,
    )

    returned = {hit.block_id for hit in result.hits}
    assert blocks[0].block_id in returned
    assert blocks[1].block_id in returned
    assert blocks[2].block_id in returned
    assert any("fifth day" in hit.text for hit in result.hits)
    assert result.telemetry.tree_count >= 3


def test_budgeted_pageindex_reasoner_recovers_semantic_section_queries(tmp_path: Path) -> None:
    repository, document_id, _blocks, nodes = indexed_repository(tmp_path)

    class FakeReasoner:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, query: str, tree: list[dict[str, object]], budget: ReasoningBudget) -> list[str]:
            assert query == "How are bills settled?"
            assert tree and budget.max_calls == 1
            self.calls += 1
            return [str(nodes[0].node_id)]

    reasoner = FakeReasoner()
    result = HybridRetriever(repository, tree_reasoner=reasoner).retrieve(
        "How are bills settled?",
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config=ParserConfig.load(ROOT / "config" / "parser-defaults.toml").retrieval,
        tree_reasoning_enabled=True,
    )

    assert reasoner.calls == 1
    assert result.telemetry.tree_reasoning_attempted
    assert result.telemetry.tree_reasoning_count >= 3
    assert not result.telemetry.semantic_attempted
    assert any("fifth day" in hit.text for hit in result.hits)


def test_deterministic_query_does_not_pay_for_pageindex_reasoning(tmp_path: Path) -> None:
    repository, document_id, _blocks, _nodes = indexed_repository(tmp_path)

    class ForbiddenReasoner:
        def search(self, query: str, tree: list[dict[str, object]], budget: ReasoningBudget) -> list[str]:
            del query, tree, budget
            raise AssertionError("deterministic retrieval must not call the reasoner")

    result = HybridRetriever(repository, tree_reasoner=ForbiddenReasoner()).retrieve(
        "Payment Terms",
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config=ParserConfig.load(ROOT / "config" / "parser-defaults.toml").retrieval,
        tree_reasoning_enabled=True,
    )

    assert result.hits
    assert not result.telemetry.tree_reasoning_attempted


def test_pageindex_reasoner_output_is_filtered_to_verified_nodes(tmp_path: Path) -> None:
    repository, document_id, _blocks, _nodes = indexed_repository(tmp_path)

    class UntrustedReasoner:
        def search(self, query: str, tree: list[dict[str, object]], budget: ReasoningBudget) -> list[str]:
            del query, tree, budget
            return ["untrusted-node-id"]

    result = HybridRetriever(repository, tree_reasoner=UntrustedReasoner()).retrieve(
        "How are bills settled?",
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        config=ParserConfig.load(ROOT / "config" / "parser-defaults.toml").retrieval,
        tree_reasoning_enabled=True,
    )

    assert result.telemetry.tree_reasoning_attempted
    assert result.telemetry.tree_reasoning_count == 0
    assert all("tree_reasoning" not in hit.channel for hit in result.hits)


def test_reference_resolver_handles_sheet_ranges_pages_slides_and_exhibits() -> None:
    document_id = uuid4()
    xlsx = ContentBlock(
        document_id=document_id,
        ordinal=0,
        block_type=BlockType.TABLE_ROW,
        text="Budget 35000",
        confidence=1,
        locator=SourceLocator(format=DocumentFormat.XLSX, part="Pricing Model", cell_range="A1:C10"),
        text_sha256=hashlib.sha256(b"Budget 35000").hexdigest(),
    )
    result = resolve_reference(
        "Read 'Pricing Model'!B5",
        tenant_id=TENANT,
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        blocks=[xlsx],
        nodes=[],
    )
    assert result and result[0].block_id == xlsx.block_id


def test_context_selects_by_relevance_then_emits_source_order_and_enforces_budget() -> None:
    document_id, blocks, _nodes = corpus()
    hits = [
        RetrievalHit(
            tenant_id=TENANT,
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            block_id=blocks[2].block_id,
            channel="lexical",
            raw_score=1,
            fused_score=1,
            rank=1,
            locator=blocks[2].locator,
            text=blocks[2].text,
        ),
        RetrievalHit(
            tenant_id=TENANT,
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            block_id=blocks[0].block_id,
            channel="tree",
            raw_score=0.5,
            fused_score=0.5,
            rank=2,
            locator=blocks[0].locator,
            text=blocks[0].text,
        ),
    ]
    context = assemble_context(hits, blocks, max_blocks=2, max_tokens=100, include_neighbors=False)
    assert [item.block.ordinal for item in context.blocks] == [0, 2]
    assert context.estimated_tokens > 0

    constrained = assemble_context(hits, blocks, max_blocks=1, max_tokens=2, include_neighbors=False)
    assert constrained.blocks == []
    assert constrained.truncated


def test_semantic_index_versions_must_match_exactly() -> None:
    expected = SemanticIndexDescriptor(model="embed-v1", dimensions=768, version="2026-01")
    require_compatible_index(expected, expected.model_copy())
    with pytest.raises(ValueError, match="version mismatch"):
        require_compatible_index(
            expected,
            SemanticIndexDescriptor(model="embed-v2", dimensions=1024, version="2026-02"),
        )


def test_semantic_retrieval_rejects_an_adapter_with_the_wrong_index_descriptor(tmp_path: Path) -> None:
    repository, document_id, _blocks, _nodes = indexed_repository(tmp_path)

    class WrongVersionSemantic:
        descriptor = SemanticIndexDescriptor(model="embed-v2", dimensions=1024, version="2026-02")

        def search(self, query: str, *, limit: int) -> list[SearchRecord]:
            del query, limit
            raise AssertionError("descriptor validation should happen before semantic search")

    with pytest.raises(ValueError, match="semantic index version mismatch"):
        HybridRetriever(repository, semantic=WrongVersionSemantic()).retrieve(
            "query with no lexical match",
            tenant_id=TENANT,
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            config=ParserConfig.load(ROOT / "config/parser-defaults.toml").retrieval,
            semantic_enabled=True,
            expected_semantic_descriptor=SemanticIndexDescriptor(
                model="embed-v1", dimensions=768, version="2026-01"
            ),
        )
