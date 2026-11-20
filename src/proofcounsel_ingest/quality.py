"""Quality scoring and fail-closed terminal-state decisions."""

from __future__ import annotations

from collections.abc import Sequence

from proofcounsel_ingest.config import QualityConfig
from proofcounsel_ingest.extractors.base import ExtractionResult
from proofcounsel_ingest.models import IngestionStatus, QualityReport, StructureNode


def evaluate_extraction(
    result: ExtractionResult,
    *,
    structure_nodes: Sequence[StructureNode],
    indexes_persisted: bool,
    quality_config: QualityConfig,
) -> QualityReport:
    expected = max(result.expected_units, 1)
    profile_name = str(result.metadata.get("quality_profile", "text"))
    profile = quality_config.profile(profile_name)
    page_coverage = min(1.0, result.extracted_units / expected)
    nonempty_blocks = [
        block
        for block in result.blocks
        if block.text.strip() and block.metadata.get("queryable_evidence", True)
    ]
    locator_coverage = len([block for block in nonempty_blocks if block.locator]) / max(len(nonempty_blocks), 1)
    known_languages = len([block for block in nonempty_blocks if block.language != "und"])
    language_coverage = known_languages / max(len(nonempty_blocks), 1)
    verified_nodes = [node for node in structure_nodes if node.verified]
    structure_validity = 1.0 if verified_nodes else 0.0

    reasons: list[str] = []
    if not nonempty_blocks:
        reasons.append("no non-empty content blocks")
    if result.ocr_required_units:
        reasons.append(f"OCR required for units: {result.ocr_required_units}")
    if page_coverage < profile.minimum_page_coverage:
        reasons.append(
            f"page coverage {page_coverage:.4f} is below {profile.minimum_page_coverage:.4f}"
        )
    if locator_coverage < quality_config.required_locator_coverage:
        reasons.append(
            f"locator coverage {locator_coverage:.4f} is below {quality_config.required_locator_coverage:.4f}"
        )
    if structure_validity < quality_config.minimum_structure_confidence:
        reasons.append("no verified structure or fallback node")
    if not indexes_persisted:
        reasons.append("retrieval indexes are not persisted")

    queryable = not reasons
    return QualityReport(
        page_coverage=page_coverage,
        nonempty_block_count=len(nonempty_blocks),
        locator_coverage=locator_coverage if nonempty_blocks else 0.0,
        language_coverage=language_coverage,
        structure_validity=structure_validity,
        indexes_persisted=indexes_persisted,
        queryable=queryable,
        reasons=reasons,
    )


def decide_terminal_status(
    result: ExtractionResult,
    quality: QualityReport,
    *,
    ocr_available: bool,
) -> IngestionStatus:
    if result.ocr_required_units and not ocr_available:
        return IngestionStatus.NEEDS_OCR
    if quality.queryable:
        return IngestionStatus.READY
    if result.blocks:
        return IngestionStatus.PARTIAL
    return IngestionStatus.FAILED
