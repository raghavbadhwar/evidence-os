from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from proofcounsel_ingest.answer import AnswerProviderResult, answer_document
from proofcounsel_ingest.models import BlockType, ContentBlock, DocumentFormat, SourceLocator
from proofcounsel_ingest.prompt_boundary import GroundedAnswer, GroundedClaim
from proofcounsel_ingest.retrieval.context import ContextBlock, ContextBundle
from proofcounsel_ingest.telemetry import JsonlTelemetrySink

TENANT = UUID("00000000-0000-0000-0000-000000001005")
SOURCE_HASH = "a" * 64


def _context() -> ContextBundle:
    text = "The filing deadline is 15 September 2026."
    block = ContentBlock(
        block_id=UUID("00000000-0000-0000-0000-000000001007"),
        document_id=UUID("00000000-0000-0000-0000-000000001006"),
        ordinal=0,
        block_type=BlockType.PARAGRAPH,
        text=text,
        confidence=1,
        locator=SourceLocator(format=DocumentFormat.TEXT, line_start=1, line_end=1),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return ContextBundle(blocks=[ContextBlock(block=block, retrieval_rank=1, source="hit")], estimated_tokens=10)


class GoodProvider:
    def generate(self, prompt):  # type: ignore[no-untyped-def]
        block = _context().blocks[0].block
        claim = GroundedClaim(
            text=block.text,
            citation={
                "tenant_id": TENANT,
                "document_id": block.document_id,
                "source_sha256": SOURCE_HASH,
                "block_id": block.block_id,
                "answer_span": block.text,
                "quoted_text": block.text,
                "locator": block.locator,
            },
        )
        return AnswerProviderResult(
            answer=GroundedAnswer(answer=block.text, claims=[claim]),
            model="test-model",
            version="v1",
            input_tokens=12,
            output_tokens=8,
        )


class UnsupportedProvider:
    def generate(self, prompt):  # type: ignore[no-untyped-def]
        del prompt
        return {"answer": "unsupported claim"}


def test_answer_service_verifies_provider_output_and_emits_redacted_stages(tmp_path) -> None:  # type: ignore[no-untyped-def]
    context = _context()
    sink = JsonlTelemetrySink(tmp_path / "telemetry.jsonl")
    result = answer_document(
        "What is the filing deadline?",
        context,
        tenant_id=TENANT,
        document_id=context.blocks[0].block.document_id,
        source_sha256=SOURCE_HASH,
        provider=GoodProvider(),
        telemetry_sink=sink,
    )

    assert not result.answer.abstained
    assert result.verification.all_verified
    events = [line for line in (tmp_path / "telemetry.jsonl").read_text().splitlines() if line]
    assert any('"stage": "answer"' in line for line in events)
    assert any('"stage": "citation_verification"' in line for line in events)
    assert all("filing deadline" not in line.casefold() for line in events)


def test_answer_service_abstains_on_invalid_provider_output(tmp_path) -> None:  # type: ignore[no-untyped-def]
    context = _context()
    with pytest.raises(ValueError):
        answer_document(
            "What is the filing deadline?",
            context,
            tenant_id=TENANT,
            document_id=context.blocks[0].block.document_id,
            source_sha256=SOURCE_HASH,
            provider=UnsupportedProvider(),
        )
