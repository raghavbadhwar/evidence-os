"""Quality-gated ingestion orchestration with immutable audit artifacts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from proofcounsel_ingest.artifacts import ArtifactWriter, write_run_record
from proofcounsel_ingest.assets import AssetRecord, persist_verification_assets
from proofcounsel_ingest.audit import HashChainedAuditLog
from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.extractors.base import ExtractionContext, ExtractionResult
from proofcounsel_ingest.extractors.router import ExtractorRouter
from proofcounsel_ingest.extractors.tika_fallback import TikaFallbackExtractor
from proofcounsel_ingest.identity import artifact_key, compute_parser_config_hash, compute_source_identity
from proofcounsel_ingest.invariants import validate_queryable
from proofcounsel_ingest.models import (
    ContentBlock,
    FailureRecord,
    IndexVersion,
    IngestionRun,
    IngestionStatus,
    QualityReport,
    SourceIdentity,
    StructureNode,
)
from proofcounsel_ingest.pageindex.adapter import to_pageindex_payload
from proofcounsel_ingest.quality import decide_terminal_status, evaluate_extraction
from proofcounsel_ingest.retrieval.repository import (
    RetrievalRepository,
    close_repository,
    repository_for_settings,
)
from proofcounsel_ingest.security.file_validation import UploadValidation, validate_upload
from proofcounsel_ingest.security.sandbox import SandboxProfile
from proofcounsel_ingest.security.scanner import DevelopmentNoOpScanner, MalwareScanner
from proofcounsel_ingest.status import require_transition
from proofcounsel_ingest.structure.tree import build_verified_tree
from proofcounsel_ingest.telemetry import NullTelemetrySink, TelemetrySink, timed_stage

EXIT_CODES: dict[IngestionStatus, int] = {
    IngestionStatus.READY: 0,
    IngestionStatus.NEEDS_OCR: 10,
    IngestionStatus.PARTIAL: 11,
    IngestionStatus.QUARANTINED: 20,
    IngestionStatus.FAILED: 30,
    IngestionStatus.DELETED: 31,
    IngestionStatus.RECEIVED: 32,
    IngestionStatus.VALIDATING: 32,
    IngestionStatus.EXTRACTING: 32,
    IngestionStatus.STRUCTURING: 32,
    IngestionStatus.INDEXING: 32,
}


class IngestionOutcome(BaseModel):
    status: IngestionStatus
    exit_code: int
    identity: SourceIdentity
    run: IngestionRun
    validation: UploadValidation
    quality: QualityReport | None = None
    artifact_key: str | None = None
    artifact_path: Path | None = None
    reused: bool = False
    warnings: list[str] = []


def ingest(
    source: Path,
    *,
    settings: Settings | None = None,
    parser_config: ParserConfig | None = None,
    router: ExtractorRouter | None = None,
    scanner: MalwareScanner | None = None,
    telemetry_sink: TelemetrySink | None = None,
) -> IngestionOutcome:
    runtime_settings = settings or Settings()
    sink = telemetry_sink or NullTelemetrySink()
    config = parser_config or ParserConfig.load(runtime_settings.parser_config)
    extractor_router = router or ExtractorRouter(
        fallback=TikaFallbackExtractor()
        if runtime_settings.resolved_binary("tika_binary", "tika") or runtime_settings.tika_jar
        else None
    )
    malware_scanner = scanner or DevelopmentNoOpScanner()
    source_path = source.expanduser().resolve(strict=True)
    with timed_stage(
        sink,
        tenant_id=runtime_settings.tenant_id,
        document_id=None,
        source_sha256=None,
        stage="validation",
    ):
        validation = validate_upload(source_path, config.limits)
    feature_flags = {
        "ocr_enabled": runtime_settings.ocr_enabled,
        "semantic_retrieval_enabled": runtime_settings.semantic_retrieval_enabled,
        "llm_tree_reasoning_enabled": runtime_settings.llm_tree_reasoning_enabled,
        "citation_enforcement_enabled": runtime_settings.citation_enforcement_enabled,
    }
    effective_config_hash = compute_parser_config_hash(
        config,
        feature_flags=feature_flags,
        backend_versions=runtime_settings.capabilities(),
    )
    identity = compute_source_identity(
        source_path,
        runtime_settings.tenant_id,
        validation.detected_mime,
        version_key=effective_config_hash,
        scope_key=str(runtime_settings.matter_id) if runtime_settings.matter_id else None,
    )
    run = IngestionRun(
        document_id=identity.document_id,
        extractor_version=config.extractor_version,
        parser_config_hash="0" * 64,
    )
    run = _transition(run, IngestionStatus.VALIDATING)
    run = run.model_copy(update={"parser_config_hash": effective_config_hash})

    with timed_stage(
        sink,
        tenant_id=identity.tenant_id,
        document_id=identity.document_id,
        source_sha256=identity.source_sha256,
        stage="malware_scan",
        metadata={"configured": scan_configured(malware_scanner)},
    ):
        scan = malware_scanner.scan(source_path)
    sandbox_unconfigured = runtime_settings.environment == "production" and (
        runtime_settings.sandbox_mode != "docker" or not runtime_settings.sandbox_image
    )
    if (
        not validation.accepted
        or not scan.clean
        or (runtime_settings.environment == "production" and not scan.configured)
        or sandbox_unconfigured
    ):
        reasons = list(validation.reasons)
        if not scan.clean:
            reasons.append(f"malware scanner rejected upload: {scan.details}")
        if runtime_settings.environment == "production" and not scan.configured:
            reasons.append("production requires a configured malware scanner")
        if sandbox_unconfigured:
            reasons.append("production requires sandbox_mode=docker and sandbox_image")
        run = _transition(run, IngestionStatus.QUARANTINED, warnings=reasons)
        outcome = IngestionOutcome(
            status=run.status,
            exit_code=EXIT_CODES[run.status],
            identity=identity,
            run=run,
            validation=validation,
            warnings=reasons,
        )
        _audit_outcome(runtime_settings.artifact_root, outcome)
        return outcome

    key = artifact_key(identity, config.extractor_version, effective_config_hash)
    try:
        with ArtifactWriter(runtime_settings.artifact_root, key) as artifacts:
            if artifacts.reused:
                stored = artifacts.read_existing_json("ingestion_manifest.json")
                stored_status = IngestionStatus(stored["status"])
                quality_payload = stored.get("quality")
                quality = QualityReport.model_validate(quality_payload) if quality_payload else None
                _ensure_reused_index(
                    artifacts=artifacts,
                    identity=identity,
                    status=stored_status,
                    quality=quality,
                    config=config,
                    config_hash=run.parser_config_hash,
                    settings=runtime_settings,
                )
                run = _transition_to_reused_status(run, stored_status)
                outcome = IngestionOutcome(
                    status=run.status,
                    exit_code=EXIT_CODES[run.status],
                    identity=identity,
                    run=run,
                    validation=validation,
                    quality=quality,
                    artifact_key=key,
                    artifact_path=artifacts.final_path,
                    reused=True,
                    warnings=list(stored.get("warnings", [])),
                )
            else:
                outcome = _build_new_artifact(
                    source_path,
                    identity=identity,
                    run=run,
                    validation=validation,
                    settings=runtime_settings,
                    telemetry_sink=sink,
                    config=config,
                    router=extractor_router,
                    artifacts=artifacts,
                    key=key,
                )
    except Exception as exc:
        failure = FailureRecord(
            code=type(exc).__name__,
            message=str(exc) or type(exc).__name__,
            stage=run.status.value,
            retryable=_is_retryable(exc),
        )
        run = _fail_from_current(run, failure)
        outcome = IngestionOutcome(
            status=run.status,
            exit_code=EXIT_CODES[run.status],
            identity=identity,
            run=run,
            validation=validation,
            artifact_key=key,
            warnings=[failure.message],
        )

    _audit_outcome(runtime_settings.artifact_root, outcome)
    return outcome


def _build_new_artifact(
    source: Path,
    *,
    identity: SourceIdentity,
    run: IngestionRun,
    validation: UploadValidation,
    settings: Settings,
    telemetry_sink: TelemetrySink,
    config: ParserConfig,
    router: ExtractorRouter,
    artifacts: ArtifactWriter,
    key: str,
) -> IngestionOutcome:
    run = _transition(run, IngestionStatus.EXTRACTING)
    profile = SandboxProfile(
        timeout_seconds=config.limits.parser_timeout_seconds,
        cpu_seconds=max(1, int(config.limits.parser_timeout_seconds)),
        max_output_bytes=config.limits.max_parser_output_bytes,
        network_isolated=settings.sandbox_mode == "docker",
        container_image=settings.sandbox_image,
    )
    extractor = router.for_mime(validation.detected_mime)
    with tempfile.TemporaryDirectory(prefix="proofcounsel-ingest-") as temporary:
        extraction_context = ExtractionContext(
            settings=settings,
            parser_config=config,
            sandbox_profile=profile,
            work_dir=Path(temporary),
            telemetry_sink=telemetry_sink,
        )
        with timed_stage(
            telemetry_sink,
            tenant_id=identity.tenant_id,
            document_id=identity.document_id,
            source_sha256=identity.source_sha256,
            stage="extraction",
            metadata={"mime": validation.detected_mime},
        ):
            extraction = extractor.extract(source, identity, extraction_context)
        with timed_stage(
            telemetry_sink,
            tenant_id=identity.tenant_id,
            document_id=identity.document_id,
            source_sha256=identity.source_sha256,
            stage="assets",
            metadata={"format": extraction.format.value},
        ):
            assets = persist_verification_assets(source, extraction, extraction_context, artifacts)
        derived_artifacts = []
        for relative_path, derived_path in sorted(extraction.derived_artifacts.items()):
            payload = derived_path.read_bytes()
            destination = f"derived/{relative_path}"
            artifacts.write_bytes(destination, payload)
            derived_artifacts.append(
                {
                    "relative_path": destination,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_size": len(payload),
                }
            )

    ocrmypdf_backend = settings.resolved_binary("ocrmypdf_binary", "ocrmypdf")
    fallback_ocr_backend = settings.resolved_binary("pdftoppm_binary", "pdftoppm") and settings.resolved_binary(
        "tesseract_binary", "tesseract"
    )
    ocr_available = bool(settings.ocr_enabled and (ocrmypdf_backend or fallback_ocr_backend))
    pageindex_payload: dict[str, Any] | None = None
    indexed_repository: RetrievalRepository | None = None
    index_persisted = False
    try:
        if extraction.ocr_required_units and not ocr_available:
            nodes: list[StructureNode] = []
            indexes: list[IndexVersion] = []
            quality = evaluate_extraction(
                extraction,
                structure_nodes=nodes,
                indexes_persisted=False,
                quality_config=config.quality,
            )
            run = _transition(run, IngestionStatus.NEEDS_OCR, finish_nonterminal=True)
        else:
            run = _transition(run, IngestionStatus.STRUCTURING)
            with timed_stage(
                telemetry_sink,
                tenant_id=identity.tenant_id,
                document_id=identity.document_id,
                source_sha256=identity.source_sha256,
                stage="structure",
            ):
                nodes, structure_report = build_verified_tree(
                    extraction,
                    minimum_confidence=config.quality.minimum_structure_confidence,
                )
            extraction.metadata["structure_validation"] = structure_report.model_dump(mode="json")
            pageindex_payload = (
                to_pageindex_payload(identity.original_filename, nodes, extraction.blocks) if nodes else None
            )
            run = _transition(run, IngestionStatus.INDEXING)
            queryable_blocks = [
                block for block in extraction.blocks if block.metadata.get("queryable_evidence", True)
            ]
            index_candidate = bool(queryable_blocks and nodes)
            quality = evaluate_extraction(
                extraction,
                structure_nodes=nodes,
                indexes_persisted=index_candidate,
                quality_config=config.quality,
            )
            terminal = decide_terminal_status(extraction, quality, ocr_available=ocr_available)
            indexes = []
            if terminal == IngestionStatus.READY:
                indexed_repository = repository_for_settings(settings)
                semantic_descriptor = settings.semantic_index_descriptor()
                index_descriptor = {"semantic": semantic_descriptor} if semantic_descriptor else {}
                indexes = [
                    IndexVersion(
                        document_id=identity.document_id,
                        source_sha256=identity.source_sha256,
                        index_type="lexical_and_tree",
                        implementation=indexed_repository.implementation,
                        config_hash=run.parser_config_hash,
                        descriptor=index_descriptor,
                    )
                ]
                with timed_stage(
                    telemetry_sink,
                    tenant_id=identity.tenant_id,
                    document_id=identity.document_id,
                    source_sha256=identity.source_sha256,
                    stage="indexing",
                    metadata={"block_count": len(queryable_blocks), "node_count": len(nodes)},
                ):
                    indexed_repository.index_document(
                        tenant_id=identity.tenant_id,
                        document_id=identity.document_id,
                        source_sha256=identity.source_sha256,
                        config_hash=run.parser_config_hash,
                        blocks=extraction.blocks,
                        nodes=nodes,
                        original_filename=identity.original_filename,
                        detected_mime=identity.detected_mime,
                        byte_size=identity.byte_size,
                        schema_version=config.schema_version,
                        extractor_version=config.extractor_version,
                        quality=quality.model_dump(mode="json"),
                        publish_ready=False,
                        index_descriptor=index_descriptor,
                        matter_id=settings.matter_id,
                    )
                index_persisted = True
                artifacts.write_json(
                    "indexes/lexical.json",
                    {
                        "implementation": indexed_repository.implementation,
                        "block_count": len(queryable_blocks),
                        "node_count": len(nodes),
                        "config_hash": run.parser_config_hash,
                    },
                )
            elif terminal == IngestionStatus.FAILED:
                raise ValueError("extraction produced no queryable evidence")
            else:
                quality = evaluate_extraction(
                    extraction,
                    structure_nodes=nodes,
                    indexes_persisted=False,
                    quality_config=config.quality,
                )
            run = _transition(run, terminal)

        with timed_stage(
            telemetry_sink,
            tenant_id=identity.tenant_id,
            document_id=identity.document_id,
            source_sha256=identity.source_sha256,
            stage="publication",
            metadata={"status": run.status.value},
        ):
            outcome = _finalize_artifact(
                identity=identity,
                run=run,
                validation=validation,
                quality=quality,
                extraction=extraction,
                nodes=nodes,
                indexes=indexes,
                pageindex_payload=pageindex_payload,
                assets=assets,
                derived_artifacts=derived_artifacts,
                artifacts=artifacts,
                config=config,
                key=key,
            )
            if indexed_repository is not None and index_persisted:
                indexed_repository.mark_ready(
                    tenant_id=identity.tenant_id,
                    document_id=identity.document_id,
                    source_sha256=identity.source_sha256,
                    config_hash=run.parser_config_hash,
                )
                close_repository(indexed_repository)
                indexed_repository = None
            return outcome
    except Exception:
        if indexed_repository is not None and index_persisted:
            indexed_repository.delete_document(
                tenant_id=identity.tenant_id,
                document_id=identity.document_id,
            )
        raise
    finally:
        if indexed_repository is not None:
            close_repository(indexed_repository)


def _ensure_reused_index(
    *,
    artifacts: ArtifactWriter,
    identity: SourceIdentity,
    status: IngestionStatus,
    quality: QualityReport | None,
    config: ParserConfig,
    config_hash: str,
    settings: Settings,
) -> None:
    if status != IngestionStatus.READY:
        return
    if quality is None or not quality.queryable:
        raise ValueError("reused ready artifact has no queryable quality report")
    repository = repository_for_settings(settings)
    try:
        _ensure_reused_index_with_repository(
            artifacts=artifacts,
            identity=identity,
            status=status,
            quality=quality,
            config=config,
            config_hash=config_hash,
            settings=settings,
            repository=repository,
        )
    finally:
        close_repository(repository)


def _ensure_reused_index_with_repository(
    *,
    artifacts: ArtifactWriter,
    identity: SourceIdentity,
    status: IngestionStatus,
    quality: QualityReport,
    config: ParserConfig,
    config_hash: str,
    settings: Settings,
    repository: RetrievalRepository,
) -> None:
    if repository.has_index(
        tenant_id=identity.tenant_id,
        document_id=identity.document_id,
        source_sha256=identity.source_sha256,
        config_hash=config_hash,
    ):
        return
    blocks = [ContentBlock.model_validate(item) for item in artifacts.read_existing_jsonl("blocks.jsonl")]
    nodes = [StructureNode.model_validate(item) for item in artifacts.read_existing_json("structure.json")]
    index_versions = [
        IndexVersion.model_validate(item) for item in artifacts.read_existing_json("index_versions.json")
    ]
    validate_queryable(
        status=status,
        quality=quality,
        blocks=blocks,
        nodes=nodes,
        index_versions=index_versions,
        minimum_page_coverage=config.quality.minimum_page_coverage,
    )
    repository.index_document(
        tenant_id=identity.tenant_id,
        document_id=identity.document_id,
        source_sha256=identity.source_sha256,
        config_hash=config_hash,
        blocks=blocks,
        nodes=nodes,
        original_filename=identity.original_filename,
        detected_mime=identity.detected_mime,
        byte_size=identity.byte_size,
        schema_version=config.schema_version,
        extractor_version=config.extractor_version,
        quality=quality.model_dump(mode="json"),
        publish_ready=False,
        index_descriptor={"semantic": settings.semantic_index_descriptor()}
        if settings.semantic_index_descriptor()
        else {},
        matter_id=settings.matter_id,
    )
    repository.mark_ready(
        tenant_id=identity.tenant_id,
        document_id=identity.document_id,
        source_sha256=identity.source_sha256,
        config_hash=config_hash,
    )


def _finalize_artifact(
    *,
    identity: SourceIdentity,
    run: IngestionRun,
    validation: UploadValidation,
    quality: QualityReport,
    extraction: ExtractionResult,
    nodes: list[StructureNode],
    indexes: list[IndexVersion],
    pageindex_payload: dict[str, Any] | None,
    assets: list[AssetRecord],
    derived_artifacts: list[dict[str, Any]],
    artifacts: ArtifactWriter,
    config: ParserConfig,
    key: str,
) -> IngestionOutcome:
    if run.status == IngestionStatus.READY:
        validate_queryable(
            status=run.status,
            quality=quality,
            blocks=extraction.blocks,
            nodes=nodes,
            index_versions=indexes,
            minimum_page_coverage=config.quality.minimum_page_coverage,
        )

    artifacts.write_jsonl("blocks.jsonl", (block.model_dump(mode="json") for block in extraction.blocks))
    artifacts.write_jsonl(
        "evidence_variants.jsonl",
        (variant.model_dump(mode="json") for variant in extraction.evidence_variants),
    )
    artifacts.write_json("pages.json", [page.model_dump(mode="json") for page in extraction.pages])
    artifacts.write_json("structure.json", [node.model_dump(mode="json") for node in nodes])
    if nodes and pageindex_payload is not None:
        artifacts.write_json("pageindex.json", pageindex_payload)
    artifacts.write_json("index_versions.json", [index.model_dump(mode="json") for index in indexes])
    manifest = {
        "schema_version": "1.0",
        "status": run.status.value,
        "artifact_key": key,
        "identity": identity.model_dump(mode="json"),
        "quality": quality.model_dump(mode="json"),
        "extraction": _extraction_summary(extraction),
        "index_versions": [index.model_dump(mode="json") for index in indexes],
        "warnings": extraction.warnings,
        "assets": [asset.model_dump(mode="json") for asset in assets],
        "derived_artifacts": derived_artifacts,
    }
    artifacts.write_json("ingestion_manifest.json", manifest)
    artifact_path = artifacts.commit(
        {
            "tenant_id": str(identity.tenant_id),
            "document_id": str(identity.document_id),
            "source_sha256": identity.source_sha256,
            "extractor_version": config.extractor_version,
            "parser_config_hash": run.parser_config_hash,
            "status": run.status.value,
        }
    )
    return IngestionOutcome(
        status=run.status,
        exit_code=EXIT_CODES[run.status],
        identity=identity,
        run=run,
        validation=validation,
        quality=quality,
        artifact_key=key,
        artifact_path=artifact_path,
        warnings=extraction.warnings,
    )


def _extraction_summary(extraction: ExtractionResult) -> dict[str, Any]:
    return {
        "format": extraction.format.value,
        "expected_units": extraction.expected_units,
        "extracted_units": extraction.extracted_units,
        "ocr_required_units": extraction.ocr_required_units,
        "block_count": len(extraction.blocks),
        "page_count": len(extraction.pages),
        "metadata": extraction.metadata,
    }


def _transition(
    run: IngestionRun,
    target: IngestionStatus,
    *,
    warnings: list[str] | None = None,
    failure: FailureRecord | None = None,
    finish_nonterminal: bool = False,
) -> IngestionRun:
    require_transition(run.status, target)
    terminal = target in {
        IngestionStatus.QUARANTINED,
        IngestionStatus.READY,
        IngestionStatus.PARTIAL,
        IngestionStatus.FAILED,
        IngestionStatus.DELETED,
    }
    update: dict[str, Any] = {
        "status": target,
        "warnings": [*run.warnings, *(warnings or [])],
        "failure": failure,
    }
    if terminal or finish_nonterminal:
        update["finished_at"] = datetime.now(UTC)
    return IngestionRun.model_validate(run.model_copy(update=update).model_dump())


def _transition_to_reused_status(run: IngestionRun, target: IngestionStatus) -> IngestionRun:
    run = _transition(run, IngestionStatus.EXTRACTING)
    if target == IngestionStatus.NEEDS_OCR:
        return _transition(run, target, finish_nonterminal=True)
    if target in {IngestionStatus.READY, IngestionStatus.PARTIAL}:
        run = _transition(run, IngestionStatus.STRUCTURING)
        run = _transition(run, IngestionStatus.INDEXING)
        return _transition(run, target)
    if target == IngestionStatus.FAILED:
        return _transition(
            run,
            target,
            failure=FailureRecord(
                code="ReusedFailedArtifact",
                message="reused artifact records a failed ingestion",
                stage="extracting",
                retryable=False,
            ),
        )
    raise ValueError(f"artifact status cannot be reused: {target.value}")


def _fail_from_current(run: IngestionRun, failure: FailureRecord) -> IngestionRun:
    if run.status in {
        IngestionStatus.QUARANTINED,
        IngestionStatus.READY,
        IngestionStatus.PARTIAL,
        IngestionStatus.FAILED,
        IngestionStatus.DELETED,
    }:
        return run
    return _transition(run, IngestionStatus.FAILED, failure=failure)


def _audit_outcome(root: Path, outcome: IngestionOutcome) -> None:
    payload = outcome.model_dump(mode="json")
    payload["record_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    write_run_record(root, outcome.identity.tenant_id, outcome.run.run_id, payload)
    HashChainedAuditLog(
        root.expanduser().resolve() / "_audit" / f"{outcome.identity.tenant_id}.jsonl",
        outcome.identity.tenant_id,
    ).append(
        action_type="ingestion_run",
        aggregate_type="ingestion_run",
        aggregate_id=outcome.run.run_id,
        payload=payload,
    )


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, OSError))


def scan_configured(scanner: MalwareScanner) -> bool:
    return not isinstance(scanner, DevelopmentNoOpScanner)
