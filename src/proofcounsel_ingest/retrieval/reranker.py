"""Bounded reranking port with deterministic local implementation and telemetry."""

from __future__ import annotations

import time
from typing import Protocol

from pydantic import BaseModel, Field

from proofcounsel_ingest.retrieval.lexical import normalized_terms
from proofcounsel_ingest.retrieval.repository import SearchRecord


class RerankTelemetry(BaseModel):
    implementation: str
    version: str
    candidate_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost: float = Field(ge=0)


class RerankResult(BaseModel):
    records: list[SearchRecord]
    telemetry: RerankTelemetry


class RerankerPort(Protocol):
    def rerank(self, query: str, candidates: list[SearchRecord], *, limit: int) -> RerankResult: ...


class LexicalOverlapReranker:
    def rerank(self, query: str, candidates: list[SearchRecord], *, limit: int) -> RerankResult:
        started = time.perf_counter()
        query_terms = _terms(query)
        scored = []
        for candidate in candidates[:limit]:
            candidate_terms = _terms(candidate.text)
            overlap = len(query_terms & candidate_terms) / max(len(query_terms), 1)
            phrase = 1.0 if query.casefold() in candidate.text.casefold() else 0.0
            scored.append((candidate.raw_score + overlap + phrase, candidate))
        scored.sort(key=lambda item: (item[0], -item[1].ordinal), reverse=True)
        records = [record.model_copy(update={"raw_score": score}) for score, record in scored]
        elapsed = (time.perf_counter() - started) * 1000
        return RerankResult(
            records=records,
            telemetry=RerankTelemetry(
                implementation="lexical-overlap",
                version="1.0",
                candidate_count=len(records),
                elapsed_ms=elapsed,
                input_tokens=sum(len(record.text) for record in records) // 4 + len(query) // 4,
                output_tokens=0,
                cost=0,
            ),
        )


def _terms(text: str) -> set[str]:
    return set(normalized_terms(text))
