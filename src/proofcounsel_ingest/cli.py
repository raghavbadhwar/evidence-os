"""Command-line entry point for the production runtime."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from proofcounsel_ingest import __version__
from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.orchestrator import ingest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proofcounsel-ingest",
        description="Quality-gated PageIndex-compatible document ingestion and retrieval.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="Report configured parser capabilities without changing state.")
    doctor.set_defaults(handler=_doctor)

    schemas = subparsers.add_parser("schemas", help="Print the canonical schema version.")
    schemas.set_defaults(handler=_schemas)

    ingest_parser = subparsers.add_parser("ingest", help="Validate and ingest one document into immutable artifacts.")
    ingest_parser.add_argument("input", type=Path, help="Source file path.")
    ingest_parser.add_argument("--artifact-root", type=Path, help="Override the artifact root.")
    ingest_parser.add_argument("--tenant-id", type=UUID, help="Override the tenant UUID.")
    ingest_parser.add_argument("--enable-ocr", action="store_true", help="Enable OCR only when a backend is available.")
    ingest_parser.set_defaults(handler=_ingest)

    return parser


def _doctor(_args: argparse.Namespace) -> int:
    settings = Settings()
    parser_config = ParserConfig.load(settings.parser_config)
    result = {
        "runtime_version": __version__,
        "schema_version": parser_config.schema_version,
        "extractor_version": parser_config.extractor_version,
        "parser_config_hash": parser_config.config_hash(),
        "environment": settings.environment,
        "capabilities": settings.capabilities(),
        "feature_flags": {
            "ingest_v2_enabled": settings.ingest_v2_enabled,
            "ocr_enabled": settings.ocr_enabled,
            "semantic_retrieval_enabled": settings.semantic_retrieval_enabled,
            "llm_tree_reasoning_enabled": settings.llm_tree_reasoning_enabled,
            "citation_enforcement_enabled": settings.citation_enforcement_enabled,
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _schemas(_args: argparse.Namespace) -> int:
    print(json.dumps({"schema_version": "1.0"}))
    return 0


def _ingest(args: argparse.Namespace) -> int:
    settings = Settings()
    updates = {}
    if args.artifact_root:
        updates["artifact_root"] = args.artifact_root
    if args.tenant_id:
        updates["tenant_id"] = args.tenant_id
    if args.enable_ocr:
        updates["ocr_enabled"] = True
    if updates:
        settings = settings.model_copy(update=updates)
    outcome = ingest(args.input, settings=settings)
    print(outcome.model_dump_json(indent=2))
    return outcome.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
