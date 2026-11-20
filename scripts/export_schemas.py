"""Export deterministic JSON Schema snapshots for public contracts."""

from __future__ import annotations

import json
from pathlib import Path

from proofcounsel_ingest.models import (
    ContentBlock,
    IndexVersion,
    IngestionRun,
    PageRecord,
    QualityReport,
    RetrievalHit,
    SourceIdentity,
    SourceLocator,
    StructureNode,
    VerifiedCitation,
)

MODELS = (
    SourceIdentity,
    IngestionRun,
    SourceLocator,
    ContentBlock,
    PageRecord,
    StructureNode,
    QualityReport,
    RetrievalHit,
    VerifiedCitation,
    IndexVersion,
)


def main() -> int:
    destination = Path("schemas/v1")
    destination.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        path = destination / f"{model.__name__}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
