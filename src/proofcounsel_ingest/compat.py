"""One-release materialization of legacy artifact names and query smoke files."""

from __future__ import annotations

import csv
import json
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from proofcounsel_ingest.orchestrator import IngestionOutcome
from proofcounsel_ingest.pageindex.adapter import search_tree


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug or "document"


def materialize_legacy_builder_outputs(
    outcome: IngestionOutcome,
    *,
    output_dir: Path,
    stem: str,
    queries: Sequence[str],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = slugify(stem)
    structure_path = output_dir / f"{safe_stem}_pageindex_structure.json"
    pages_path = output_dir / f"{safe_stem}_pages.jsonl"
    manifest_path = output_dir / f"{safe_stem}_manifest.json"
    query_csv_path = output_dir / f"{safe_stem}_query_smoke.csv"
    report_path = output_dir / f"{safe_stem}_report.md"

    query_rows: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "status": outcome.status.value,
        "queryable": bool(outcome.quality and outcome.quality.queryable),
        "source_sha256": outcome.identity.source_sha256,
        "reused": outcome.reused,
    }
    if outcome.artifact_path and (outcome.artifact_path / "pageindex.json").exists():
        pageindex = json.loads((outcome.artifact_path / "pageindex.json").read_text(encoding="utf-8"))
        shutil.copyfile(outcome.artifact_path / "pageindex.json", structure_path)
        shutil.copyfile(outcome.artifact_path / "blocks.jsonl", pages_path)
        structure_flat = json.loads((outcome.artifact_path / "structure.json").read_text(encoding="utf-8"))
        diagnostics.update(
            {
                "leaf_nodes": len(structure_flat),
                "top_level_nodes": len(pageindex.get("structure", [])),
                "block_count": sum(1 for _ in pages_path.open(encoding="utf-8")),
            }
        )
        for query in queries:
            matches = search_tree(query, pageindex, limit=5)
            query_rows.append(
                {
                    "query": query,
                    "selected_node_titles": " | ".join(str(match["title"]) for match in matches),
                    "selected_node_ids": " | ".join(str(match["node_id"]) for match in matches),
                    "candidate_page_count": len(matches),
                    "top_pages": " | ".join(str(match["start_index"]) for match in matches),
                }
            )
    else:
        structure_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "doc_name": outcome.identity.original_filename,
                    "status": outcome.status.value,
                    "structure": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        pages_path.write_text("", encoding="utf-8")

    if query_rows:
        with query_csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(query_rows[0]))
            writer.writeheader()
            writer.writerows(query_rows)
    else:
        query_csv_path.unlink(missing_ok=True)

    manifest = {
        "source_pdf": outcome.identity.original_filename,
        "source_sha256": outcome.identity.source_sha256,
        "status": outcome.status.value,
        "structure_path": str(structure_path),
        "pages_path": str(pages_path),
        "query_csv_path": str(query_csv_path) if query_rows else None,
        "report_path": str(report_path),
        "artifact_path": str(outcome.artifact_path) if outcome.artifact_path else None,
        "diagnostics": diagnostics,
        "settings": {"compatibility_release": "0.1"},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(
        "\n".join(
            (
                f"# {outcome.identity.original_filename}: verified ingestion report",
                "",
                f"- Status: `{outcome.status.value}`",
                f"- Source SHA-256: `{outcome.identity.source_sha256}`",
                f"- Queryable: `{diagnostics['queryable']}`",
                f"- Structure: `{structure_path}`",
                f"- Evidence blocks: `{pages_path}`",
                f"- Canonical artifacts: `{outcome.artifact_path}`",
                "",
                "This is a one-release compatibility view over ProofCounsel V2 artifacts.",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def legacy_universal_manifest(
    outcome: IngestionOutcome,
    *,
    output_dir: Path,
    stem: str,
    builder_manifest: dict[str, Any],
) -> dict[str, Any]:
    status = "completed" if outcome.status.value == "ready" else outcome.status.value
    manifest = {
        "source": outcome.identity.original_filename,
        "source_sha256": outcome.identity.source_sha256,
        "status": status,
        "queryable": bool(outcome.quality and outcome.quality.queryable),
        "extraction": {
            "method": "proofcounsel-v2-canonical",
            "ocr_status": "completed"
            if outcome.status.value == "ready" and any("ocr" in warning.lower() for warning in outcome.warnings)
            else outcome.status.value,
        },
        "builder_manifest": builder_manifest,
        "artifact_path": str(outcome.artifact_path) if outcome.artifact_path else None,
        "warnings": outcome.warnings,
    }
    path = output_dir / f"{slugify(stem)}_universal_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
