"""Validated runtime and parser configuration."""

from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LimitsConfig(BaseModel):
    max_file_bytes: int = Field(gt=0)
    max_pages: int = Field(gt=0)
    max_image_pixels: int = Field(gt=0)
    max_spreadsheet_rows: int = Field(gt=0)
    max_archive_entries: int = Field(gt=0)
    max_uncompressed_bytes: int = Field(gt=0)
    parser_timeout_seconds: float = Field(gt=0)
    max_parser_output_bytes: int = Field(gt=0)


class QualityProfileConfig(BaseModel):
    minimum_page_coverage: float = Field(ge=0, le=1)
    minimum_ocr_confidence: float = Field(ge=0, le=1)


class QualityConfig(BaseModel):
    minimum_page_coverage: float = Field(ge=0, le=1)
    minimum_native_chars_per_page: int = Field(ge=0)
    minimum_native_printable_ratio: float = Field(ge=0, le=1)
    minimum_ocr_confidence: float = Field(ge=0, le=1)
    minimum_structure_confidence: float = Field(ge=0, le=1)
    required_locator_coverage: float = Field(ge=0, le=1)
    maximum_unicode_replacement_ratio: float = Field(ge=0, le=1)
    maximum_duplicate_glyph_ratio: float = Field(ge=0, le=1)
    profiles: dict[str, QualityProfileConfig]

    def profile(self, name: str) -> QualityProfileConfig:
        return self.profiles.get(
            name,
            QualityProfileConfig(
                minimum_page_coverage=self.minimum_page_coverage,
                minimum_ocr_confidence=self.minimum_ocr_confidence,
            ),
        )


class RetrievalConfig(BaseModel):
    fused_candidate_limit: int = Field(gt=0, le=1000)
    reranker_candidate_limit: int = Field(gt=0, le=1000)
    answer_context_block_limit: int = Field(gt=0, le=100)
    tree_range_block_limit: int = Field(default=64, gt=0, le=1000)
    semantic_fallback_threshold: float = Field(ge=0, le=1)
    exact_reference_threshold: float = Field(ge=0, le=1)


class ParserConfig(BaseModel):
    schema_version: str
    extractor_version: str
    limits: LimitsConfig
    quality: QualityConfig
    retrieval: RetrievalConfig

    @classmethod
    def load(cls, path: Path) -> ParserConfig:
        with path.open("rb") as handle:
            return cls.model_validate(tomllib.load(handle))

    def config_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROOFCOUNSEL_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    sandbox_mode: Literal["local", "docker"] = "local"
    sandbox_image: str | None = None
    tenant_id: UUID = UUID("00000000-0000-0000-0000-000000000001")
    user_id: UUID | None = None
    actor_role: Literal["user", "service_worker", "admin"] = "service_worker"
    matter_id: UUID | None = None
    artifact_root: Path = Path("artifacts")
    database_url: str = "sqlite:///./artifacts/proofcounsel.db"
    parser_config: Path = Path("config/parser-defaults.toml")
    ingest_v2_enabled: bool = False
    ocr_enabled: bool = False
    semantic_retrieval_enabled: bool = False
    semantic_index_model: str | None = None
    semantic_index_dimensions: int | None = Field(default=None, gt=0)
    semantic_index_version: str | None = None
    llm_tree_reasoning_enabled: bool = False
    citation_enforcement_enabled: bool = True
    ocr_languages: str = "eng"
    pdftotext_binary: str | None = None
    pdftocairo_binary: str | None = None
    pdftoppm_binary: str | None = None
    pdfinfo_binary: str | None = None
    ocrmypdf_binary: str | None = None
    tesseract_binary: str | None = None
    soffice_binary: str | None = None
    java_binary: str | None = None
    tika_binary: str | None = None
    tika_jar: Path | None = None

    @model_validator(mode="after")
    def validate_semantic_configuration(self) -> Settings:
        descriptor_fields = (
            self.semantic_index_model,
            self.semantic_index_dimensions,
            self.semantic_index_version,
        )
        if self.semantic_retrieval_enabled and any(value is None for value in descriptor_fields):
            raise ValueError(
                "semantic retrieval requires semantic_index_model, "
                "semantic_index_dimensions, and semantic_index_version"
            )
        return self

    @model_validator(mode="after")
    def validate_actor_configuration(self) -> Settings:
        if self.actor_role == "user" and self.user_id is None:
            raise ValueError("user actor_role requires user_id")
        return self

    def semantic_index_descriptor(self) -> dict[str, str | int] | None:
        fields = (
            self.semantic_index_model,
            self.semantic_index_dimensions,
            self.semantic_index_version,
        )
        if any(value is None for value in fields):
            return None
        model, dimensions, version = fields
        assert model is not None and dimensions is not None and version is not None
        return {"model": model, "dimensions": dimensions, "version": version}

    def resolved_binary(self, field_name: str, default_name: str) -> str | None:
        configured = getattr(self, field_name)
        if configured:
            path = Path(configured).expanduser()
            return str(path.resolve()) if path.exists() else None
        return shutil.which(default_name)

    def capabilities(self) -> dict[str, Any]:
        return {
            "pdftotext": self.resolved_binary("pdftotext_binary", "pdftotext"),
            "pdftocairo": self.resolved_binary("pdftocairo_binary", "pdftocairo"),
            "pdftoppm": self.resolved_binary("pdftoppm_binary", "pdftoppm"),
            "pdfinfo": self.resolved_binary("pdfinfo_binary", "pdfinfo"),
            "ocrmypdf": self.resolved_binary("ocrmypdf_binary", "ocrmypdf"),
            "tesseract": self.resolved_binary("tesseract_binary", "tesseract"),
            "soffice": self.resolved_binary("soffice_binary", "soffice"),
            "java": self.resolved_binary("java_binary", "java"),
            "tika": self.resolved_binary("tika_binary", "tika"),
            "sandbox_mode": self.sandbox_mode,
            "sandbox_image": self.sandbox_image,
            "semantic_index_model": self.semantic_index_model,
            "semantic_index_dimensions": self.semantic_index_dimensions,
            "semantic_index_version": self.semantic_index_version,
        }
