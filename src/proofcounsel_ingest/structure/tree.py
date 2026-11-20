"""Build a verified hierarchy or an explicit, non-deceptive fallback."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid5

from proofcounsel_ingest.extractors.base import ExtractionResult
from proofcounsel_ingest.models import ContentBlock, StructureNode
from proofcounsel_ingest.structure.candidates import StructureCandidate, collect_candidates
from proofcounsel_ingest.structure.repair import StructureRepairBudget, StructureRepairPort
from proofcounsel_ingest.structure.validator import StructureValidationReport, validate_structure


def build_verified_tree(
    extraction: ExtractionResult,
    *,
    minimum_confidence: float,
    repairer: StructureRepairPort | None = None,
    repair_budget: StructureRepairBudget | None = None,
) -> tuple[list[StructureNode], StructureValidationReport]:
    candidates = collect_candidates(extraction)
    queryable_blocks = sorted(
        (block for block in extraction.blocks if block.metadata.get("queryable_evidence", True)),
        key=lambda block: block.ordinal,
    )
    nodes = _nodes_from_candidates(candidates, queryable_blocks)
    if nodes:
        report = validate_structure(nodes, extraction.blocks, minimum_confidence=minimum_confidence)
        if report.valid:
            return [node.model_copy(update={"verified": True}) for node in nodes], report

    if repairer is not None:
        budget = repair_budget or StructureRepairBudget()
        repaired = repairer.repair(extraction, candidates[: budget.max_candidates], budget)
        candidates = [*candidates, *repaired[: budget.max_candidates]]
        nodes = _nodes_from_candidates(candidates, queryable_blocks)
        if nodes:
            report = validate_structure(nodes, extraction.blocks, minimum_confidence=minimum_confidence)
            if report.valid:
                return [node.model_copy(update={"verified": True}) for node in nodes], report

    fallback = build_explicit_fallback(queryable_blocks)
    report = validate_structure(fallback, extraction.blocks, minimum_confidence=minimum_confidence)
    if not report.valid:
        return [], report
    warnings = [*report.warnings]
    if candidates:
        warnings.append("candidate hierarchy failed validation; explicit fallback was used")
    report = report.model_copy(update={"warnings": warnings})
    return fallback, report


def build_explicit_fallback(blocks: Sequence[ContentBlock]) -> list[StructureNode]:
    queryable = sorted(
        (block for block in blocks if block.metadata.get("queryable_evidence", True)),
        key=lambda block: block.ordinal,
    )
    if not queryable:
        return []
    document_id = queryable[0].document_id
    node_id = uuid5(document_id, "structure:explicit-fallback")
    return [
        StructureNode(
            node_id=node_id,
            document_id=document_id,
            title="Document evidence",
            kind="explicit_fallback",
            ordinal=0,
            start_block_id=queryable[0].block_id,
            end_block_id=queryable[-1].block_id,
            method="explicit_fallback",
            confidence=1.0,
            verified=True,
            metadata={"reason": "no candidate hierarchy passed structural validation", "level": 1},
        )
    ]


def _nodes_from_candidates(
    candidates: Sequence[StructureCandidate],
    blocks: Sequence[ContentBlock],
) -> list[StructureNode]:
    if not candidates or not blocks:
        return []
    position_by_id = {block.block_id: position for position, block in enumerate(blocks)}
    usable = [candidate for candidate in candidates if candidate.block_id in position_by_id]
    usable.sort(key=lambda candidate: position_by_id[candidate.block_id])
    if not usable:
        return []

    node_ids = [
        uuid5(
            blocks[0].document_id,
            f"structure:{candidate.block_id}:{candidate.level}:{candidate.title.casefold()}",
        )
        for candidate in usable
    ]
    nodes: list[StructureNode] = []
    stack: list[int] = []
    for index, candidate in enumerate(usable):
        start_position = position_by_id[candidate.block_id]
        end_position = len(blocks) - 1
        for following in usable[index + 1 :]:
            if following.level <= candidate.level:
                end_position = max(start_position, position_by_id[following.block_id] - 1)
                break
        while stack and usable[stack[-1]].level >= candidate.level:
            stack.pop()
        parent_node_id = node_ids[stack[-1]] if stack else None
        nodes.append(
            StructureNode(
                node_id=node_ids[index],
                document_id=blocks[0].document_id,
                parent_node_id=parent_node_id,
                title=candidate.title,
                kind="section",
                ordinal=index,
                start_block_id=blocks[start_position].block_id,
                end_block_id=blocks[end_position].block_id,
                method=candidate.method,
                confidence=candidate.confidence,
                verified=False,
                metadata={
                    "level": candidate.level,
                    "source_block_id": str(candidate.block_id),
                    "start_block_ordinal": blocks[start_position].ordinal,
                    "end_block_ordinal": blocks[end_position].ordinal,
                },
            )
        )
        stack.append(index)
    return nodes
