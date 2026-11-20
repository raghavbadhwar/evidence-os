from __future__ import annotations

from scripts.release_gate import evaluate


def test_release_gate_fails_a_single_format_slice() -> None:
    result = evaluate(
        {
            "slices": {
                "born_digital_pdf": {"retrieval_recall_at_20": 0.99},
                "scanned_pdf": {"retrieval_recall_at_20": 0.80},
            }
        }
    )
    assert not result["passed"]
    assert result["failures"][0]["slice"] == "scanned_pdf"


def test_release_gate_accepts_sufficient_slice_metrics() -> None:
    result = evaluate(
        {
            "slices": {
                "born_digital_pdf": {
                    "retrieval_recall_at_20": 0.99,
                    "exact_citation_precision": 1.0,
                    "page_coverage": 1.0,
                    "locator_coverage": 1.0,
                    "source_traceability_rate": 1.0,
                    "silent_empty_successes": 0,
                }
            }
        },
        operational_report={
            "schema_version": "1.0",
            "metrics": {"cross_tenant_retrievals": 0, "prompt_policy_overrides": 0},
        },
    )
    assert result["passed"]
    assert len(result["input_report_sha256"]) == 64
    assert len(result["operational_report_sha256"]) == 64


def test_release_gate_can_sign_the_immutable_report() -> None:
    result = evaluate({"slices": {"html": {"retrieval_recall_at_20": 1.0}}}, signing_key="test-key")
    assert result["signature_algorithm"] == "HMAC-SHA256"
    assert len(result["signature"]) == 64


def test_release_gate_accepts_benchmark_recall_alias_and_enforces_counts() -> None:
    result = evaluate(
        {
            "document_count": 100,
            "question_count": 500,
            "slices": {
                "synthetic": {
                    "recall_at_20": 1.0,
                    "exact_citation_precision": 1.0,
                }
            },
        },
        min_documents=100,
        min_questions=500,
    )
    assert result["passed"]


def test_release_gate_can_fail_when_required_metrics_are_missing() -> None:
    result = evaluate(
        {"slices": {"synthetic": {"retrieval_recall_at_20": 1.0}}},
        require_all_metrics=True,
    )
    assert not result["passed"]
    assert any(failure["metric"] == "exact_citation_precision" for failure in result["failures"])
    assert any(
        failure["slice"] == "operational" and failure["metric"] == "cross_tenant_retrievals"
        for failure in result["failures"]
    )


def test_release_gate_fails_nonzero_operational_security_metrics() -> None:
    result = evaluate(
        {"slices": {"text": {"retrieval_recall_at_20": 1.0}}},
        operational_report={
            "metrics": {"cross_tenant_retrievals": 1, "prompt_policy_overrides": 0}
        },
    )

    assert not result["passed"]
    assert result["failures"] == [
        {
            "slice": "operational",
            "metric": "cross_tenant_retrievals",
            "actual": 1.0,
            "threshold": 0,
        }
    ]
