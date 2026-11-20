"""Exact-reference, tree, lexical, optional semantic, and bounded reranking pipeline."""

from __future__ import annotations

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from pydantic import BaseModel, Field

from proofcounsel_ingest.config import RetrievalConfig
from proofcounsel_ingest.models import ContentBlock, RetrievalHit, StructureNode
from proofcounsel_ingest.pageindex.adapter import (
    PageIndexReasonerPort,
    ReasoningBudget,
    to_pageindex_payload,
)
from proofcounsel_ingest.retrieval.reference import resolve_reference
from proofcounsel_ingest.retrieval.repository import RetrievalRepository, SearchRecord
from proofcounsel_ingest.retrieval.reranker import (
    LexicalOverlapReranker,
    RerankerPort,
    RerankTelemetry,
)
from proofcounsel_ingest.retrieval.semantic import (
    DisabledSemanticSearch,
    SemanticIndexDescriptor,
    SemanticSearchPort,
    require_compatible_index,
)


class RetrievalTelemetry(BaseModel):
    elapsed_ms: float = Field(ge=0)
    exact_count: int = Field(ge=0)
    tree_count: int = Field(ge=0)
    lexical_count: int = Field(ge=0)
    semantic_count: int = Field(ge=0)
    fused_count: int = Field(ge=0)
    semantic_attempted: bool = False
    tree_reasoning_attempted: bool = False
    tree_reasoning_count: int = Field(ge=0)
    reranker: RerankTelemetry | None = None


class RetrievalResult(BaseModel):
    query: str
    hits: list[RetrievalHit]
    telemetry: RetrievalTelemetry


