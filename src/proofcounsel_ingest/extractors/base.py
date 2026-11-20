from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from proofcounsel_ingest.config import ParserConfig, Settings
from proofcounsel_ingest.models import ContentBlock, DocumentFormat, PageRecord, Sha256, SourceIdentity
from proofcounsel_ingest.security.sandbox import SandboxProfile


class EvidenceVariant(BaseModel):
    unit: int = Field(ge=1)
    method: str = Field(min_length=1)
    text: str
    text_sha256: Sha256
    confidence: float = Field(ge=0, le=1)
    selected: bool = False


class ExtractionResult(BaseModel):
    format: DocumentFormat
    blocks: list[ContentBlock] = Field(default_factory=list)
    pages: list[PageRecord] = Field(default_factory=list)
    expected_units: int = Field(ge=0)
    extracted_units: int = Field(ge=0)
    ocr_required_units: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_variants: list[EvidenceVariant] = Field(default_factory=list)
    derived_artifacts: dict[str, Path] = Field(default_factory=dict, exclude=True)


class ExtractionContext(BaseModel):
    settings: Settings
    parser_config: ParserConfig
    sandbox_profile: SandboxProfile
    work_dir: Path | None = None
    telemetry_sink: Any | None = Field(default=None, exclude=True)

    model_config = {"arbitrary_types_allowed": True}


class ExtractorPort(Protocol):
    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult: ...


class UnsupportedFormatError(ValueError):
    pass
