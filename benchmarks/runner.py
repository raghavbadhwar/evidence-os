"""Run the deterministic local benchmark over a versioned corpus manifest."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.graders import exact_citation_precision, first_matching_rank, hit_matches, recall_at_k
from proofcounsel_ingest.answer import AnswerProvider, answer_document
from proofcounsel_ingest.citations import CitationRequest, verify_citations
from proofcounsel_ingest.config import Settings
from proofcounsel_ingest.orchestrator import ingest
from proofcounsel_ingest.retrieval.repository import close_repository, repository_for_settings
from proofcounsel_ingest.retrieval.service import retrieve_document
from proofcounsel_ingest.telemetry import JsonlTelemetrySink


def run_benchmark(
    manifest_path: Path,
    questions_path: Path,
    *,
    repeats: int = 5,
    artifact_root: Path | None = None,
    project_root: Path | None = None,
    source_root: Path | None = None,
    answer_provider: AnswerProvider | None = None,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    questions = [
        json.loads(line)
        for line in questions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    root = artifact_root or Path(".benchmark-artifacts")
    resolved_project_root = (project_root or manifest_path.parent.parent).expanduser().resolve()
    resolved_source_root = (source_root or resolved_project_root).expanduser().resolve()
    results: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix="proofcounsel-benchmark-") as temporary, ExitStack() as stack:
        runtime_root = Path(temporary) if artifact_root is None else root
        settings = Settings(
            environment="test",
            artifact_root=runtime_root,
            parser_config=resolved_project_root / "config" / "parser-defaults.toml",
        )
        documents: dict[str, Any] = {}
        for document in manifest["documents"]:
            source = (resolved_source_root / document["path"]).resolve()
            outcome = ingest(source, settings=settings)
            documents[document["id"]] = (document, outcome)
        document_metrics_by_id = {
            document_id: _document_metrics(outcome)
            for document_id, (_document, outcome) in documents.items()
        }
        telemetry = JsonlTelemetrySink(runtime_root / "benchmark-telemetry.jsonl")
        repository = repository_for_settings(settings)
        stack.callback(close_repository, repository)
        for question in questions:
            document, outcome = documents[question["document_id"]]
            document_metrics = document_metrics_by_id[question["document_id"]]
            expected_texts = _expected_texts(question)
            question_repeats = _question_repeats(question, repeats)
            if not outcome.quality or not outcome.quality.queryable:
                results.append(
                    {
                        "question_id": question["id"],
                        "document_id": question["document_id"],
                        "slice": document["slice"],
                        "status": outcome.status.value,
                        "recall_at_20": 0.0,
                        "retrieval_recall_at_20": 0.0,
                        "exact_citation_precision": 0.0,
                        "first_rank": None,
                        **document_metrics,
                    }
                )
                continue
            for repeat in range(question_repeats):
                started = time.perf_counter()
                retrieval, context = retrieve_document(
                    question["query"],
                    tenant_id=outcome.identity.tenant_id,
                    document_id=outcome.identity.document_id,
                    source_sha256=outcome.identity.source_sha256,
                    settings=settings,
                    telemetry_sink=telemetry,
                    repository=repository,
                )
                model_metrics = _model_answer_metrics(
                    question,
                    expected_texts,
                    context=context,
                    tenant_id=outcome.identity.tenant_id,
                    document_id=outcome.identity.document_id,
                    source_sha256=outcome.identity.source_sha256,
                    provider=answer_provider,
                    telemetry_sink=telemetry,
                )
                blocks = repository.get_blocks(
                    tenant_id=outcome.identity.tenant_id,
                    document_id=outcome.identity.document_id,
                )
                citation_requests = [
                    CitationRequest(
                        tenant_id=outcome.identity.tenant_id,
                        document_id=outcome.identity.document_id,
                        source_sha256=outcome.identity.source_sha256,
                        block_id=hit.block_id,
                        answer_span=expected,
                        quoted_text=expected,
                        locator=hit.locator,
                    )
                    for expected in expected_texts
                    for hit in retrieval.hits
                    if hit_matches(hit, [expected])
                ][: len(expected_texts)]
                citation_result = verify_citations(
                    citation_requests,
                    blocks,
                    expected_tenant_id=outcome.identity.tenant_id,
                    expected_document_id=outcome.identity.document_id,
                    expected_source_sha256=outcome.identity.source_sha256,
                )
                results.append(
                    {
                        "question_id": question["id"],
                        "document_id": question["document_id"],
                        "slice": document["slice"],
                        "repeat": repeat,
                        "evaluation_mode": question.get("evaluation_mode", "unspecified"),
                        "status": "ok",
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                        "hit_count": len(retrieval.hits),
                        "recall_at_5": recall_at_k(retrieval.hits, expected_texts, 5),
                        "recall_at_20": recall_at_k(retrieval.hits, expected_texts, 20),
                        "retrieval_recall_at_20": recall_at_k(
                            retrieval.hits, expected_texts, 20
                        ),
                        "first_rank": first_matching_rank(retrieval.hits, expected_texts),
                        "gold_locator_accuracy": _gold_locator_accuracy(question, retrieval.hits),
                        "exact_citation_precision": exact_citation_precision(
                            [
                                check.citation.model_dump(mode="json")
                                for check in citation_result.checks
                                if check.citation
                            ],
                            blocks,
                        ),
                        "input_tokens": context.estimated_tokens,
                        "semantic_attempted": retrieval.telemetry.semantic_attempted,
                        "reranker_cost": retrieval.telemetry.reranker.cost
                        if retrieval.telemetry.reranker
                        else 0.0,
                        **model_metrics,
                        **document_metrics,
                    }
                )
    return _summarize(manifest, questions, results, repeats)


def _summarize(
    manifest: dict[str, Any], questions: list[dict[str, Any]], results: list[dict[str, Any]], repeats: int
) -> dict[str, Any]:
    slices: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        slices.setdefault(result["slice"], []).append(result)
    slice_summaries: dict[str, Any] = {}
    for name, rows in slices.items():
        successful = [row for row in rows if row["status"] == "ok"]
        grounded_successes = sum(row.get("grounded_answer_success", 0) for row in rows)
        recorded_cost = sum(row.get("reranker_cost", 0) + row.get("answer_provider_cost", 0) for row in rows)
        slice_summaries[name] = {
            "question_runs": len(rows),
            "successful_runs": len(successful),
            "recall_at_5": _mean(successful, "recall_at_5"),
            "recall_at_20": _mean(successful, "recall_at_20"),
            "retrieval_recall_at_20": _mean(successful, "retrieval_recall_at_20"),
            "exact_citation_precision": _mean(successful, "exact_citation_precision"),
            "average_latency_ms": _mean(successful, "elapsed_ms"),
            "average_input_tokens": _mean(successful, "input_tokens"),
            "average_reranker_cost_per_completed_run": _mean(successful, "reranker_cost"),
            "cost_per_successful_task": recorded_cost / grounded_successes if grounded_successes else None,
            "page_coverage": _minimum(rows, "page_coverage"),
            "locator_coverage": _minimum(rows, "locator_coverage"),
            "source_traceability_rate": _mean(rows, "source_traceability_rate"),
            "gold_locator_accuracy": _mean(successful, "gold_locator_accuracy"),
            "grounded_answer_success_rate": _mean(successful, "grounded_answer_success"),
            "model_abstention_rate": _mean(successful, "model_abstained"),
            "average_answer_latency_ms": _mean(successful, "answer_elapsed_ms"),
            "average_answer_input_tokens": _mean(successful, "answer_input_tokens"),
            "average_answer_output_tokens": _mean(successful, "answer_output_tokens"),
            "average_answer_provider_cost": _mean(successful, "answer_provider_cost"),
            "silent_empty_successes": sum(int(row.get("silent_empty_success", 0)) for row in rows),
        }
    return {
        "schema_version": "1.0",
        "repeats": repeats,
        "document_count": len(manifest["documents"]),
        "question_count": len(questions),
        "run_count": len(results),
        "results": results,
        "slices": slice_summaries,
    }


def _mean(rows: list[dict[str, Any]], key: str) -> float:
    values = [float(row[key]) for row in rows if key in row and row[key] is not None]
    return sum(values) / len(values) if values else 0.0


def _minimum(rows: list[dict[str, Any]], key: str) -> float:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return min(values) if values else 0.0


def _expected_texts(question: dict[str, Any]) -> list[str]:
    labels = question.get("expected_labels")
    if isinstance(labels, list) and labels:
        texts = [
            str(label.get("answer_text", ""))
            for label in labels
            if isinstance(label, dict) and str(label.get("answer_text", "")).strip()
        ]
        if texts:
            return texts
    expected = question.get("expected_texts")
    if isinstance(expected, list) and all(isinstance(item, str) and item.strip() for item in expected):
        return expected
    raise ValueError(f"question {question.get('id', '<unknown>')} has no expected answer labels")


def _question_repeats(question: dict[str, Any], default: int) -> int:
    value = question.get("repeats", default)
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"question {question.get('id', '<unknown>')} has invalid repeats")
    return value


def _gold_locator_accuracy(question: dict[str, Any], hits: list[Any]) -> float | None:
    labels = question.get("expected_labels")
    if not isinstance(labels, list) or not labels:
        return None
    matched = 0
    for label in labels:
        if not isinstance(label, dict):
            continue
        answer_text = str(label.get("answer_text", ""))
        expected_locator = label.get("locator")
        if not isinstance(expected_locator, dict):
            continue
        matching_hits = [hit for hit in hits if hit_matches(hit, [answer_text])]
        if any(_locator_matches(hit.locator.model_dump(mode="json"), expected_locator) for hit in matching_hits):
            matched += 1
    return matched / len(labels)


def _locator_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _model_answer_metrics(
    question: dict[str, Any],
    expected_texts: list[str],
    *,
    context: Any,
    tenant_id: Any,
    document_id: Any,
    source_sha256: str,
    provider: AnswerProvider | None,
    telemetry_sink: Any,
) -> dict[str, Any]:
    mode = question.get("evaluation_mode")
    if mode != "model":
        return {"model_called": False}
    if provider is None:
        raise ValueError(
            f"question {question.get('id', '<unknown>')} requires an answer provider"
        )
    started = time.perf_counter()
    result = answer_document(
        str(question["query"]),
        context,
        tenant_id=tenant_id,
        document_id=document_id,
        source_sha256=source_sha256,
        provider=provider,
        telemetry_sink=telemetry_sink,
    )
    claim_texts = [claim.text for claim in result.answer.claims]
    expected_recall = _claim_recall(claim_texts, expected_texts)
    all_verified = bool(getattr(result.verification, "all_verified", False))
    success = not result.answer.abstained and all_verified and expected_recall == 1.0
    return {
        "model_called": True,
        "grounded_answer_success": 1.0 if success else 0.0,
        "model_abstained": 1.0 if result.answer.abstained else 0.0,
        "answer_expected_recall": expected_recall,
        "answer_all_citations_verified": all_verified,
        "answer_elapsed_ms": (time.perf_counter() - started) * 1000,
        "answer_model": result.model,
        "answer_model_version": result.version,
        "answer_input_tokens": result.input_tokens,
        "answer_output_tokens": result.output_tokens,
        "answer_provider_cost": result.provider_cost,
        "prompt_injection_signal_count": result.prompt_injection_signal_count,
    }


def _claim_recall(claim_texts: list[str], expected_texts: list[str]) -> float:
    if not expected_texts:
        return 0.0
    normalized_claims = [" ".join(text.casefold().split()) for text in claim_texts]
    matches = 0
    for expected in expected_texts:
        normalized_expected = " ".join(expected.casefold().split())
        if any(
            normalized_expected in claim or claim in normalized_expected
            for claim in normalized_claims
        ):
            matches += 1
    return matches / len(expected_texts)


def load_answer_provider(specification: str) -> AnswerProvider:
    module_name, separator, factory_name = specification.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("answer provider must use module.path:factory_name")
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError(f"answer provider factory is not callable: {specification}")
    provider = factory()
    if not callable(getattr(provider, "generate", None)):
        raise ValueError(f"answer provider has no generate method: {specification}")
    return cast(AnswerProvider, provider)


def _document_metrics(outcome: Any) -> dict[str, float | int]:
    quality = outcome.quality
    traceable = False
    if outcome.artifact_path:
        ingestion_manifest_path = outcome.artifact_path / "ingestion_manifest.json"
        artifact_manifest_path = outcome.artifact_path / "artifact_manifest.json"
        if ingestion_manifest_path.exists() and artifact_manifest_path.exists():
            try:
                ingestion_manifest = json.loads(ingestion_manifest_path.read_text(encoding="utf-8"))
                artifact_manifest = json.loads(artifact_manifest_path.read_text(encoding="utf-8"))
                identity = ingestion_manifest.get("identity", {})
                metadata = artifact_manifest.get("metadata", {})
                traceable = (
                    identity.get("tenant_id") == str(outcome.identity.tenant_id)
                    and identity.get("document_id") == str(outcome.identity.document_id)
                    and identity.get("source_sha256") == outcome.identity.source_sha256
                    and metadata.get("tenant_id") == str(outcome.identity.tenant_id)
                    and metadata.get("document_id") == str(outcome.identity.document_id)
                    and metadata.get("source_sha256") == outcome.identity.source_sha256
                    and metadata.get("extractor_version") == outcome.run.extractor_version
                    and metadata.get("parser_config_hash") == outcome.run.parser_config_hash
                )
            except (OSError, TypeError, ValueError):
                traceable = False
    silent_empty_success = int(
        outcome.status.value == "ready" and (quality is None or quality.nonempty_block_count == 0)
    )
    return {
        "page_coverage": float(quality.page_coverage) if quality is not None else 0.0,
        "locator_coverage": float(quality.locator_coverage) if quality is not None else 0.0,
        "source_traceability_rate": 1.0 if traceable else 0.0,
        "silent_empty_success": silent_empty_success,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/corpus_manifest.json"))
    parser.add_argument("--questions", type=Path, default=Path("benchmarks/questions.jsonl"))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--answer-provider-factory")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    answer_provider = (
        load_answer_provider(args.answer_provider_factory)
        if args.answer_provider_factory
        else None
    )
    report = run_benchmark(
        args.manifest,
        args.questions,
        repeats=args.repeats,
        project_root=args.project_root,
        source_root=args.source_root,
        answer_provider=answer_provider,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