class HybridRetriever:
    def __init__(
        self,
        repository: RetrievalRepository,
        *,
        semantic: SemanticSearchPort | None = None,
        reranker: RerankerPort | None = None,
        tree_reasoner: PageIndexReasonerPort | None = None,
        tree_reasoning_budget: ReasoningBudget | None = None,
    ) -> None:
        self.repository = repository
        self.semantic = semantic or DisabledSemanticSearch()
        self.reranker = reranker or LexicalOverlapReranker()
        self.tree_reasoner = tree_reasoner
        self.tree_reasoning_budget = tree_reasoning_budget or ReasoningBudget()

    def retrieve(
        self,
        query: str,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config: RetrievalConfig,
        semantic_enabled: bool = False,
        expected_semantic_descriptor: SemanticIndexDescriptor | None = None,
        tree_reasoning_enabled: bool = False,
    ) -> RetrievalResult:
        started = time.perf_counter()
        blocks = self.repository.get_blocks(tenant_id=tenant_id, document_id=document_id)
        nodes = self.repository.get_nodes(tenant_id=tenant_id, document_id=document_id)
        block_by_id = {block.block_id: block for block in blocks}
        exact = resolve_reference(
            query,
            tenant_id=tenant_id,
            document_id=document_id,
            source_sha256=source_sha256,
            blocks=blocks,
            nodes=nodes,
            limit=config.fused_candidate_limit,
        )
        tree: list[SearchRecord] = []
        lexical: list[SearchRecord] = []
        if not exact:
            if self.repository.implementation == "sqlite-fts5":
                # A short-lived executor would allocate a new thread-local
                # SQLite connection for every query. Keep the local pilot on
                # one persistent connection; PostgreSQL still fans out below.
                tree = self.repository.search_nodes(
                    tenant_id=tenant_id,
                    query=query,
                    document_id=document_id,
                    limit=config.reranker_candidate_limit,
                )
                lexical = self.repository.search_blocks(
                    tenant_id=tenant_id,
                    query=query,
                    document_id=document_id,
                    limit=config.fused_candidate_limit,
                )
            else:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    tree_future = executor.submit(
                        self.repository.search_nodes,
                        tenant_id=tenant_id,
                        query=query,
                        document_id=document_id,
                        limit=config.reranker_candidate_limit,
                    )
                    lexical_future = executor.submit(
                        self.repository.search_blocks,
                        tenant_id=tenant_id,
                        query=query,
                        document_id=document_id,
                        limit=config.fused_candidate_limit,
                    )
                    tree = tree_future.result()
                    lexical = lexical_future.result()

            # A tree hit is a routing decision, not evidence by itself. Expand
            # the selected verified node into a bounded set of its source blocks
            # before fusion so PageIndex navigation can reach answer-bearing text.
            tree = _expand_tree_hits(
                tree,
                nodes,
                blocks,
                limit_per_node=config.tree_range_block_limit,
            )

        deterministic_confidence = 1.0 if exact else max(
            [record.raw_score for record in [*tree, *lexical]], default=0.0
        )
        deterministic_sufficient = deterministic_confidence >= config.semantic_fallback_threshold
        tree_reasoning_attempted = (
            tree_reasoning_enabled
            and self.tree_reasoner is not None
            and not deterministic_sufficient
        )
        tree_reasoning = (
            _reason_over_verified_tree(
                query,
                tenant_id=tenant_id,
                document_id=document_id,
                source_sha256=source_sha256,
                nodes=nodes,
                blocks=blocks,
                reasoner=self.tree_reasoner,
                budget=self.tree_reasoning_budget,
            )
            if tree_reasoning_attempted
            else []
        )
        semantic_attempted = semantic_enabled and not deterministic_sufficient and not tree_reasoning
        if semantic_attempted and expected_semantic_descriptor is not None:
            actual_descriptor = self.semantic.descriptor
            if actual_descriptor is None:
                raise ValueError("semantic retrieval adapter has no index descriptor")
            require_compatible_index(expected_semantic_descriptor, actual_descriptor)
        semantic_results = (
            self.semantic.search(query, limit=config.fused_candidate_limit) if semantic_attempted else []
        )
        semantic = [
            record
            for record in semantic_results
            if record.tenant_id == tenant_id
            and record.document_id == document_id
            and record.source_sha256 == source_sha256
        ]
        fused = _rrf([exact, tree, lexical, semantic, tree_reasoning], config.fused_candidate_limit)

        rerank_telemetry = None
        if not deterministic_sufficient and fused:
            reranked = self.reranker.rerank(
                query,
                fused[: config.reranker_candidate_limit],
                limit=config.reranker_candidate_limit,
            )
            rerank_telemetry = reranked.telemetry
            reranked_by_id = {record.block_id: record for record in reranked.records}
            fused = [reranked_by_id.get(record.block_id, record) for record in fused]
            fused.sort(key=lambda record: record.raw_score, reverse=True)

        hits: list[RetrievalHit] = []
        channels_by_id: dict[UUID, set[str]] = defaultdict(set)
        node_by_id: dict[UUID, UUID] = {}
        for channel_records in (exact, tree, lexical, semantic, tree_reasoning):
            for record in channel_records:
                channels_by_id[record.block_id].add(record.channel)
                if record.node_id is not None:
                    node_by_id[record.block_id] = record.node_id
        for rank, record in enumerate(fused[: config.fused_candidate_limit], start=1):
            block = block_by_id.get(record.block_id)
            if block is None:
                continue
            hits.append(
                RetrievalHit(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    block_id=block.block_id,
                    node_id=node_by_id.get(block.block_id),
                    channel="+".join(sorted(channels_by_id[block.block_id])),
                    raw_score=record.raw_score,
                    fused_score=record.raw_score,
                    rank=rank,
                    locator=block.locator,
                    text=block.text,
                )
            )
        return RetrievalResult(
            query=query,
            hits=hits,
            telemetry=RetrievalTelemetry(
                elapsed_ms=(time.perf_counter() - started) * 1000,
                exact_count=len(exact),
                tree_count=len(tree),
                lexical_count=len(lexical),
                semantic_count=len(semantic),
                fused_count=len(fused),
                semantic_attempted=semantic_attempted,
                tree_reasoning_attempted=tree_reasoning_attempted,
                tree_reasoning_count=len(tree_reasoning),
                reranker=rerank_telemetry,
            ),
        )


