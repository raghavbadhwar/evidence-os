"""Reproducible long-document retrieval speed and deterministic-accuracy benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from proofcounsel_ingest.config import ParserConfig, RetrievalConfig
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    SourceLocator,
    StructureNode,
)
from proofcounsel_ingest.retrieval.hybrid import HybridRetriever
from proofcounsel_ingest.retrieval.repository import SQLiteRetrievalRepository

TENANT_ID = UUID("00000000-0000-0000-0000-000000009000")
DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000009001")
SOURCE_SHA256 = "9" * 64
CONFIG_HASH = "c" * 64


def run_benchmark(
    *,
    pages: int = 900,
    blocks_per_page: int = 10,
    query_count: int = 50,
) -> dict[str, Any]:
    if pages <= 0 or blocks_per_page <= 0 or query_count <= 0:
        raise ValueError("pages, blocks_per_page, and query_count must be positive")
    blocks = _blocks(pages, blocks_per_page)
    nodes = _nodes(blocks)
    config = ParserConfig.load(Path("config/parser-defaults.toml")).retrieval
    with tempfile.TemporaryDirectory(prefix="proofcounsel-long-document-") as temporary:
        database = Path(temporary) / "retrieval.sqlite3"
        repository = SQLiteRetrievalRepository(database)
        started = time.perf_counter()
        repository.index_document(
            tenant_id=TENANT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            config_hash=CONFIG_HASH,
            blocks=blocks,
            nodes=nodes,
        )
        indexing_ms = _elapsed_ms(started)
        retriever = HybridRetriever(repository)
        latencies: list[float] = []
        correct = 0
        for query_number in range(query_count):
            target = (query_number * 173) % len(blocks)
            query = f"Which provision has unique obligation marker term{target} and notice period?"
            started = time.perf_counter()
            result = retriever.retrieve(
                query,
                tenant_id=TENANT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
                config=config,
            )
            latencies.append(_elapsed_ms(started))
            correct += any(f"term{target} " in hit.text for hit in result.hits[:5])
        deterministic_accuracy = _accuracy_probes(retriever, config)
        repository.close()

        cold_repository = SQLiteRetrievalRepository(database)
        cold_retriever = HybridRetriever(cold_repository)
        cold_target = min(8711, len(blocks) - 1)
        started = time.perf_counter()
        cold_result = cold_retriever.retrieve(
            f"Find unique obligation marker term{cold_target}",
            tenant_id=TENANT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            config=config,
        )
        cold_reopen_ms = _elapsed_ms(started)
        cold_correct = any(f"term{cold_target} " in hit.text for hit in cold_result.hits[:5])
        cold_repository.close()

    warm = latencies[1:] if len(latencies) > 1 else latencies
    report = {
        "schema_version": "1.0",
        "workload": {
            "pages": pages,
            "blocks_per_page": blocks_per_page,
            "block_count": len(blocks),
            "query_count": query_count,
        },
        "performance": {
            "indexing_ms": indexing_ms,
            "first_in_process_query_ms": latencies[0],
            "warm_mean_ms": statistics.fmean(warm),
            "warm_p95_ms": _percentile(warm, 0.95),
            "warm_p99_ms": _percentile(warm, 0.99),
            "cold_reopen_query_ms": cold_reopen_ms,
        },
        "accuracy": {
            "unique_marker_top5": correct / query_count,
            "cold_reopen_top5": float(cold_correct),
            **deterministic_accuracy,
        },
    }
    report["gates"] = _evaluate_gates(report)
    return report


def _blocks(pages: int, blocks_per_page: int) -> list[ContentBlock]:
    result: list[ContentBlock] = []
    for ordinal in range(pages * blocks_per_page):
        if ordinal == 17:
            text = "Termination is permitted when the supplier materially breaches the agreement."
        else:
            text = (
                f"Clause {ordinal}: standard commercial provision ordinary evidence. "
                f"Unique obligation marker term{ordinal} requires notice period {ordinal % 90 + 1} days."
            )
        result.append(
            ContentBlock(
                block_id=uuid5(DOCUMENT_ID, f"block:{ordinal}"),
                document_id=DOCUMENT_ID,
                ordinal=ordinal,
                block_type=BlockType.HEADING if ordinal % 100 == 0 else BlockType.PARAGRAPH,
                text=text,
                confidence=1,
                locator=SourceLocator(
                    format=DocumentFormat.PDF,
                    page=ordinal // blocks_per_page + 1,
                    bbox=(0, 0, 100, 100),
                ),
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                metadata={"queryable_evidence": True},
            )
        )
    return result


def _nodes(blocks: list[ContentBlock]) -> list[StructureNode]:
    result: list[StructureNode] = []
    for start in range(0, len(blocks), 100):
        ordinal = start // 100
        result.append(
            StructureNode(
                node_id=uuid5(DOCUMENT_ID, f"node:{ordinal}"),
                document_id=DOCUMENT_ID,
                title=f"Part {ordinal + 1} Commercial Terms",
                kind="section",
                ordinal=ordinal,
                start_block_id=blocks[start].block_id,
                end_block_id=blocks[min(start + 99, len(blocks) - 1)].block_id,
                method="long-document-benchmark",
                confidence=1,
                verified=True,
            )
        )
    return result


def _accuracy_probes(retriever: HybridRetriever, config: RetrievalConfig) -> dict[str, float]:
    probes = {
        "morphology_top5": ("When may the supplier terminate after breaching?", "Termination is permitted"),
        "ocr_typo_top5": ("terminaton", "Termination is permitted"),
        "exact_reference_top5": ("What is required by Part 1?", "Clause 0:"),
    }
    scores: dict[str, float] = {}
    for metric, (query, expected) in probes.items():
        result = retriever.retrieve(
            query,
            tenant_id=TENANT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            config=config,
        )
        scores[metric] = float(any(expected in hit.text for hit in result.hits[:5]))
    return scores


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def _evaluate_gates(report: dict[str, Any]) -> dict[str, Any]:
    performance = report["performance"]
    accuracy = report["accuracy"]
    checks = {
        "first_in_process_query_under_500_ms": performance["first_in_process_query_ms"] <= 500,
        "warm_p95_under_100_ms": performance["warm_p95_ms"] <= 100,
        "cold_reopen_query_under_1500_ms": performance["cold_reopen_query_ms"] <= 1500,
        "all_accuracy_probes_pass": all(value == 1.0 for value in accuracy.values()),
    }
    return {"passed": all(checks.values()), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=900)
    parser.add_argument("--blocks-per-page", type=int, default=10)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = run_benchmark(
        pages=arguments.pages,
        blocks_per_page=arguments.blocks_per_page,
        query_count=arguments.queries,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["gates"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
