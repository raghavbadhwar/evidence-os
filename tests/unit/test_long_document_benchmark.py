from __future__ import annotations

from benchmarks.long_document_benchmark import run_benchmark


def test_long_document_benchmark_reports_performance_and_accuracy() -> None:
    report = run_benchmark(pages=3, blocks_per_page=10, query_count=3)

    assert report["workload"] == {
        "pages": 3,
        "blocks_per_page": 10,
        "block_count": 30,
        "query_count": 3,
    }
    assert report["accuracy"] == {
        "unique_marker_top5": 1.0,
        "cold_reopen_top5": 1.0,
        "morphology_top5": 1.0,
        "ocr_typo_top5": 1.0,
        "exact_reference_top5": 1.0,
    }
    assert all(value >= 0 for value in report["performance"].values())
    assert report["gates"]["passed"]
    assert all(report["gates"]["checks"].values())
