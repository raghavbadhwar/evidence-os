from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from benchmarks.graders import first_matching_rank, recall_at_k
from benchmarks.runner import (
    _expected_texts,
    _gold_locator_accuracy,
    _model_answer_metrics,
    _question_repeats,
)
from proofcounsel_ingest.answer import AnswerProviderResult
from proofcounsel_ingest.citations import CitationRequest
from proofcounsel_ingest.models import (
    BlockType,
    ContentBlock,
    DocumentFormat,
    RetrievalHit,
    SourceLocator,
)
from proofcounsel_ingest.prompt_boundary import GroundedAnswer, GroundedClaim
from proofcounsel_ingest.retrieval.context import ContextBlock, ContextBundle
from proofcounsel_ingest.telemetry import NullTelemetrySink


def test_retrieval_grader_matches_normalized_expected_text() -> None:
    document_id = uuid4()
    block_id = uuid4()
    text = "Invoices are due before the fifth day of each month."
    hit = RetrievalHit(
        tenant_id=UUID("00000000-0000-0000-0000-000000001004"),
        document_id=document_id,
        source_sha256="a" * 64,
        block_id=block_id,
        channel="lexical",
        raw_score=1,
        fused_score=1,
        rank=1,
        locator=SourceLocator(format=DocumentFormat.TEXT, line_start=1, line_end=1),
        text=text,
    )
    assert recall_at_k([hit], ["  invoices   are due before the fifth day of each month. "], 5) == 1
    assert first_matching_rank([hit], ["fifth day"]) == 1


def test_gold_question_uses_adjudicated_labels_locators_and_repeat_policy() -> None:
    document_id = uuid4()
    text = "The filing deadline is 15 September 2026."
    hit = RetrievalHit(
        tenant_id=UUID("00000000-0000-0000-0000-000000001004"),
        document_id=document_id,
        source_sha256="a" * 64,
        block_id=uuid4(),
        channel="lexical",
        raw_score=1,
        fused_score=1,
        rank=1,
        locator=SourceLocator(format=DocumentFormat.TEXT, line_start=7, line_end=7),
        text=text,
    )
    question = {
        "id": "deadline",
        "expected_labels": [
            {
                "answer_text": text,
                "locator": {"format": "text", "line_start": 7},
            }
        ],
        "evaluation_mode": "model",
        "repeats": 5,
    }

    assert _expected_texts(question) == [text]
    assert _question_repeats(question, 1) == 5
    assert _gold_locator_accuracy(question, [hit]) == 1.0
    question["expected_labels"][0]["locator"]["line_start"] = 8
    assert _gold_locator_accuracy(question, [hit]) == 0.0


def test_model_question_fails_closed_without_provider_and_scores_grounded_answer() -> None:
    tenant_id = UUID("00000000-0000-0000-0000-000000001004")
    document_id = uuid4()
    block_id = uuid4()
    text = "The filing deadline is 15 September 2026."
    locator = SourceLocator(format=DocumentFormat.TEXT, line_start=7, line_end=7)
    block = ContentBlock(
        block_id=block_id,
        document_id=document_id,
        ordinal=0,
        block_type=BlockType.PARAGRAPH,
        text=text,
        confidence=1,
        locator=locator,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    context = ContextBundle(
        blocks=[ContextBlock(block=block, retrieval_rank=1, source="hit")],
        estimated_tokens=10,
    )
    question = {"id": "deadline", "query": "When?", "evaluation_mode": "model"}

    with pytest.raises(ValueError, match="requires an answer provider"):
        _model_answer_metrics(
            question,
            [text],
            context=context,
            tenant_id=tenant_id,
            document_id=document_id,
            source_sha256="a" * 64,
            provider=None,
            telemetry_sink=NullTelemetrySink(),
        )

    class Provider:
        def generate(self, prompt):  # type: ignore[no-untyped-def]
            del prompt
            citation = CitationRequest(
                tenant_id=tenant_id,
                document_id=document_id,
                source_sha256="a" * 64,
                block_id=block_id,
                answer_span=text,
                quoted_text=text,
                locator=locator,
            )
            answer = GroundedAnswer(
                answer=text,
                claims=[GroundedClaim(text=text, citation=citation)],
            )
            return AnswerProviderResult(
                answer=answer,
                model="test-model",
                version="v1",
                input_tokens=20,
                output_tokens=10,
                provider_cost=0.01,
            )

    metrics = _model_answer_metrics(
        question,
        [text],
        context=context,
        tenant_id=tenant_id,
        document_id=document_id,
        source_sha256="a" * 64,
        provider=Provider(),
        telemetry_sink=NullTelemetrySink(),
    )

    assert metrics["model_called"]
    assert metrics["grounded_answer_success"] == 1.0
    assert metrics["answer_all_citations_verified"]
    assert metrics["answer_input_tokens"] == 20
    assert metrics["answer_provider_cost"] == 0.01
