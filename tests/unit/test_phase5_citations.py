from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

from proofcounsel_ingest.citations import CitationRequest, verify_citations
from proofcounsel_ingest.models import (
    BlockType,
    CitationVerificationStatus,
    ContentBlock,
    DocumentFormat,
    SourceLocator,
)

TENANT = UUID("00000000-0000-0000-0000-000000000502")
SOURCE_HASH = "7" * 64


def evidence() -> ContentBlock:
    text = "Payment is due within thirty calendar days after invoice receipt."
    return ContentBlock(
        document_id=uuid4(),
        ordinal=0,
        block_type=BlockType.PARAGRAPH,
        text=text,
        confidence=1,
        locator=SourceLocator(format=DocumentFormat.PDF, page=12, bbox=(10, 20, 500, 80)),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        metadata={"queryable_evidence": True},
    )


def request(block: ContentBlock, **updates: object) -> CitationRequest:
    payload: dict[str, object] = {
        "tenant_id": TENANT,
        "document_id": block.document_id,
        "source_sha256": SOURCE_HASH,
        "block_id": block.block_id,
        "answer_span": block.text,
        "quoted_text": block.text,
        "locator": block.locator,
    }
    payload.update(updates)
    return CitationRequest.model_validate(payload)


def verify(block: ContentBlock, citation: CitationRequest):  # type: ignore[no-untyped-def]
    return verify_citations(
        [citation],
        [block],
        expected_tenant_id=TENANT,
        expected_document_id=block.document_id,
        expected_source_sha256=SOURCE_HASH,
    )


def test_exact_text_version_and_locator_round_trip_is_verified() -> None:
    block = evidence()
    result = verify(block, request(block))
    assert result.all_verified
    assert not result.must_abstain
    assert result.checks[0].citation
    assert result.checks[0].citation.locator == block.locator


def test_missing_or_approximate_locator_is_rejected() -> None:
    block = evidence()
    missing = verify(block, request(block, locator=None))
    wrong = verify(
        block,
        request(
            block,
            locator=SourceLocator(format=DocumentFormat.PDF, page=13, bbox=(10, 20, 500, 80)),
        ),
    )
    assert missing.must_abstain and "exact source locator" in (missing.checks[0].reason or "")
    assert wrong.must_abstain and "does not match" in (wrong.checks[0].reason or "")


def test_stale_source_missing_block_and_tampered_text_are_rejected() -> None:
    block = evidence()
    stale = verify(block, request(block, source_sha256="8" * 64))
    missing = verify(block, request(block, block_id=uuid4()))
    tampered = block.model_copy(update={"text": block.text + " Altered."})
    corrupt = verify(tampered, request(tampered))
    assert stale.checks[0].status == CitationVerificationStatus.REJECTED
    assert missing.checks[0].status == CitationVerificationStatus.REJECTED
    assert corrupt.checks[0].status == CitationVerificationStatus.REJECTED
    assert "integrity" in (corrupt.checks[0].reason or "")


def test_unsupported_number_is_rejected_and_nonexact_paraphrase_needs_review() -> None:
    block = evidence()
    unsupported = verify(
        block,
        request(block, answer_span="Payment is due in 45 days.", quoted_text=block.text),
    )
    paraphrase = verify(
        block,
        request(
            block,
            answer_span="Payment is due within thirty calendar days after invoice receipt",
            quoted_text=block.text,
        ),
    )
    assert unsupported.checks[0].status == CitationVerificationStatus.REJECTED
    assert paraphrase.checks[0].status == CitationVerificationStatus.NEEDS_REVIEW
    assert paraphrase.must_abstain


def test_no_material_claims_fails_closed_when_enforcement_is_enabled() -> None:
    block = evidence()
    result = verify_citations(
        [],
        [block],
        expected_tenant_id=TENANT,
        expected_document_id=block.document_id,
        expected_source_sha256=SOURCE_HASH,
    )
    assert not result.all_verified
    assert result.must_abstain
