from __future__ import annotations

from pathlib import Path

from scripts.operational_evidence import _parse_junit, _violation_count


def test_parse_junit_counts_failures_errors_and_skips(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite tests="4">
    <testcase name="passed" time="0.1" />
    <testcase name="failed" time="0.2"><failure /></testcase>
    <testcase name="errored" time="0.3"><error /></testcase>
    <testcase name="skipped" time="0.4"><skipped /></testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    counts = _parse_junit(junit)

    assert counts == {
        "tests": 4,
        "failures": 1,
        "errors": 1,
        "skipped": 1,
        "test_time_seconds": 1.0,
    }
    assert _violation_count(counts, return_code=1, timed_out=False) == 3


def test_operational_evidence_treats_missing_or_timed_out_tests_as_violations() -> None:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "test_time_seconds": 0.0}

    assert _violation_count(counts, return_code=5, timed_out=False) == 1
    assert _violation_count(counts, return_code=124, timed_out=True) == 1
