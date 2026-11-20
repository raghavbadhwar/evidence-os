"""Run release-critical security checks and emit structured operational metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


@dataclass(frozen=True)
class OperationalCheck:
    metric: str
    selectors: tuple[str, ...]


CHECKS = (
    OperationalCheck(
        metric="cross_tenant_retrievals",
        selectors=(
            "tests/integration/test_phase5_retrieval.py::test_retrieval_cannot_cross_tenant_boundary",
            "tests/integration/test_postgres_rls.py::test_postgres_rls_matrix",
        ),
    ),
    OperationalCheck(
        metric="prompt_policy_overrides",
        selectors=("tests/security/test_prompt_boundary.py",),
    ),
)


def collect_operational_evidence(
    project_root: Path,
    *,
    timeout_seconds: float = 900,
) -> dict[str, Any]:
    root = project_root.expanduser().resolve(strict=True)
    checks = [_run_check(check, root, timeout_seconds=timeout_seconds) for check in CHECKS]
    metrics = {check["metric"]: check["violation_count"] for check in checks}
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "metrics": metrics,
        "checks": checks,
        "passed": all(value == 0 for value in metrics.values()),
    }


def _run_check(
    check: OperationalCheck,
    project_root: Path,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    with TemporaryDirectory(prefix="proofcounsel-operational-") as temporary:
        junit_path = Path(temporary) / "junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *check.selectors,
            "--junitxml",
            str(junit_path),
        ]
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return_code = completed.returncode
            output = completed.stdout + "\n" + completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            return_code = 124
            output = _timeout_output(exc)
        counts = _parse_junit(junit_path) if junit_path.exists() else _empty_counts()
    violation_count = _violation_count(counts, return_code=return_code, timed_out=timed_out)
    return {
        "metric": check.metric,
        "selectors": list(check.selectors),
        "return_code": return_code,
        "timed_out": timed_out,
        "duration_ms": (time.perf_counter() - started) * 1000,
        "counts": counts,
        "violation_count": violation_count,
        "output_sha256": hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest(),
    }


def _parse_junit(path: Path) -> dict[str, int | float]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    return {
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
        "test_time_seconds": sum(float(case.attrib.get("time", "0") or 0) for case in cases),
    }


def _empty_counts() -> dict[str, int | float]:
    return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "test_time_seconds": 0.0}


def _violation_count(
    counts: dict[str, int | float],
    *,
    return_code: int,
    timed_out: bool,
) -> int:
    violations = int(counts["failures"]) + int(counts["errors"]) + int(counts["skipped"])
    if timed_out or (return_code != 0 and violations == 0):
        violations += 1
    return violations


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    return stdout + "\n" + stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    args = parser.parse_args()
    report = collect_operational_evidence(
        args.project_root,
        timeout_seconds=args.timeout_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
