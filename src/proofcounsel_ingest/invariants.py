"""Cross-object checks that protect queryable evidence."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from uuid import UUID

from proofcounsel_ingest.models import (
    ContentBlock,
    IndexVersion,
    IngestionStatus,
    QualityReport,
    StructureNode,
)


class InvariantViolation(ValueError):
    pass


def require_tenant_scope(tenant_id: UUID | None) -> UUID:
    if tenant_id is None:
        raise InvariantViolation("tenant_id is required for every repository operation")
    return tenant_id


def validate_structure_ranges(nodes: Sequence[StructureNode], blocks: Sequence[ContentBlock]) -> None:
    block_by_id = {block.block_id: block for block in blocks}
    ordinal_by_id = {block.block_id: block.ordinal for block in blocks}
    node_by_id = {node.node_id: node for node in nodes}

    if len(block_by_id) != len(blocks):
        raise InvariantViolation("content block IDs must be unique")
    if len(node_by_id) != len(nodes):
        raise InvariantViolation("structure node IDs must be unique")

    for node in nodes:
        start = block_by_id.get(node.start_block_id)
        end = block_by_id.get(node.end_block_id)
        if start is None or end is None:
            raise InvariantViolation(f"structure node {node.node_id} references a missing block")
        if start.document_id != node.document_id or end.document_id != node.document_id:
            raise InvariantViolation(f"structure node {node.node_id} crosses document versions")
        if ordinal_by_id[start.block_id] > ordinal_by_id[end.block_id]:
            raise InvariantViolation(f"structure node {node.node_id} has an inverted range")
        if node.parent_node_id is not None:
            parent = node_by_id.get(node.parent_node_id)
            if parent is None:
                raise InvariantViolation(f"structure node {node.node_id} has a missing parent")
            if parent.document_id != node.document_id:
                raise InvariantViolation(f"structure node {node.node_id} has a cross-document parent")
            if not (
                ordinal_by_id[parent.start_block_id]
                <= ordinal_by_id[start.block_id]
                <= ordinal_by_id[end.block_id]
                <= ordinal_by_id[parent.end_block_id]
            ):
                raise InvariantViolation(f"structure node {node.node_id} is outside its parent range")


def validate_queryable(
    *,
    status: IngestionStatus,
    quality: QualityReport,
    blocks: Sequence[ContentBlock],
    nodes: Sequence[StructureNode],
    index_versions: Collection[IndexVersion],
    minimum_page_coverage: float,
) -> None:
    if status != IngestionStatus.READY:
        if quality.queryable:
            raise InvariantViolation("only ready ingestion runs may be queryable")
        return

    violations: list[str] = []
    if not quality.queryable:
        violations.append("ready requires quality_report.queryable=true")
    queryable_blocks = [block for block in blocks if block.metadata.get("queryable_evidence", True)]
    if quality.nonempty_block_count <= 0 or not queryable_blocks:
        violations.append("ready requires at least one content block")
    if quality.nonempty_block_count != len(queryable_blocks):
        violations.append("quality block count must equal persisted block count")
    if quality.page_coverage < minimum_page_coverage:
        violations.append("page coverage is below the configured minimum")
    if quality.locator_coverage != 1.0:
        violations.append("ready requires complete locator coverage")
    if not quality.indexes_persisted or not index_versions:
        violations.append("ready requires persisted index versions")
    if not nodes:
        violations.append("ready requires a verified structure or explicit fallback node")
    if violations:
        raise InvariantViolation("; ".join(violations))
    validate_structure_ranges(nodes, blocks)
