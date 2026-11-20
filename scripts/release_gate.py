"""Evaluate a benchmark report against per-slice production gates."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_GATES = {
    "retrieval_recall_at_20": 0.97,
    "exact_citation_precision": 0.99,
    "page_coverage": 0.995,
    "locator_coverage": 1.0,
    "source_traceability_rate": 1.0,
    "silent_empty_successes": 0,
    "cross_tenant_retrievals": 0,
    "prompt_policy_overrides": 0,
}
POSITIVE_METRICS = {
    "retrieval_recall_at_20",
    "exact_citation_precision",
    "page_coverage",
    "locator_coverage",
    "source_traceability_rate",
}
METRIC_ALIASES = {
    "retrieval_recall_at_20": ("retrieval_recall_at_20", "recall_at_20"),
}
OPERATIONAL_METRICS = {"cross_tenant_retrievals", "prompt_policy_overrides"}


def evaluate(
    report: dict[str, Any],
    gates: dict[str, float] | None = None,
    *,
    operational_report: dict[str, Any] | None = None,
    signing_key: str | bytes | None = None,
    min_documents: int | None = None,
    min_questions: int | None = None,
    require_all_metrics: bool = False,
) -> dict[str, Any]:
    thresholds = gates or DEFAULT_GATES
    failures: list[dict[str, Any]] = []
    slices = report.get("slices") or {"aggregate": report}
    for slice_name, metrics in slices.items():
        for metric, threshold in thresholds.items():
            if metric in OPERATIONAL_METRICS:
                continue
            actual_value = _metric_value(metrics, metric)
            if actual_value is None:
                if require_all_metrics:
                    failures.append(
                        {
                            "slice": slice_name,
                            "metric": metric,
                            "actual": "missing",
                            "threshold": threshold,
                        }
                    )
                continue
            actual = float(actual_value)
            passed = actual >= threshold if metric in POSITIVE_METRICS else actual <= threshold
            if not passed:
                failures.append(
                    {
                        "slice": slice_name,
                        "metric": metric,
                        "actual": actual,
                        "threshold": threshold,
                    }
                )
    operational_metrics = _operational_metrics(report, operational_report)
    for metric in sorted(OPERATIONAL_METRICS):
        if metric not in thresholds:
            continue
        threshold = thresholds[metric]
        actual_value = _metric_value(operational_metrics, metric)
        if actual_value is None:
            if require_all_metrics:
                failures.append(
                    {
                        "slice": "operational",
                        "metric": metric,
                        "actual": "missing",
                        "threshold": threshold,
                    }
                )
            continue
        actual = float(actual_value)
        if actual > threshold:
            failures.append(
                {
                    "slice": "operational",
                    "metric": metric,
                    "actual": actual,
                    "threshold": threshold,
                }
            )
    _check_minimum_count(report, "document_count", min_documents, failures)
    _check_minimum_count(report, "question_count", min_questions, failures)
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    result = {
        "schema_version": "1.0",
        "passed": not failures,
        "failures": failures,
        "gates": thresholds,
        "minimum_documents": min_documents,
        "minimum_questions": min_questions,
        "required_metrics": require_all_metrics,
        "input_report_sha256": hashlib.sha256(canonical).hexdigest(),
        "operational_report_sha256": _report_hash(operational_report),
    }
    if signing_key:
        key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
        signed_payload = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        result["signature_algorithm"] = "HMAC-SHA256"
        result["signature"] = hmac.new(key, signed_payload, hashlib.sha256).hexdigest()
    else:
        result["signature_algorithm"] = None
        result["signature"] = None
    return result


def _operational_metrics(
    report: dict[str, Any],
    operational_report: dict[str, Any] | None,
) -> dict[str, Any]:
    if operational_report is not None:
        metrics = operational_report.get("metrics")
        return metrics if isinstance(metrics, dict) else {}
    embedded = report.get("operational_metrics")
    return embedded if isinstance(embedded, dict) else {}


def _report_hash(report: dict[str, Any] | None) -> str | None:
    if report is None:
        return None
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _metric_value(metrics: dict[str, Any], metric: str) -> Any:
    for candidate in METRIC_ALIASES.get(metric, (metric,)):
        if candidate in metrics:
            return metrics[candidate]
    return None


def _check_minimum_count(
    report: dict[str, Any],
    field: str,
    minimum: int | None,
    failures: list[dict[str, Any]],
) -> None:
    if minimum is None:
        return
    actual = report.get(field)
    if not isinstance(actual, int) or actual < minimum:
        failures.append(
            {
                "slice": "release_report",
                "metric": field,
                "actual": actual if actual is not None else "missing",
                "threshold": minimum,
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--operational-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signing-key-env", default="PROOFCOUNSEL_RELEASE_SIGNING_KEY")
    parser.add_argument("--require-signature", action="store_true")
    parser.add_argument("--min-documents", type=int)
    parser.add_argument("--min-questions", type=int)
    parser.add_argument("--require-all-metrics", action="store_true")
    args = parser.parse_args()
    signing_key = os.environ.get(args.signing_key_env)
    operational_report = (
        json.loads(args.operational_report.read_text(encoding="utf-8"))
        if args.operational_report
        else None
    )
    result = evaluate(
        json.loads(args.report.read_text(encoding="utf-8")),
        operational_report=operational_report,
        signing_key=signing_key,
        min_documents=args.min_documents,
        min_questions=args.min_questions,
        require_all_metrics=args.require_all_metrics,
    )
    if args.require_signature and not signing_key:
        result["passed"] = False
        result["failures"].append(
            {
                "slice": "release_report",
                "metric": "signature",
                "actual": "missing",
                "threshold": "HMAC-SHA256",
            }
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
