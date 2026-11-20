from __future__ import annotations

from uuid import UUID

from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.pageindex.adapter import PageIndexReasonerPort, ReasoningBudget
from proofcounsel_ingest.retrieval.context import ContextBundle, assemble_context
from proofcounsel_ingest.retrieval.hybrid import HybridRetriever, RetrievalResult
from proofcounsel_ingest.retrieval.repository import RetrievalRepository, repository_for_settings
from proofcounsel_ingest.retrieval.reranker import RerankerPort
from proofcounsel_ingest.retrieval.semantic import (
    SemanticIndexDescriptor,
    SemanticSearchPort,
    require_compatible_index,
)
from proofcounsel_ingest.telemetry import NullTelemetrySink, TelemetryEvent, TelemetrySink, stage_span


def retrieve_document(
    query: str,
    *,
    tenant_id: UUID,
    document_id: UUID,
    source_sha256: str,
    settings: Settings,
    parser_config: ParserConfig | None = None,
    semantic: SemanticSearchPort | None = None,
    reranker: RerankerPort | None = None,
    telemetry_sink: TelemetrySink | None = None,
    repository: RetrievalRepository | None = None,
    tree_reasoner: PageIndexReasonerPort | None = None,
    tree_reasoning_budget: ReasoningBudget | None = None,
) -> tuple[RetrievalResult, ContextBundle]:
    config = parser_config or ParserConfig.load(settings.parser_config)
    sink = telemetry_sink or NullTelemetrySink()
    with stage_span(sink, "retrieval", {"query": query}):
        active_repository = repository or repository_for_settings(settings)
        if not active_repository.has_index(
            tenant_id=tenant_id,
            document_id=document_id,
            source_sha256=source_sha256,
        ):
            raise ValueError("requested document version is not indexed")
        retriever = HybridRetriever(
            active_repository,
            semantic=semantic,
            reranker=reranker,
            tree_reasoner=tree_reasoner,
            tree_reasoning_budget=tree_reasoning_budget,
        )
        expected_descriptor = settings.semantic_index_descriptor()
        expected_semantic_descriptor = (
            SemanticIndexDescriptor.model_validate(expected_descriptor)
            if expected_descriptor is not None
            else None
        )
        if settings.semantic_retrieval_enabled and expected_semantic_descriptor is not None:
            stored_descriptor = active_repository.get_index_descriptor(
                tenant_id=tenant_id,
                document_id=document_id,
                source_sha256=source_sha256,
            )
            stored_semantic = (stored_descriptor or {}).get("semantic")
            if stored_semantic is None:
                raise ValueError("indexed document has no compatible semantic descriptor")
            require_compatible_index(
                expected_semantic_descriptor,
                SemanticIndexDescriptor.model_validate(stored_semantic),
            )
        result = retriever.retrieve(
            query,
            tenant_id=tenant_id,
            document_id=document_id,
            source_sha256=source_sha256,
            config=config.retrieval,
            semantic_enabled=settings.semantic_retrieval_enabled,
            expected_semantic_descriptor=expected_semantic_descriptor,
            tree_reasoning_enabled=settings.llm_tree_reasoning_enabled,
        )
        blocks = active_repository.get_blocks(tenant_id=tenant_id, document_id=document_id)
        context = assemble_context(
            result.hits,
            blocks,
            max_blocks=config.retrieval.answer_context_block_limit,
        )
    reranker_cost = result.telemetry.reranker.cost if result.telemetry.reranker else 0.0
    sink.emit(
        TelemetryEvent(
            tenant_id=tenant_id,
            document_id=document_id,
            source_sha256=source_sha256,
            stage="retrieval",
            status="ok",
            elapsed_ms=result.telemetry.elapsed_ms,
            input_tokens=context.estimated_tokens,
            provider_cost=reranker_cost,
            metadata={
                "query": query,
                "exact_count": result.telemetry.exact_count,
                "tree_count": result.telemetry.tree_count,
                "lexical_count": result.telemetry.lexical_count,
                "semantic_count": result.telemetry.semantic_count,
                "semantic_attempted": result.telemetry.semantic_attempted,
                "tree_reasoning_attempted": result.telemetry.tree_reasoning_attempted,
                "tree_reasoning_count": result.telemetry.tree_reasoning_count,
                "context_blocks": len(context.blocks),
                "context_truncated": context.truncated,
                "reranker_implementation": result.telemetry.reranker.implementation
                if result.telemetry.reranker
                else None,
                "reranker_version": result.telemetry.reranker.version if result.telemetry.reranker else None,
                "reranker_candidates": result.telemetry.reranker.candidate_count
                if result.telemetry.reranker
                else 0,
                "reranker_input_tokens": result.telemetry.reranker.input_tokens
                if result.telemetry.reranker
                else 0,
                "reranker_output_tokens": result.telemetry.reranker.output_tokens
                if result.telemetry.reranker
                else 0,
            },
        )
    )
    return result, context
