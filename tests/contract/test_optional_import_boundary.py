from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_core_cli_import_does_not_require_optional_format_dependencies() -> None:
    script = """
import importlib.abc
import sys

blocked = {'pypdf', 'lxml', 'docx', 'openpyxl', 'pptx', 'PIL'}
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == item or fullname.startswith(item + '.') for item in blocked):
            raise ImportError('optional dependency imported during core startup: ' + fullname)
        return None
sys.meta_path.insert(0, Blocker())
from proofcounsel_ingest.cli import build_parser
assert build_parser().prog == 'proofcounsel-ingest'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={"PATH": str(Path(sys.executable).parent), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
