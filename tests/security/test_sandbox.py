from __future__ import annotations

import sys

import pytest

from proofcounsel_ingest.security.sandbox import SandboxExecutionError, SandboxProfile, run_sandboxed


def test_sandbox_uses_bounded_environment(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = run_sandboxed(
        [sys.executable, "-c", "import os; print(os.environ.get('UNRELATED_SECRET', 'absent'))"],
        cwd=tmp_path,
        profile=SandboxProfile(timeout_seconds=2, cpu_seconds=2, max_output_bytes=1000),
        environment={},
    )
    assert result.stdout.strip() == "absent"
    assert not result.network_isolated


def test_sandbox_kills_timed_out_process(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SandboxExecutionError) as captured:
        run_sandboxed(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            profile=SandboxProfile(timeout_seconds=0.1, cpu_seconds=1, max_output_bytes=1000),
        )
    assert captured.value.result.timed_out


def test_network_isolation_never_falls_back_to_host_process(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SandboxExecutionError) as captured:
        run_sandboxed(
            [sys.executable, "-c", "print('must not run')"],
            cwd=tmp_path,
            profile=SandboxProfile(network_isolated=True),
        )
    assert "container image" in captured.value.result.stderr
    assert not captured.value.result.network_isolated


def test_configured_container_requires_docker_runtime(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("proofcounsel_ingest.security.sandbox.shutil.which", lambda name: None)
    with pytest.raises(SandboxExecutionError) as captured:
        run_sandboxed(
            [sys.executable, "-c", "print('must not run')"],
            cwd=tmp_path,
            profile=SandboxProfile(network_isolated=True, container_image="example/parser:1"),
        )
    assert "docker executable" in captured.value.result.stderr
