"""Bounded parser process execution with an OCI production command builder."""

from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, Field


class SandboxProfile(BaseModel):
    timeout_seconds: float = Field(default=300, gt=0)
    cpu_seconds: int = Field(default=300, gt=0)
    memory_bytes: int = Field(default=2 * 1024**3, gt=0)
    max_output_bytes: int = Field(default=1024**3, gt=0)
    max_open_files: int = Field(default=256, gt=2)
    network_isolated: bool = False
    container_image: str | None = None


class CommandResult(BaseModel):
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float
    timed_out: bool = False
    output_truncated: bool = False
    network_isolated: bool = False


class SandboxExecutionError(RuntimeError):
    def __init__(self, message: str, result: CommandResult) -> None:
        super().__init__(message)
        self.result = result


def run_sandboxed(
    argv: Sequence[str],
    *,
    cwd: Path,
    profile: SandboxProfile,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
) -> CommandResult:
    if not argv or not argv[0]:
        raise ValueError("sandbox command cannot be empty")
    working_directory = cwd.expanduser().resolve(strict=True)
    if not working_directory.is_dir():
        raise ValueError("sandbox cwd must be a directory")
    if profile.network_isolated and not profile.container_image:
        result = CommandResult(
            argv=list(argv),
            returncode=-1,
            stdout="",
            stderr="network isolation requires a configured container image",
            elapsed_ms=0,
            network_isolated=False,
        )
        raise SandboxExecutionError("refusing non-isolated execution", result)

    effective_argv = (
        _build_container_argv(list(argv), working_directory, profile) if profile.container_image else list(argv)
    )

    safe_environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": str(working_directory),
        "TMPDIR": str(working_directory),
    }
    if environment:
        safe_environment.update(environment)

    started = time.perf_counter()
    with tempfile.TemporaryFile() as stdout_handle, tempfile.TemporaryFile() as stderr_handle:
        process = subprocess.Popen(
            effective_argv,
            cwd=working_directory,
            env=safe_environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            preexec_fn=_limit_process(profile),
        )
        timed_out = False
        try:
            returncode = process.wait(timeout=profile.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            returncode = process.wait(timeout=5)

        stdout, stdout_truncated = _read_bounded(stdout_handle, profile.max_output_bytes)
        stderr, stderr_truncated = _read_bounded(stderr_handle, profile.max_output_bytes)
    result = CommandResult(
        argv=list(argv),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        timed_out=timed_out,
        output_truncated=stdout_truncated or stderr_truncated,
        network_isolated=profile.network_isolated or bool(profile.container_image),
    )
    if timed_out:
        raise SandboxExecutionError("sandboxed command timed out", result)
    if check and returncode != 0:
        raise SandboxExecutionError(f"sandboxed command exited with status {returncode}", result)
    return result


def build_docker_command(
    *,
    image: str,
    input_path: Path,
    output_path: Path,
    command: Sequence[str],
    profile: SandboxProfile,
) -> list[str]:
    if not image or not command:
        raise ValueError("docker image and command are required")
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        str(profile.memory_bytes),
        "--cpus",
        "1.0",
        "--user",
        "65534:65534",
        "--mount",
        f"type=bind,src={input_path.resolve()},dst=/input/source,readonly",
        "--mount",
        f"type=bind,src={output_path.resolve()},dst=/output",
        image,
        *command,
    ]


def _build_container_argv(argv: list[str], cwd: Path, profile: SandboxProfile) -> list[str]:
    image = profile.container_image
    if not image:
        raise ValueError("container image is required for containerized execution")
    docker = shutil.which("docker")
    if not docker:
        raise SandboxExecutionError(
            "docker is unavailable for the configured isolated sandbox",
            CommandResult(
                argv=argv,
                returncode=-1,
                stdout="",
                stderr="docker executable not found",
                elapsed_ms=0,
                network_isolated=False,
            ),
        )

    command: list[str] = [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        str(profile.memory_bytes),
        "--cpus",
        "1.0",
        "--user",
        "65534:65534",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--mount",
        f"type=bind,src={cwd},dst=/work,rw",
        "--workdir",
        "/work",
    ]
    mapped: list[str] = []
    external_mounts: dict[Path, str] = {}
    for index, value in enumerate(argv):
        candidate = Path(value)
        if index == 0 and candidate.is_absolute():
            mapped.append(candidate.name)
            continue
        if not candidate.is_absolute():
            mapped.append(value)
            continue
        resolved = candidate.resolve()
        if resolved == cwd or cwd in resolved.parents:
            mapped.append(f"/work/{resolved.relative_to(cwd).as_posix()}")
            continue
        parent = resolved.parent
        mount_root = external_mounts.setdefault(parent, f"/inputs/{len(external_mounts)}")
        command.extend(["--mount", f"type=bind,src={parent},dst={mount_root},readonly"])
        mapped.append(f"{mount_root}/{resolved.name}")
    command.extend([image, *mapped])
    return command


def _limit_process(profile: SandboxProfile):  # type: ignore[no-untyped-def]
    def apply_limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (profile.cpu_seconds, profile.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (profile.max_output_bytes, profile.max_output_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (profile.max_open_files, profile.max_open_files))
        with suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_AS, (profile.memory_bytes, profile.memory_bytes))

    return apply_limits


def _read_bounded(handle, limit: int) -> tuple[str, bool]:  # type: ignore[no-untyped-def]
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    payload = handle.read(limit)
    return payload.decode("utf-8", errors="replace"), size > limit
