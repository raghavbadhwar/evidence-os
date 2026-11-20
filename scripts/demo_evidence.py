"""Create a synthetic matter and exercise the running Evidence OS HTTP API."""

import argparse
import base64
import json
import os
import statistics
import time
import urllib.request
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765")
    p.add_argument("--auth-file", type=Path, default=Path(os.getenv("EVIDENCE_AUTH_FILE", ".evidence/identities.json")))
    args = p.parse_args()
    token = os.getenv("EVIDENCE_DEMO_TOKEN") or next(iter(json.loads(args.auth_file.read_text())))
    if not args.url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise SystemExit("This demo is intended for a local listener.")

    def api(path, data=None):
        request = urllib.request.Request(
            args.url + path,
            json.dumps(data).encode() if data is not None else None,
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)

    mid = api("/v1/source-sets", {"name": "Notice period demonstration"})["source_set_id"]
    base = "/v1/source-sets/" + mid
    for name, role, body in [
        (
            "Agreement.txt",
            "agreement",
            "The buyer may terminate for convenience on thirty calendar days of written notice.",
        ),
        (
            "Amendment.txt",
            "amendment",
            "From 1 March 2026, the buyer must give forty-five calendar days of written notice for convenience termination.",
        ),
        ("Service.txt", "schedule", "Section 8 requires written service of notice at the registered office."),
    ]:
        job = api(
            base + "/documents", {"name": name, "role": role, "content_b64": base64.b64encode(body.encode()).decode()}
        )
        for _ in range(100):
            status = api("/v1/jobs/" + job["ingestion_job_id"])
            if status["status"] == "ready":
                break
            if status["status"] == "failed":
                raise RuntimeError(status["detail"])
            time.sleep(0.2)
        else:
            raise RuntimeError("worker did not finish within 20 seconds")
    passages = api(base + "/answers", {"mode": "find", "question": "written notice service"})["evidence"]
    manifest = api(base)
    # Fixture operation simulates a reviewer's decision on these three known synthetic texts only.
    api(
        base + "/notice-rules",
        {
            "snapshot": manifest["snapshot"],
            "party": "buyer",
            "route": "convenience",
            "effective_from": "2026-03-01",
            "minimum_calendar_days": 45,
            "conditions": ["written service at the registered office"],
            "evidence_ids": [p["id"] for p in passages],
            "review_note": "Synthetic demonstration. Checked agreement, amendment, and service schedule.",
        },
    )
    latencies = []
    for days in range(30, 51):
        req = {
            "question": "Does this proposed notice meet the reviewed period?",
            "mode": "notice",
            "as_of": "2026-06-01",
            "scenario": {
                "party": "buyer",
                "route": "convenience",
                "proposed_days": days,
                "assumed_conditions": ["written service at the registered office"],
            },
        }
        start = time.perf_counter()
        answer = api(base + "/answers", req)
        latencies.append(1000 * (time.perf_counter() - start))
        if answer["status"] != "supported" or answer["cache_hit"]:
            raise RuntimeError("expected a fresh supported result")
        if days in {30, 45}:
            print(answer["statements"][0]["text"])
    print(
        f"{len(passages)} cited passages. Local HTTP p50 across {len(latencies)} distinct scenarios: {statistics.median(latencies):.2f} ms."
    )
    print("Source set", mid)


if __name__ == "__main__":
    main()
