"""Versioned contracts shared by extraction, indexing, retrieval, and citation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class StrictModel(BaseModel):
    """Reject unknown fields so stage boundaries fail closed."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)


class DocumentFormat(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    HTML = "html"
    TEXT = "text"


class BlockType(StrEnum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_ROW = "table_row"
    FORMULA = "formula"
    FIGURE = "figure"
    CAPTION = "caption"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"
    COMMENT = "comment"
    NOTE = "note"
    CODE = "code"
    PAGE = "page"


class IngestionStatus(StrEnum):
    RECEIVED = "received"
    VALIDATING = "validating"
    QUARANTINED = "quarantined"
    EXTRACTING = "extracting"
    NEEDS_OCR = "needs_ocr"
    STRUCTURING = "structuring"
    INDEXING = "indexing"
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"
    DELETED = "deleted"


class CitationVerificationStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class FailureRecord(StrictModel):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)
    stage: str = Field(min_length=1, max_length=120)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class SourceIdentity(StrictModel):
    schema_version: str = SCHEMA_VERSION
    tenant_id: UUID
    document_id: UUID = Field(default_factory=uuid4)
    source_sha256: Sha256
    byte_size: int = Field(ge=0)
    detected_mime: str = Field(min_length=1, max_length=255)
    original_filename: str = Field(min_length=1, max_length=1024)


class IngestionRun(StrictModel):
    schema_version: str = SCHEMA_VERSION
    run_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    status: IngestionStatus = IngestionStatus.RECEIVED
    extractor_version: str = Field(min_length=1, max_length=120)
    parser_config_hash: Sha256
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    failure: FailureRecord | None = None

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> IngestionRun:
        terminal = {
            IngestionStatus.QUARANTINED,
            IngestionStatus.READY,
            IngestionStatus.PARTIAL,
            IngestionStatus.FAILED,
            IngestionStatus.DELETED,
        }
        if self.status in terminal and self.finished_at is None:
            raise ValueError("terminal ingestion runs require finished_at")
        if self.status == IngestionStatus.FAILED and self.failure is None:
            raise ValueError("failed ingestion runs require failure details")
        if self.failure is not None and self.status != IngestionStatus.FAILED:
            raise ValueError("failure details are only valid for failed runs")
        return self


class SourceLocator(StrictModel):
    schema_version: str = SCHEMA_VERSION
    format: DocumentFormat
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    part: str | None = Field(default=None, max_length=1024)
    paragraph_id: str | None = Field(default=None, max_length=255)
    table_id: str | None = Field(default=None, max_length=255)
    cell_range: str | None = Field(default=None, max_length=255)
    slide: int | None = Field(default=None, ge=1)
    shape_id: str | None = Field(default=None, max_length=255)
    dom_path: str | None = Field(default=None, max_length=4096)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_format_locator(self) -> SourceLocator:
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
                raise ValueError("bbox must have non-negative origin and positive area")

        if self.format in {DocumentFormat.PDF, DocumentFormat.IMAGE}:
            if self.page is None or self.bbox is None:
                raise ValueError("PDF and image locators require page and bbox")
        elif self.format == DocumentFormat.DOCX:
            if not self.part or not any((self.paragraph_id, self.table_id, self.cell_range)):
                raise ValueError("DOCX locators require part and an element identifier")
        elif self.format == DocumentFormat.PPTX:
            if self.slide is None or not self.shape_id:
                raise ValueError("PPTX locators require slide and shape_id")
        elif self.format == DocumentFormat.XLSX:
            if not self.part or not self.cell_range:
                raise ValueError("XLSX locators require sheet part and cell_range")
        elif self.format == DocumentFormat.HTML:
            if not self.dom_path:
                raise ValueError("HTML locators require dom_path")
        elif self.format == DocumentFormat.TEXT and (
            self.line_start is None or self.line_end is None or self.line_end < self.line_start
        ):
            raise ValueError("text locators require an ordered line range")
        return self


class ContentBlock(StrictModel):
    schema_version: str = SCHEMA_VERSION
    block_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    ordinal: int = Field(ge=0)
    block_type: BlockType
    text: str = Field(min_length=1)
    language: str = Field(default="und", min_length=2, max_length=35)
    confidence: Confidence
    locator: SourceLocator
    parent_block_id: UUID | None = None
    text_sha256: Sha256
    metadata: dict[str, Any] = Field(default_factory=dict)


class PageRecord(StrictModel):
    schema_version: str = SCHEMA_VERSION
    document_id: UUID
    page_number: int = Field(ge=1)
    native_text_coverage: Confidence
    ocr_used: bool = False
    ocr_confidence: Confidence | None = None
    image_artifact_id: UUID | None = None
    warnings: list[str] = Field(default_factory=list)
    quality_metrics: dict[str, float] = Field(default_factory=dict)


class StructureNode(StrictModel):
    schema_version: str = SCHEMA_VERSION
    node_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    parent_node_id: UUID | None = None
    title: str = Field(min_length=1, max_length=2000)
    kind: str = Field(min_length=1, max_length=120)
    ordinal: int = Field(ge=0)
    start_block_id: UUID
    end_block_id: UUID
    method: str = Field(min_length=1, max_length=120)
    confidence: Confidence
    verified: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class QualityReport(StrictModel):
    schema_version: str = SCHEMA_VERSION
    page_coverage: Confidence
    nonempty_block_count: int = Field(ge=0)
    locator_coverage: Confidence
    language_coverage: Confidence
    structure_validity: Confidence
    indexes_persisted: bool = False
    queryable: bool = False
    reasons: list[str] = Field(default_factory=list)


class RetrievalHit(StrictModel):
    schema_version: str = SCHEMA_VERSION
    tenant_id: UUID
    document_id: UUID
    source_sha256: Sha256
    block_id: UUID
    node_id: UUID | None = None
    channel: str = Field(min_length=1, max_length=120)
    raw_score: float
    fused_score: float
    rank: int = Field(ge=1)
    locator: SourceLocator
    text: str = Field(min_length=1)


class VerifiedCitation(StrictModel):
    schema_version: str = SCHEMA_VERSION
    citation_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    document_id: UUID
    source_sha256: Sha256
    answer_span: str = Field(min_length=1)
    block_id: UUID
    quoted_text_sha256: Sha256
    locator: SourceLocator
    verification_status: CitationVerificationStatus
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_rejection_reason(self) -> VerifiedCitation:
        if self.verification_status == CitationVerificationStatus.VERIFIED and self.failure_reason:
            raise ValueError("verified citations cannot have a failure_reason")
        if self.verification_status == CitationVerificationStatus.REJECTED and not self.failure_reason:
            raise ValueError("rejected citations require a failure_reason")
        return self


class IndexVersion(StrictModel):
    schema_version: str = SCHEMA_VERSION
    index_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    source_sha256: Sha256
    index_type: str = Field(min_length=1, max_length=120)
    implementation: str = Field(min_length=1, max_length=255)
    config_hash: Sha256
    descriptor: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
