from datetime import date
from pathlib import PurePath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

Label = Annotated[str, Field(min_length=1, max_length=160, pattern=r".*\S.*")]
Text = Annotated[str, Field(min_length=1, max_length=8000, pattern=r"(?s).*\S.*")]
NoticeLabel = Annotated[Label, WithJsonSchema({"type": "string"})]
Role = Literal["agreement", "amendment", "definition", "schedule", "receipt", "other"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Principal(Contract):
    tenant: UUID
    actor: UUID
    reviewer: bool = False


class Matter(Contract):
    name: Label
    allow_hosted: bool = False


class Member(Contract):
    actor: UUID
    permission: Literal["reader", "editor", "revoked"]


class Document(Contract):
    expected_current_id: UUID | None = None
    name: Label
    role: Role = "other"
    content_b64: Annotated[str, Field(min_length=1, max_length=14_000_000)]

    @model_validator(mode="after")
    def safe_name(self):
        if (
            self.name in (".", "..")
            or PurePath(self.name).name != self.name
            or "\\" in self.name
            or any(ord(c) < 32 for c in self.name)
        ):
            raise ValueError("invalid document name")
        return self


class Link(Contract):
    parent_document: UUID
    dependency_document: UUID


class Scenario(Contract):
    party: Label
    route: Label
    proposed_days: Annotated[int, Field(strict=True, ge=0, le=36500)]
    assumed_conditions: list[Label] = Field(default_factory=list, max_length=20)


class NoticeScope(Contract):
    party: Label
    route: Label


class Ask(Contract):
    question: Text
    mode: Literal["interpret", "locate", "find", "quote", "notice"] = "interpret"
    as_of: date = Field(default_factory=date.today)
    evidence_id: UUID | None = None
    scenario: Scenario | None = None
    receipt_id: UUID | None = None
    notice_review: NoticeScope | None = None

    @model_validator(mode="after")
    def validate_route(self):
        if (self.mode == "quote") != (self.evidence_id is not None):
            raise ValueError("supply an evidence ID only for a quote")
        if int(self.scenario is not None) + int(self.receipt_id is not None) != int(self.mode == "notice"):
            raise ValueError("notice requires exactly one hypothetical scenario or reviewed receipt")
        if self.notice_review and self.mode != "interpret":
            raise ValueError("first-time notice scope requires interpret mode")
        return self


class Draft(Contract):
    statements: list[Text] = Field(min_length=1, max_length=8)
    as_of: date = Field(default_factory=date.today)


class Rule(Contract):
    reviewed_assessment_id: UUID | None = None
    snapshot: str = Field(pattern="^[a-f0-9]{64}$")
    party: Label
    route: Label
    effective_from: date
    effective_until: date | None = None
    minimum_calendar_days: int = Field(strict=True, ge=0, le=36500)
    conditions: list[Label] = Field(min_length=1, max_length=20)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=32)
    review_note: Text

    @model_validator(mode="after")
    def interval(self):
        if self.effective_until and self.effective_until <= self.effective_from:
            raise ValueError("effective_until must be after effective_from")
        return self


class Review(Contract):
    document_notes: dict[UUID, Label] = Field(default_factory=dict, max_length=64)
    decision: Literal["approve", "reject"]
    reason: Text


class Statement(Contract):
    text: Text
    evidence_ids: list[UUID] = Field(min_length=1, max_length=32)


class Proposal(Contract):
    statements: list[Statement] = Field(min_length=1, max_length=8)


class NoticeRelationship(Contract):
    kind: Literal["change", "exception", "draft"]
    from_evidence_id: UUID
    to_evidence_id: UUID
    parties: list[NoticeLabel] = Field(min_length=1, max_length=20)
    route: NoticeLabel
    effective_from: date | None
    effective_until: date | None
    execution: Literal["executed", "draft", "uncertain"]
    execution_evidence_ids: list[UUID] = Field(max_length=16)

    @model_validator(mode="after")
    def validate_relationship(self):
        if self.from_evidence_id == self.to_evidence_id:
            raise ValueError("relationship requires distinct passages")
        if self.effective_from and self.effective_until and self.effective_until <= self.effective_from:
            raise ValueError("invalid effective interval")
        if self.execution != "uncertain" and not self.execution_evidence_ids:
            raise ValueError("execution status requires evidence")
        if (self.kind == "draft") != (self.execution == "draft"):
            raise ValueError("draft kind and execution must agree")
        return self


class NoticeFinding(Contract):
    minimum_calendar_days: Annotated[int, Field(strict=True, ge=0, le=36500)] | None
    evidence_ids: list[UUID] = Field(max_length=32)
    relationships: list[NoticeRelationship] = Field(max_length=7)
    conditions: list[NoticeLabel] = Field(max_length=20)
    gaps: list[NoticeLabel] = Field(max_length=8)


class ExceptionReading(Contract):
    minimum_calendar_days: Annotated[int, Field(strict=True, ge=0, le=36500)] | None
    evidence_ids: list[UUID] = Field(max_length=32)
    conditions: list[NoticeLabel] = Field(max_length=20)
    gaps: list[NoticeLabel] = Field(max_length=8)


class Withdrawal(Contract):
    reason: Text


class Receipt(Contract):
    snapshot: str = Field(pattern="^[a-f0-9]{64}$")
    party: Label
    route: Label
    served_on: date
    termination_on: date
    verified_conditions: list[Label] = Field(min_length=1, max_length=20)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=32)
    review_note: Text

    @model_validator(mode="after")
    def dates(self):
        if self.termination_on < self.served_on:
            raise ValueError("termination precedes service")
        return self


class ActionProposal(Contract):
    assessment_id: UUID
    operation: Literal["prepare_notice_draft"]
    target: Label
    expected_external_version: Label
