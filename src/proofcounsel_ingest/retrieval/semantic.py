"""Optional semantic search contract; disabled unless an index version is explicitly configured."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from proofcounsel_ingest.retrieval.repository import SearchRecord


class SemanticIndexDescriptor(BaseModel):
    model: str
    dimensions: int = Field(gt=0)
    version: str


class SemanticSearchPort(Protocol):
    descriptor: SemanticIndexDescriptor | None

    def search(self, query: str, *, limit: int) -> list[SearchRecord]: ...


class DisabledSemanticSearch:
    descriptor: SemanticIndexDescriptor | None = None

    def search(self, query: str, *, limit: int) -> list[SearchRecord]:
        del query, limit
        return []


def require_compatible_index(expected: SemanticIndexDescriptor, actual: SemanticIndexDescriptor) -> None:
    if expected != actual:
        raise ValueError(
            "semantic index version mismatch: "
            f"expected {expected.model}/{expected.version}/{expected.dimensions}, "
            f"got {actual.model}/{actual.version}/{actual.dimensions}"
        )