def _reason_over_verified_tree(
    query: str,
    *,
    tenant_id: UUID,
    document_id: UUID,
    source_sha256: str,
    nodes: list[StructureNode],
    blocks: list[ContentBlock],
    reasoner: PageIndexReasonerPort | None,
    budget: ReasoningBudget,
) -> list[SearchRecord]:
    if reasoner is None or not nodes or not blocks or budget.max_calls <= 0:
        return []
    payload = to_pageindex_payload("retrieval-document", nodes, blocks)
    selected_ids = reasoner.search(query, payload["structure"][: budget.max_nodes], budget)
    selected = {str(node_id) for node_id in selected_ids[: budget.max_nodes]}
    block_by_id = {block.block_id: block for block in blocks}
    records: list[SearchRecord] = []
    for node in nodes:
        if str(node.node_id) not in selected or not node.verified:
            continue
        block = block_by_id.get(node.start_block_id)
        if block is None or not block.metadata.get("queryable_evidence", True):
            continue
        records.append(
            SearchRecord(
                tenant_id=tenant_id,
                document_id=document_id,
                source_sha256=source_sha256,
                block_id=block.block_id,
                node_id=node.node_id,
                ordinal=block.ordinal,
                text=block.text,
                raw_score=0.9,
                channel="tree_reasoning",
            )
        )
    return _expand_tree_hits(records, nodes, blocks, limit_per_node=budget.max_nodes)


def _rrf(channels: list[list[SearchRecord]], limit: int, k: int = 60) -> list[SearchRecord]:
    scores: dict[UUID, float] = defaultdict(float)
    records: dict[UUID, SearchRecord] = {}
    for channel in channels:
        for rank, record in enumerate(channel, start=1):
            boost = 1.0 if record.channel == "exact_reference" else 0.0
            scores[record.block_id] += boost + 1.0 / (k + rank)
            records[record.block_id] = record
    ordered = sorted(scores, key=lambda block_id: scores[block_id], reverse=True)[:limit]
    return [records[block_id].model_copy(update={"raw_score": scores[block_id]}) for block_id in ordered]


def _expand_tree_hits(
    tree_hits: list[SearchRecord],
    nodes: list[StructureNode],
    blocks: list[ContentBlock],
    *,
    limit_per_node: int,
) -> list[SearchRecord]:
    """Turn matched structure nodes into bounded block candidates.

    The repository's node search intentionally returns the node's first block
    for a cheap title lookup. Without this expansion, a matching section title
    can never route retrieval to the section body. The expansion is bounded
    per node to prevent a large chapter from flooding fusion and context.
    """
    if not tree_hits or not nodes or not blocks:
        return tree_hits
    position_by_id = {block.block_id: position for position, block in enumerate(blocks)}
    node_by_id = {node.node_id: node for node in nodes}
    expanded: dict[UUID, SearchRecord] = {}
    for hit in tree_hits:
        node = node_by_id.get(hit.node_id) if hit.node_id is not None else None
        if node is None:
            expanded[hit.block_id] = hit
            continue
        start = position_by_id.get(node.start_block_id)
        end = position_by_id.get(node.end_block_id)
        if start is None or end is None or end < start:
            expanded[hit.block_id] = hit
            continue
        candidates = [
            block
            for block in blocks[start : end + 1]
            if block.metadata.get("queryable_evidence", True)
        ][:limit_per_node]
        for block in candidates:
            expanded.setdefault(
                block.block_id,
                hit.model_copy(
                    update={
                        "block_id": block.block_id,
                        "ordinal": block.ordinal,
                        "text": block.text,
                    }
                ),
            )
    return list(expanded.values())
