"""Finite-state transition rules for a single immutable ingestion run."""

from __future__ import annotations

from collections.abc import Mapping

from proofcounsel_ingest.models import IngestionStatus


class InvalidStatusTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: Mapping[IngestionStatus, frozenset[IngestionStatus]] = {
    IngestionStatus.RECEIVED: frozenset({IngestionStatus.VALIDATING, IngestionStatus.FAILED}),
    IngestionStatus.VALIDATING: frozenset(
        {IngestionStatus.QUARANTINED, IngestionStatus.EXTRACTING, IngestionStatus.FAILED}
    ),
    IngestionStatus.EXTRACTING: frozenset(
        {
            IngestionStatus.NEEDS_OCR,
            IngestionStatus.STRUCTURING,
            IngestionStatus.PARTIAL,
            IngestionStatus.FAILED,
        }
    ),
    IngestionStatus.NEEDS_OCR: frozenset({IngestionStatus.EXTRACTING, IngestionStatus.FAILED}),
    IngestionStatus.STRUCTURING: frozenset(
        {IngestionStatus.INDEXING, IngestionStatus.PARTIAL, IngestionStatus.FAILED}
    ),
    IngestionStatus.INDEXING: frozenset(
        {IngestionStatus.READY, IngestionStatus.PARTIAL, IngestionStatus.FAILED}
    ),
    IngestionStatus.QUARANTINED: frozenset(),
    IngestionStatus.READY: frozenset(),
    IngestionStatus.PARTIAL: frozenset(),
    IngestionStatus.FAILED: frozenset(),
    IngestionStatus.DELETED: frozenset(),
}

TERMINAL_STATUSES = frozenset(
    {
        IngestionStatus.QUARANTINED,
        IngestionStatus.READY,
        IngestionStatus.PARTIAL,
        IngestionStatus.FAILED,
        IngestionStatus.DELETED,
    }
)


def can_transition(current: IngestionStatus, target: IngestionStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def require_transition(current: IngestionStatus, target: IngestionStatus) -> None:
    if not can_transition(current, target):
        raise InvalidStatusTransition(f"invalid ingestion transition: {current.value} -> {target.value}")


def is_terminal(status: IngestionStatus) -> bool:
    return status in TERMINAL_STATUSES

