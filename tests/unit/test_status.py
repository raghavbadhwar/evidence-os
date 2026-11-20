from __future__ import annotations

import pytest

from proofcounsel_ingest.models import IngestionStatus
from proofcounsel_ingest.status import InvalidStatusTransition, can_transition, is_terminal, require_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IngestionStatus.RECEIVED, IngestionStatus.VALIDATING),
        (IngestionStatus.VALIDATING, IngestionStatus.EXTRACTING),
        (IngestionStatus.EXTRACTING, IngestionStatus.NEEDS_OCR),
        (IngestionStatus.NEEDS_OCR, IngestionStatus.EXTRACTING),
        (IngestionStatus.EXTRACTING, IngestionStatus.STRUCTURING),
        (IngestionStatus.STRUCTURING, IngestionStatus.INDEXING),
        (IngestionStatus.INDEXING, IngestionStatus.READY),
    ],
)
def test_valid_transitions(current: IngestionStatus, target: IngestionStatus) -> None:
    assert can_transition(current, target)
    require_transition(current, target)


@pytest.mark.parametrize(
    "terminal",
    [
        IngestionStatus.READY,
        IngestionStatus.PARTIAL,
        IngestionStatus.FAILED,
        IngestionStatus.QUARANTINED,
        IngestionStatus.DELETED,
    ],
)
def test_terminal_states_have_no_outgoing_transitions(terminal: IngestionStatus) -> None:
    assert is_terminal(terminal)
    for target in IngestionStatus:
        assert not can_transition(terminal, target)


def test_invalid_transition_fails_closed() -> None:
    with pytest.raises(InvalidStatusTransition, match="ready -> extracting"):
        require_transition(IngestionStatus.READY, IngestionStatus.EXTRACTING)

