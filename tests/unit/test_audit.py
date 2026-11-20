from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from proofcounsel_ingest.audit import HashChainedAuditLog

TENANT = UUID("00000000-0000-0000-0000-000000001005")


def test_audit_chain_verifies_and_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = HashChainedAuditLog(path, TENANT)
    log.append(
        action_type="ingestion_run",
        aggregate_type="run",
        aggregate_id=uuid4(),
        payload={"status": "ready", "secret": "never stored"},
    )
    log.append(
        action_type="retrieval",
        aggregate_type="document",
        aggregate_id=uuid4(),
        payload={"hits": 3},
    )
    log.verify()
    assert "never stored" not in path.read_text()

    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["action_type"] = "tampered"
    lines[0] = json.dumps(first, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="audit event hash failed"):
        log.verify()
