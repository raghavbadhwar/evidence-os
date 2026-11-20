"""Budgeted optional repair port for unresolved structure candidates."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from proofcounsel_ingest.extractors.base import ExtractionResult
from proofcounsel_ingest.structure.candidates import StructureCandidate


class StructureRepairBudget(BaseModel):
    max_calls: int = Field(default=1, ge=0, le=3)
    max_blocks: int = Field(default=200, gt=0, le=2000)
    max_candidates: int = Field(default=100, gt=0, le=1000)
    max_output_tokens: int = Field(default=2000, gt=0, le=10_000)


class StructureRepairPort(Protocol):
    def repair(
        self,
        extraction: ExtractionResult,
        existing: list[StructureCandidate],
        budget: StructureRepairBudget,
    ) -> list[StructureCandidate]: ...


class NoOpStructureRepair:
    def repair(
        self,
        extraction: ExtractionResult,
        existing: list[StructureCandidate],
        budget: StructureRepairBudget,
    ) -> list[StructureCandidate]:
        del extraction, existing, budget
        return []
