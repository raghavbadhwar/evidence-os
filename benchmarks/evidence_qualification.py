"""Offline qualification against synthetic sources using the normal workspace API."""

import base64
import hashlib
import json
import math
import statistics
import tempfile
import time
from collections import defaultdict
from uuid import UUID

from fastapi.testclient import TestClient

from proofcounsel_ingest.evidence.api import create_app
from proofcounsel_ingest.evidence.contracts import Principal
from proofcounsel_ingest.evidence.ingestion import work_one
from proofcounsel_ingest.evidence.store import Store

CLAUSE = "The buyer may terminate for convenience on thirty days of written notice."
AMENDMENT = "Effective 1 March 2026, the interval in section eight becomes forty-five calendar days."
EXCEPTION = "Except during the renewal window, written service at the registered office is sufficient."
GOLD = {("Agreement.txt", CLAUSE), ("Amendment.txt", AMENDMENT), ("Schedule.txt", EXCEPTION)}


class OfflineDecision:
    version = ("scripted-keep-all-v1", "no-reasoner")

    def __init__(self):
        self.calls = 0

    def available(self, reason):
        return not reason

    def select(self, question, selection, mandatory, deadline):
        self.calls += 1
        return [p["id"] for p in selection], "complete"


def labels(passages):
    return {(p["filename"], p["text"]) for p in passages}


def grade(answer, expected, required):
    statements = answer["statements"]
    by_id = {p["id"]: p for p in answer["evidence"]}
    decisive = required <= labels(answer["evidence"])
    if expected == "matches":
        return {"retrieved_all_decisive": decisive, "unexpected_release": bool(statements)}
    if expected.startswith("gap="):
        return {
            "correct_abstention": answer["status"] == "unresolved"
            and not statements
            and any(expected[4:] in g for g in answer["gaps"]),
            "unexpected_release": bool(statements),
        }
    correct = (
        answer["status"] == "supported"
        and len(statements) == 1
        and expected in statements[0]["text"]
        and "minimum of 45 calendar days" in statements[0]["text"]
        and required <= labels([by_id[i] for i in statements[0]["evidence_ids"] if i in by_id])
    )
    return {"correct_supported": correct, "unexpected_release": bool(statements) and not correct}


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["route"], row["pass"])].append(row["ms"])
    return {
        f"{route}/{stage}": {
            "n": len(vals),
            "p50_ms": round(statistics.median(vals), 3),
            "p95_ms": round(sorted(vals)[math.ceil(0.95 * len(vals)) - 1], 3),
        }
        for (route, stage), vals in groups.items()
    }


def run():
    with tempfile.TemporaryDirectory() as root:
        store = Store(root)
        provider = OfflineDecision()
        user = Principal(tenant=UUID(int=1), actor=UUID(int=2), reviewer=True)
        auth = {hashlib.sha256(b"fixture").hexdigest(): user}
        with TestClient(create_app(store, auth, provider), headers={"Authorization": "Bearer fixture"}) as client:

            def api(path, data=None):
                r = client.get(path) if data is None else client.post(path, json=data)
                r.raise_for_status()
                return r.json()

            base = (
                "/v1/source-sets/"
                + api("/v1/source-sets", {"name": "Qualification", "allow_hosted": True})["source_set_id"]
            )
            spans = []
            for name, role, body in [
                ("Agreement.txt", "agreement", CLAUSE),
                ("Amendment.txt", "amendment", AMENDMENT),
                ("Schedule.txt", "schedule", EXCEPTION),
            ]:
                did = api(
                    base + "/documents",
                    {"name": name, "role": role, "content_b64": base64.b64encode(body.encode()).decode()},
                )["document_id"]
                if not work_one(store) or api("/v1/jobs/" + did)["status"] != "ready":
                    raise RuntimeError("fixture ingestion failed")
                spans.extend(api(base + "/documents/" + did + "/passages")["passages"])
            rule = {
                "snapshot": api(base)["snapshot"],
                "party": "buyer",
                "route": "convenience",
                "effective_from": "2026-03-01",
                "minimum_calendar_days": 45,
                "conditions": ["outside renewal window", "written service"],
                "evidence_ids": [p["id"] for p in spans],
                "review_note": "Synthetic reviewer fixture.",
            }
            api(base + "/notice-rules", rule)
            scenario = {
                "party": "buyer",
                "route": "convenience",
                "proposed_days": 30,
                "assumed_conditions": rule["conditions"],
            }
            notice = {
                "mode": "notice",
                "question": "Does this period suffice?",
                "as_of": "2026-03-01",
                "scenario": scenario,
            }
            query = {"question": "Can the buyer terminate for convenience on written notice?", "as_of": "2026-03-01"}
            cases = [
                ("lexical wording mismatch", {**query, "mode": "find"}, "matches"),
                ("protected amendment", {**query, "mode": "locate"}, "matches"),
                ("effective first day", notice, "30 calendar days falls short"),
                ("minimum met", {**notice, "scenario": {**scenario, "proposed_days": 45}}, "45 calendar days meets"),
                (
                    "unresolved exception",
                    {**notice, "scenario": {**scenario, "assumed_conditions": ["written service"]}},
                    "gap=unknown condition: outside renewal window",
                ),
                ("before effective date", {**notice, "as_of": "2026-02-28"}, "gap=no applicable reviewed rule"),
                (
                    "missing service receipt",
                    {k: v for k, v in (notice | {"receipt_id": str(UUID(int=55))}).items() if k != "scenario"},
                    "gap=current reviewer-verified service receipt required",
                ),
                ("overlapping rules", notice, "gap=overlapping reviewed rules"),
            ]
            rows = []
            for name, request, expected in cases:
                if name == "overlapping rules":
                    api(base + "/notice-rules", rule)
                for stage in ("first", "repeat"):
                    before = provider.calls
                    start = time.perf_counter()
                    a = api(base + "/answers", request)
                    ms = (time.perf_counter() - start) * 1000
                    rows.append(
                        {
                            "case": name,
                            "route": request["mode"],
                            "pass": stage,
                            "ms": round(ms, 3),
                            "cache_hit": a["cache_hit"],
                            "scripted_calls": provider.calls - before,
                            **grade(a, expected, GOLD),
                        }
                    )
            first = [r for r in rows if r["pass"] == "first"]
            scores = {
                k: {"count": sum(r[k] for r in first if k in r), "total": sum(k in r for r in first)}
                for k in ["retrieved_all_decisive", "correct_supported", "correct_abstention", "unexpected_release"]
            }
            return {
                "scope": "synthetic, indexed matters, in-process HTTP; first and repeated requests",
                "hosted_calls": 0,
                "hosted_spend_this_run": 0,
                "estimated_live_cost": None,
                "scores": scores,
                "timings": summarize(rows),
                "cases": rows,
            }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
