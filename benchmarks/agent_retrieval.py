"""Isolate search latency using a seeded, synthetic, already-indexed SQLite catalogue."""

import argparse
import hashlib
import json
import math
import os
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from proofcounsel_ingest.evidence import service
from proofcounsel_ingest.evidence.api import create_app
from proofcounsel_ingest.evidence.contracts import Matter, Principal
from proofcounsel_ingest.evidence.ingestion import terms
from proofcounsel_ingest.evidence.store import Store, digest, dump, uid


def run(n=1000, repeats=50, required_library=False):
    with tempfile.TemporaryDirectory() as root:
        store = Store(Path(root))
        principal = Principal(tenant=uuid4(), actor=uuid4())
        mid = service.create(store, principal, Matter(name="Synthetic search trial"))["source_set_id"]
        sources = {}
        with store.tx(True) as db:
            for i in range(n):
                did, eid = uid(), uid()
                text = f"Signed amendment AG-{10000 + i} concerns the renewal of this synthetic service contract."
                sha = digest(text.encode())
                if required_library:
                    name = f"Amendment {i}.txt"
                    sources[name] = {"path": str(Path(root) / name), "role": "amendment", "sha256": sha}
                db.execute(
                    "INSERT INTO ev_docs(id,matter,name,role,sha,status,active) VALUES(?,?,?,?,?,'ready',1)",
                    (did, mid, f"Amendment {i}.txt", "amendment", sha),
                )
                db.execute(
                    "INSERT INTO ev_blocks VALUES(?,?,?,?,?,?,?,?)",
                    (eid, did, 0, text, sha, dump({"line_start": 1, "line_end": 1}), "", 0),
                )
                db.many("INSERT INTO ev_terms VALUES(?,?)", ((t, eid) for t in terms(text)))
        app = create_app(store, {hashlib.sha256(b"fixture").hexdigest(): principal})
        results = {}
        config = Path(root) / "library.json"
        config.write_text(json.dumps({mid: sources}))
        with (
            patch.dict(os.environ, EVIDENCE_LIBRARY_CONFIG=str(config) if required_library else ""),
            TestClient(app, headers={"Authorization": "Bearer fixture"}) as client,
        ):
            for mode in ("filename", "reference", "question"):
                ms = []
                for j in range(repeats + 1):
                    i = (j * 37) % n
                    query = {
                        "filename": f"Amendment {i}.txt",
                        "reference": f"AG-{10000 + i}",
                        "question": f"renewal under AG-{10000 + i}",
                    }[mode]
                    t = time.perf_counter()
                    v = (
                        client.post(f"/v1/source-sets/{mid}/search", json={"mode": mode, "question": query})
                        .raise_for_status()
                        .json()
                    )
                    ms.append(1000 * (time.perf_counter() - t))
                    assert v["answer"] is None and not v["required_file_gaps"]
                    if mode == "filename":
                        assert len(v["documents"]) == 1 and v["documents"][0]["name"] == query
                    else:
                        assert v["evidence"][0]["filename"] == f"Amendment {i}.txt"
                results[mode] = {
                    "first_ms": round(ms[0], 2),
                    "warm_p50_ms": round(statistics.median(ms[1:]), 2),
                    "warm_p95_ms": round(sorted(ms[1:])[math.ceil(0.95 * repeats) - 1], 2),
                    "correct_first_hits": repeats + 1,
                }
            assert (
                client.post(f"/v1/source-sets/{mid}/search", json={"mode": "reference", "question": "AG-99999"}).json()[
                    "status"
                ]
                == "unresolved"
            )
        return {
            "documents": n,
            "required_files": len(sources),
            "passages_per_document": 1,
            "warm_queries_per_mode": repeats,
            "transport": "in-process HTTP TestClient",
            "results": results,
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--documents", type=int, default=1000)
    p.add_argument("--repeats", type=int, default=50)
    p.add_argument(
        "--required-library",
        action="store_true",
        help="Reconcile every indexed file against a synthetic records register",
    )
    a = p.parse_args()
    if min(a.documents, a.repeats) < 1:
        p.error("counts must be positive")
    print(json.dumps(run(a.documents, a.repeats, a.required_library), indent=2))
