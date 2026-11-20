from __future__ import annotations

from uuid import UUID

from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.identity import artifact_key, compute_parser_config_hash, compute_source_identity


def test_source_identity_is_stable_within_tenant_and_scoped_across_tenants(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "document.txt"
    source.write_text("same bytes", encoding="utf-8")
    tenant_a = UUID("00000000-0000-0000-0000-000000000001")
    tenant_b = UUID("00000000-0000-0000-0000-000000000002")

    first = compute_source_identity(source, tenant_a, "text/plain")
    repeated = compute_source_identity(source, tenant_a, "text/plain")
    other_tenant = compute_source_identity(source, tenant_b, "text/plain")

    assert first.source_sha256 == repeated.source_sha256 == other_tenant.source_sha256
    assert first.document_id == repeated.document_id
    assert first.document_id != other_tenant.document_id

    versioned = compute_source_identity(source, tenant_a, "text/plain", version_key="parser-config-v2")
    assert versioned.source_sha256 == first.source_sha256
    assert versioned.document_id != first.document_id

    matter_scoped = compute_source_identity(
        source,
        tenant_a,
        "text/plain",
        version_key="parser-config-v2",
        scope_key="matter-a",
    )
    other_matter = compute_source_identity(
        source,
        tenant_a,
        "text/plain",
        version_key="parser-config-v2",
        scope_key="matter-b",
    )
    assert matter_scoped.document_id != other_matter.document_id
    assert matter_scoped.source_sha256 == other_matter.source_sha256 == first.source_sha256


def test_artifact_key_changes_with_bytes_config_and_tenant(tmp_path) -> None:  # type: ignore[no-untyped-def]
    first_path = tmp_path / "first" / "document.txt"
    second_path = tmp_path / "second" / "document.txt"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.write_text("alpha", encoding="utf-8")
    second_path.write_text("beta", encoding="utf-8")
    tenant = UUID("00000000-0000-0000-0000-000000000001")
    config = ParserConfig.load(Settings(_env_file=None).parser_config)
    hash_without_ocr = compute_parser_config_hash(config, feature_flags={"ocr_enabled": False})
    hash_with_ocr = compute_parser_config_hash(config, feature_flags={"ocr_enabled": True})
    first = compute_source_identity(first_path, tenant, "text/plain")
    second = compute_source_identity(second_path, tenant, "text/plain")

    assert artifact_key(first, config.extractor_version, hash_without_ocr) != artifact_key(
        second, config.extractor_version, hash_without_ocr
    )
    assert artifact_key(first, config.extractor_version, hash_without_ocr) != artifact_key(
        first, config.extractor_version, hash_with_ocr
    )
