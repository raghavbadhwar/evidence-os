#!/usr/bin/env python3
"""Deprecated PageIndex-builder CLI over verified canonical evidence blocks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proofcounsel_ingest.compat import materialize_legacy_builder_outputs  # noqa: E402
from proofcounsel_ingest.config import Settings  # noqa: E402
from proofcounsel_ingest.orchestrator import ingest  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compatibility wrapper for verified PageIndex-compatible tree construction."
    )
    parser.add_argument("--pdf", required=True, help="Original PDF path or display source.")
    parser.add_argument("--output-dir", default=str(ROOT / "outputs" / "pageindex_first_run"))
    parser.add_argument("--text-path", help="Optional pre-extracted text source.")
    parser.add_argument("--force-text", action="store_true")
    parser.add_argument("--structure-source", choices=["auto", "toc", "headings"], default="auto")
    parser.add_argument("--numbered-label", default="Section")
    parser.add_argument("--min-ascii-ratio", type=float, default=0.72)
    parser.add_argument("--include-all-languages", action="store_true")
    parser.add_argument("--fallback-pages-per-node", type=int, default=10)
    parser.add_argument("--candidate-page-padding", type=int, default=1)
    parser.add_argument("--toc-pages", type=int, default=30)
    parser.add_argument("--min-toc-entries", type=int, default=3)
    parser.add_argument("--min-leaf-nodes", type=int, default=3)
    parser.add_argument("--node-limit", type=int, default=5)
    parser.add_argument("--page-limit", type=int, default=8)
    parser.add_argument("--query", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    pdf_path = Path(args.pdf).expanduser().resolve()
    source = Path(args.text_path).expanduser().resolve() if args.text_path else pdf_path
    output_dir = Path(args.output_dir).expanduser().resolve()
    print(
        "warning: outputs/pageindex_first_builder.py is deprecated; use `proofcounsel-ingest ingest`.",
        file=sys.stderr,
    )
    settings = Settings(
        artifact_root=output_dir / ".proofcounsel_v2",
        parser_config=ROOT / "config" / "parser-defaults.toml",
    )
    outcome = ingest(source, settings=settings)
    manifest = materialize_legacy_builder_outputs(
        outcome,
        output_dir=output_dir,
        stem=pdf_path.stem,
        queries=args.query,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
