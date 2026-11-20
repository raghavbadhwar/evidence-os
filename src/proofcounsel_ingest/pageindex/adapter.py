"""Serialize verified canonical structures to the PageIndex tree contract."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field

from proofcounsel_ingest.models import ContentBlock, StructureNode

PAGEINDEX_UPSTREAM_COMMIT = "f413c66fee0bfbb7291c389333f9cc1adac68d57"


class ReasoningBudget(BaseModel):
    max_calls: int = Field(default=1, ge=0, le=10)
    max_nodes: int = Field(default=100, gt=0, le=10_000)
    max_output_tokens: int = Field(default=1000, gt=0, le=20_000)


class PageIndexReasonerPort(Protocol):
    def search(self, query: str, tree: list[dict[str, Any]], budget: ReasoningBudget) -> list[str]: ...


def to_pageindex_payload(
    document_name: str,
    nodes: Sequence[StructureNode],
    blocks: Sequence[ContentBlock],
) -> dict[str, Any]:
    block_by_id = {block.block_id: block for block in blocks}
    child_map: dict[object | None, list[StructureNode]] = {}
    for node in nodes:
        child_map.setdefault(node.parent_node_id, []).append(node)
    for children in child_map.values():
        children.sort(key=lambda node: node.ordinal)

    def serialize(node: StructureNode) -> dict[str, Any]:
        start = block_by_id[node.start_block_id]
        end = block_by_id[node.end_block_id]
        payload = {
            "node_id": str(node.node_id),
            "title": node.title,
            "start_index": start.ordinal + 1,
            "end_index": end.ordinal + 1,
            "start_block_id": str(start.block_id),
            "end_block_id": str(end.block_id),
            "start_locator": start.locator.model_dump(mode="json"),
            "end_locator": end.locator.model_dump(mode="json"),
            "method": node.method,
            "confidence": node.confidence,
            "verified": node.verified,
        }
        children = child_map.get(node.node_id, [])
        if children:
            payload["nodes"] = [serialize(child) for child in children]
        return payload

    structure = [serialize(node) for node in child_map.get(None, [])]
    result = {
        "schema_version": "1.0",
        "pageindex_upstream_commit": PAGEINDEX_UPSTREAM_COMMIT,
        "doc_name": document_name,
        "doc_description": "Verified PageIndex-compatible tree over canonical evidence blocks.",
        "indexing_method": "proofcounsel_verified_canonical_tree",
        "structure": structure,
    }
    validate_pageindex_payload(result)
    return result


def validate_pageindex_payload(payload: dict[str, Any]) -> None:
    if payload.get("pageindex_upstream_commit") != PAGEINDEX_UPSTREAM_COMMIT:
        raise ValueError("PageIndex payload uses an unpinned upstream version")
    structure = payload.get("structure")
    if not isinstance(structure, list) or not structure:
        raise ValueError("PageIndex payload requires a non-empty structure")
    seen: set[str] = set()

    def visit(node: dict[str, Any], parent: dict[str, Any] | None = None) -> None:
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or node_id in seen:
            raise ValueError("PageIndex node IDs must be unique strings")
        seen.add(node_id)
        start, end = node.get("start_index"), node.get("end_index")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
            raise ValueError(f"PageIndex node {node_id} has an invalid range")
        if parent is not None and not (parent["start_index"] <= start <= end <= parent["end_index"]):
            raise ValueError(f"PageIndex node {node_id} is outside its parent range")
        if not node.get("verified"):
            raise ValueError(f"PageIndex node {node_id} is not verified")
        for child in node.get("nodes", []):
            visit(child, node)

    for root in structure:
        visit(root)


def search_tree(
    query: str,
    payload: dict[str, Any],
    *,
    limit: int = 10,
    reasoner: PageIndexReasonerPort | None = None,
    budget: ReasoningBudget | None = None,
) -> list[dict[str, Any]]:
    validate_pageindex_payload(payload)
    flat = _flatten(payload["structure"])
    active_budget = budget or ReasoningBudget()
    if reasoner is not None and active_budget.max_calls > 0:
        selected_ids = set(reasoner.search(query, payload["structure"][: active_budget.max_nodes], active_budget))
        return [node for node in flat if node["node_id"] in selected_ids][:limit]
    query_terms = _terms(query)
    scored = []
    for node in flat:
        title_terms = _terms(str(node["title"]))
        overlap = len(query_terms & title_terms) / max(len(query_terms), 1)
        phrase = 1.0 if query.casefold() in str(node["title"]).casefold() else 0.0
        score = overlap + phrase
        if score > 0:
            scored.append((score, node))
    scored.sort(key=lambda item: (item[0], item[1]["confidence"]), reverse=True)
    return [{**node, "tree_score": score} for score, node in scored[:limit]]


def _flatten(nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in nodes:
        result.append(node)
        result.extend(_flatten(node.get("nodes", [])))
    return result


def _terms(text: str) -> set[str]:
    return {term.casefold() for term in re.findall(r"[A-Za-z0-9]{2,}", text)}
