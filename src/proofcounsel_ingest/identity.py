"""Tenant-scoped source identity and immutable artifact keys."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from proofcounsel_ingest.config import ParserConfig
from proofcounsel_ingest.models import SourceIdentity


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def compute_source_identity(
    path: Path,
    tenant_id: UUID,
    detected_mime: str,
    *,
    version_key: str | None = None,
    scope_key: str | None = None,
) -> SourceIdentity:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"source is not a regular file: {resolved}")
    source_hash = sha256_file(resolved)
    document_namespace = source_hash
    if version_key is not None:
        document_namespace = f"{document_namespace}:{version_key}"
    if scope_key is not None:
        document_namespace = f"{document_namespace}:{scope_key}"
    return SourceIdentity(
        tenant_id=tenant_id,
        document_id=uuid5(tenant_id, document_namespace),
        source_sha256=source_hash,
        byte_size=resolved.stat().st_size,
        detected_mime=detected_mime,
        original_filename=resolved.name,
    )


def compute_parser_config_hash(
    parser_config: ParserConfig,
    *,
    feature_flags: Mapping[str, bool],
    backend_versions: Mapping[str, str | None] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "parser_config": parser_config.model_dump(mode="json"),
        "feature_flags": dict(sorted(feature_flags.items())),
        "backend_versions": dict(sorted((backend_versions or {}).items())),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def artifact_key(identity: SourceIdentity, extractor_version: str, parser_config_hash: str) -> str:
    return "/".join(
        (
            str(identity.tenant_id),
            identity.source_sha256,
            _safe_component(extractor_version),
            parser_config_hash,
        )
    )


def _safe_component(value: str) -> str:
    normalized = "".join(character if character.isalnum() or character in ".-_" else "-" for character in value)
    normalized = normalized.strip(".-")
    if not normalized:
        raise ValueError("artifact key component cannot be empty")
    return normalized
