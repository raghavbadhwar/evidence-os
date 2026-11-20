"""Exact source-version and source-locator verification for material answer claims."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, Field

from proofcounsel_ingest.models import (
    CitationVerificationStatus,
    ContentBlock,
    SourceLocator,
    VerifiedCitation,
)
from proofcounsel_ingest.telemetry import NullTelemetrySink, TelemetrySink, timed_stage


class CitationRequest(BaseModel):
    tenant_id: UUID
    document_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    block_id: UUID
    answer_span: str = Field(min_length=1)
    quoted_text: str | None = None
    locator: SourceLocator | None = None


class CitationCheck(BaseModel):
    request: CitationRequest
    status: CitationVerificationStatus
    citation: VerifiedCitation | None = None
    reason: str | None = None


class CitationVerificationResult(BaseModel):
    checks: list[CitationCheck]
    all_verified: bool
    must_abstain: bool


def verify_citations(
    requests: Sequence[CitationRequest],
    blocks: Sequence[ContentBlock],
    *,
    expected_tenant_id: UUID,
    expected_document_id: UUID,
    expected_source_sha256: str,
    enforce: bool = True,
    telemetry_sink: TelemetrySink | None = None,
) -> CitationVerificationResult:
    sink = telemetry_sink or NullTelemetrySink()
    with timed_stage(
        sink,
        tenant_id=expected_tenant_id,
        document_id=expected_document_id,
        source_sha256=expected_source_sha256,
        stage="citation_verification",
        metadata={"request_count": len(requests)},
    ):
        return _verify_citations(
            requests,
            blocks,
            expected_tenant_id=expected_tenant_id,
            expected_document_id=expected_document_id,
            expected_source_sha256=expected_source_sha256,
            enforce=enforce,
        )


def _verify_citations(
    requests: Sequence[CitationRequest],
    blocks: Sequence[ContentBlock],
    *,
    expected_tenant_id: UUID,
    expected_document_id: UUID,
    expected_source_sha256: str,
    enforce: bool,
) -> CitationVerificationResult:
    block_by_id = {block.block_id: block for block in blocks}
    checks: list[CitationCheck] = []
    for request in requests:
        scope_error = _scope_error(
            request,
            expected_tenant_id=expected_tenant_id,
            expected_document_id=expected_document_id,
            expected_source_sha256=expected_source_sha256,
        )
        if scope_error:
            checks.append(
                CitationCheck(
                    request=request,
                    status=CitationVerificationStatus.REJECTED,
                    reason=scope_error,
                )
            )
            continue
        block = block_by_id.get(request.block_id)
        if block is None:
            checks.append(
                CitationCheck(
                    request=request,
                    status=CitationVerificationStatus.REJECTED,
                    reason="citation block does not exist in the selected document version",
                )
            )
            continue
        if block.document_id != expected_document_id:
            checks.append(
                CitationCheck(
                    request=request,
                    status=CitationVerificationStatus.REJECTED,
                    reason="citation block belongs to another document",
                )
            )
            continue
        if request.locator is None:
            checks.append(
                CitationCheck(
                    request=request,
                    status=CitationVerificationStatus.REJECTED,
                    reason="citation must include the exact source locator",
                )
            )
            continue
        if request.locator != block.locator:
            checks.append(
                CitationCheck(
                    request=request,
                    status=CitationVerificationStatus.REJECTED,
                    reason="citation locator does not match the indexed source block",
                )
            )
            continue
        if not block.metadata.get("queryable_evidence", True):
            checks.append(
                CitationCheck(
                    request=request,
                    status=CitationVerificationStatus.REJECTED,
                    reason="citation block is not approved as queryable evidence",
                )
            )
            continue
        actual_text_hash = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
        if actual_text_hash != block.text_sha256:
            checks.append(
                CitationCheck(
                    request=request,
                    status=CitationVerificationStatus.REJECTED,
                    reason="indexed evidence text failed its integrity check",
                )
            )
            continue

        quote = request.quoted_text or request.answer_span
        status, reason = _support_status(request.answer_span, quote, block.text)
        citation = VerifiedCitation(
            tenant_id=expected_tenant_id,
            document_id=expected_document_id,
            source_sha256=expected_source_sha256,
            answer_span=request.answer_span,
            block_id=block.block_id,
            quoted_text_sha256=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            locator=block.locator,
            verification_status=status,
            failure_reason=reason,
        )
        checks.append(CitationCheck(request=request, status=status, citation=citation, reason=reason))
    all_verified = bool(checks) and all(check.status == CitationVerificationStatus.VERIFIED for check in checks)
    return CitationVerificationResult(
        checks=checks,
        all_verified=all_verified,
        must_abstain=enforce and not all_verified,
    )


def _scope_error(
    request: CitationRequest,
    *,
    expected_tenant_id: UUID,
    expected_document_id: UUID,
    expected_source_sha256: str,
) -> str | None:
    if request.tenant_id != expected_tenant_id:
        return "citation tenant mismatch"
    if request.document_id != expected_document_id:
        return "citation document mismatch"
    if request.source_sha256 != expected_source_sha256:
        return "citation source version mismatch"
    return None


def _support_status(
    answer_span: str,
    quote: str,
    evidence: str,
) -> tuple[CitationVerificationStatus, str | None]:
    normalized_answer = _normalize(answer_span)
    normalized_quote = _normalize(quote)
    normalized_evidence = _normalize(evidence)
    quote_verified = bool(normalized_quote and normalized_quote in normalized_evidence)
    if answer_span.strip() == quote.strip() == evidence.strip() and normalized_quote:
        return CitationVerificationStatus.VERIFIED, None

    answer_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", answer_span))
    evidence_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", evidence))
    if answer_numbers - evidence_numbers:
        return CitationVerificationStatus.REJECTED, "claim contains numbers or dates absent from cited evidence"
    answer_terms = _terms(answer_span)
    evidence_terms = _terms(evidence)
    overlap = len(answer_terms & evidence_terms) / max(len(answer_terms), 1)
    if quote_verified and overlap >= 0.80:
        return CitationVerificationStatus.NEEDS_REVIEW, "paraphrase overlap is high but exact claim support is not proven"
    if not quote_verified:
        return CitationVerificationStatus.REJECTED, "quoted text does not occur in the cited evidence block"
    return CitationVerificationStatus.REJECTED, "cited evidence does not contain the claimed support"


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _terms(text: str) -> set[str]:
    return {term.casefold() for term in re.findall(r"[\w]{2,}", text, flags=re.UNICODE)}
