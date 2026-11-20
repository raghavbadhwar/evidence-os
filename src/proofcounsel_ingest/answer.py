"""Provider-neutral grounded-answer boundary with citation enforcement."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from proofcounsel_ingest.prompt_boundary import (
    GroundedAnswer,
    PromptBundle,
    build_prompt_bundle,
    validate_grounded_answer,
)
from proofcounsel_ingest.retrieval.context import ContextBundle
from proofcounsel_ingest.telemetry import NullTelemetrySink, TelemetryEvent, TelemetrySink, timed_stage


class AnswerProviderResult(BaseModel):
    answer: GroundedAnswer
    model: str = Field(default="unknown", min_length=1, max_length=120)
    version: str = Field(default="unknown", min_length=1, max_length=120)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    provider_cost: float = Field(default=0, ge=0)


class AnswerProvider(Protocol):
    def generate(self, prompt: PromptBundle) -> AnswerProviderResult | GroundedAnswer | Mapping[str, Any]: ...


class GroundedAnswerResult(BaseModel):
    answer: GroundedAnswer
    verification: Any
    prompt_injection_signal_count: int = Field(ge=0)
    model: str
    version: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    provider_cost: float = Field(default=0, ge=0)


def answer_document(
    query: str,
    context: ContextBundle,
    *,
    tenant_id: UUID,
    document_id: UUID,
    source_sha256: str,
    provider: AnswerProvider,
    telemetry_sink: TelemetrySink | None = None,
    enforce_citations: bool = True,
) -> GroundedAnswerResult:
    sink = telemetry_sink or NullTelemetrySink()
    prompt = build_prompt_bundle(query, context, source_sha256, tenant_id=tenant_id)
    with timed_stage(
        sink,
        tenant_id=tenant_id,
        document_id=document_id,
        source_sha256=source_sha256,
        stage="answer",
        metadata={"evidence_blocks": len(context.blocks), "injection_signal_count": len(prompt.injection_signals)},
    ):
        generated = provider.generate(prompt)
        provider_result = _coerce_provider_result(generated)

    answer = provider_result.answer
    if answer.abstained:
        verification = _abstention_verification()
    else:
        verification = validate_grounded_answer(
            answer,
            [item.block for item in context.blocks],
            tenant_id=tenant_id,
            document_id=document_id,
            source_sha256=source_sha256,
            enforce=enforce_citations,
            telemetry_sink=sink,
        )
        if not verification.all_verified:
            answer = GroundedAnswer(
                answer="",
                claims=[],
                abstained=True,
                abstention_reason="citation verification failed; answer withheld",
            )
        else:
            answer = answer.model_copy(update={"answer": "\n".join(c.text for c in answer.claims)})
    sink.emit(
        TelemetryEvent(
            tenant_id=tenant_id,
            document_id=document_id,
            source_sha256=source_sha256,
            stage="answer_result",
            status="abstained" if answer.abstained else "grounded",
            elapsed_ms=0,
            input_tokens=provider_result.input_tokens,
            output_tokens=provider_result.output_tokens,
            provider_cost=provider_result.provider_cost,
            metadata={
                "model": provider_result.model,
                "version": provider_result.version,
                "injection_signal_count": len(prompt.injection_signals),
            },
        )
    )
    return GroundedAnswerResult(
        answer=answer,
        verification=verification,
        prompt_injection_signal_count=len(prompt.injection_signals),
        model=provider_result.model,
        version=provider_result.version,
        input_tokens=provider_result.input_tokens,
        output_tokens=provider_result.output_tokens,
        provider_cost=provider_result.provider_cost,
    )


def _coerce_provider_result(value: AnswerProviderResult | GroundedAnswer | Mapping[str, Any]) -> AnswerProviderResult:
    if isinstance(value, AnswerProviderResult):
        return value
    if isinstance(value, GroundedAnswer):
        return AnswerProviderResult(answer=value)
    return AnswerProviderResult.model_validate(value)


def _abstention_verification() -> Any:
    from proofcounsel_ingest.citations import CitationVerificationResult

    return CitationVerificationResult(checks=[], all_verified=True, must_abstain=False)
