"""Replay notice questions using the signed-in Codex CLI and fictional files only."""

import base64
import hashlib
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from benchmarks.first_time_notice import SIDE, ask, load
from proofcounsel_ingest.evidence.api import create_app
from proofcounsel_ingest.evidence.codex_models import CodexSol
from proofcounsel_ingest.evidence.contracts import Principal
from proofcounsel_ingest.evidence.ingestion import work_one
from proofcounsel_ingest.evidence.store import Store


def run_review_cases(provider, *, hosted):
    """Send only synthetic originals and scope through the real first-review endpoint."""
    owner = Principal(tenant=uuid4(), actor=uuid4(), reviewer=True)
    auth = {hashlib.sha256(b"fixture").hexdigest(): owner}
    rows = []
    with tempfile.TemporaryDirectory() as root:
        store = Store(Path(root))
        with TestClient(create_app(store, auth, provider), headers={"Authorization": "Bearer fixture"}) as client:
            mid = (
                client.post("/v1/source-sets", json={"name": "Single clause", "allow_hosted": hosted})
                .raise_for_status()
                .json()["source_set_id"]
            )
            simple = "/v1/source-sets/" + mid
            original = b"Alder Services may terminate for convenience on 30 days of written notice."
            client.post(
                simple + "/documents",
                json={"name": "Terms.txt", "role": "agreement", "content_b64": base64.b64encode(original).decode()},
            ).raise_for_status()
            assert work_one(store)
            amended, _ = load(client, store, SIDE, allow_hosted=hosted)
            cases = [(simple, "Alder Services", 30), (amended, "Birch Services", 45)]
            if hosted:
                cases.insert(1, (amended, "Alder Services", 30))
            for base, party, expected in cases:
                result = ask(client, base, party, "2026-03-01")
                finding = result.get("notice_review") or {}
                cited = set(finding.get("evidence_ids", []))
                rows.append(
                    {
                        "party": party,
                        "packet": "single" if base == simple else "amended",
                        "reference_days": expected,
                        "cited_sources": sorted({p["filename"] for p in result["evidence"] if p["id"] in cited}),
                        "service_clock": result["service"]["clock_started"],
                        "exception_check": result.get("exception_check", {}).get("status"),
                        "proposed_days": (result.get("notice_review") or {}).get("minimum_calendar_days"),
                        "status": result["status"],
                        "gaps": result["gaps"],
                        "released": len(result["statements"]),
                        "model_version": provider.version,
                        "metrics": result["metrics"],
                    }
                )
    return rows


if __name__ == "__main__":
    print(json.dumps(run_review_cases(CodexSol(), hosted=True), indent=2))
