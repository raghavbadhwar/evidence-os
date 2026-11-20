#!/usr/bin/env python3
"""Deprecated compatibility CLI over the ProofCounsel V2 ingestion runtime."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proofcounsel_ingest.compat import (  # noqa: E402
    legacy_universal_manifest,
    materialize_legacy_builder_outputs,
)
from proofcounsel_ingest.config import Settings  # noqa: E402
from proofcounsel_ingest.orchestrator import ingest  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compatibility wrapper for verified multi-format ProofCounsel ingestion."
    )
    parser.add_argument("--input", required=True, help="Input document/image path.")
    parser.add_argument("--output-dir", default=str(ROOT / "outputs" / "universal_pageindex_run"))
    parser.add_argument("--force", action="store_true", help="Accepted for compatibility; artifacts remain immutable.")
    parser.add_argument("--ocr-lang", default="eng")
    parser.add_argument("--enable-ocr", action="store_true")
    parser.add_argument("--structure-source", choices=["auto", "toc", "headings"], default="auto")
    parser.add_argument("--numbered-label", default="Section")
    parser.add_argument("--min-ascii-ratio", type=float, default=0.72)
    parser.add_argument("--include-all-languages", action="store_true")
    parser.add_argument("--fallback-pages-per-node", type=int, default=10)
    parser.add_argument("--candidate-page-padding", type=int, default=1)
    parser.add_argument("--query", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    source = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    print(
        "warning: outputs/universal_pageindex_ingest.py is deprecated; use `proofcounsel-ingest ingest`.",
        file=sys.stderr,
    )
    settings = Settings(
        artifact_root=output_dir / ".proofcounsel_v2",
        parser_config=ROOT / "config" / "parser-defaults.toml",
        ocr_enabled=args.enable_ocr,
        ocr_languages=args.ocr_lang,
    )
    outcome = ingest(source, settings=settings)
    builder_manifest = materialize_legacy_builder_outputs(
        outcome,
        output_dir=output_dir,
        stem=source.stem,
        queries=args.query,
    )
    manifest = legacy_universal_manifest(
        outcome,
        output_dir=output_dir,
        stem=source.stem,
        builder_manifest=builder_manifest,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

