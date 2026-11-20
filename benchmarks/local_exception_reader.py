"""Run the production reader prompt on four fictional passages using an installed local Qwen model."""

import argparse
import json
import time
from uuid import UUID

import httpx

from benchmarks.first_time_notice import CHANGE, DRAFT, ORIGINAL, SIDE
from proofcounsel_ingest.evidence.local_models import LocalModels
from proofcounsel_ingest.evidence.providers import Providers


class LocalFixtureReader(Providers):
    """Adapt the structured-output transport only for this synthetic model probe."""

    def __init__(self, client):
        self.client = client

    def structured(self, name, schema, instructions, data, deadline, max_tokens=1400):
        response = self.client.post(
            "/api/chat",
            json={
                "model": "qwen3:1.7b",
                "format": schema,
                "stream": False,
                "think": False,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(data)},
                ],
                "options": {"temperature": 0, "seed": 7, "num_predict": min(700, max_tokens)},
            },
            timeout=max(0.1, deadline - time.monotonic()),
        ).raise_for_status()
        return response.json()["message"]["content"]


def run(port):
    packet = [
        {"id": str(UUID(int=500 + i)), "document_id": str(UUID(int=700 + i)), "text": source}
        for i, source in enumerate([ORIGINAL, CHANGE, SIDE, DRAFT])
    ]
    rows = []
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
        reader = LocalFixtureReader(client)
        for party, expected in [("Alder Services", 30), ("Birch Services", 45)]:
            start = time.monotonic()
            scope = {"party": party, "route": "convenience", "as_of": "2026-03-01"}
            result = reader.read_exceptions(scope, packet, start + 35)
            rows.append(
                {
                    "party": party,
                    "expected_days": expected,
                    "reading": result,
                    "all_citations_known": set(result["evidence_ids"]) <= {p["id"] for p in packet},
                    "cited_side_letter": packet[2]["id"] in result["evidence_ids"],
                    "elapsed_seconds": round(time.monotonic() - start, 2),
                }
            )
    return rows


def run_workflow():
    from benchmarks.codex_notice import run_review_cases

    return run_review_cases(LocalModels(), hosted=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=11434, help="Port of the local Ollama server")
    parser.add_argument(
        "--workflow", action="store_true", help="Run the entire review with the configured EVIDENCE_OLLAMA settings"
    )
    args = parser.parse_args()
    print(json.dumps(run_workflow() if args.workflow else run(args.port), indent=2))
