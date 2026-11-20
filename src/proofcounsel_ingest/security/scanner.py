from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class ScanResult(BaseModel):
    clean: bool
    scanner: str
    configured: bool
    details: str = ""


class MalwareScanner(Protocol):
    def scan(self, path: Path) -> ScanResult: ...


class DevelopmentNoOpScanner:
    """Explicit development adapter; production orchestration rejects it."""

    def scan(self, path: Path) -> ScanResult:
        return ScanResult(clean=True, scanner="development-noop", configured=False, details=path.name)

