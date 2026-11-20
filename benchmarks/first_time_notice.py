"""Synthetic first-time notice benchmark. Run with --baseline against the old API."""

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from uuid import UUID

import pymupdf
from fastapi.testclient import TestClient

from benchmarks.evidence_qualification import labels, summarize
from proofcounsel_ingest.evidence.api import create_app
from proofcounsel_ingest.evidence.contracts import Principal
from proofcounsel_ingest.evidence.ingestion import work_one
from proofcounsel_ingest.evidence.store import Store


class ScriptedDecisions:
    """Read a controlled source grammar. No expected answers, source names or gold IDs."""

    version = ("scripted-notice-1",)

    def available(self, reason):
        return True

    def select(self, question, selection, mandatory, deadline):
        return [p["id"] for p in selection], "complete"

    def propose(self, question, packet, deadline):
        as_of, party, route = re.search(
            r"As of (\S+): What minimum notice applies to (.+) for (.+) termination", question
        ).groups()
        finding = self.propose_notice({"party": party, "route": route, "as_of": as_of}, packet, deadline)
        return [{"text": f"{finding['minimum_calendar_days']} calendar days", "evidence_ids": finding["evidence_ids"]}]

    def propose_notice(self, scope, packet, deadline):
        party, route, as_of = (scope[k] for k in ("party", "route", "as_of"))
        original = next(p for p in packet if "original period:" in p["text"])
        aliases = dict(re.findall(r"([A-Z]+-\d+) = ([\w ]+)", original["text"]))
        controls = [original["id"]]
        days = int(re.search(r"original period: (\d+)", original["text"])[1])
        relationships = []
        for p in sorted(packet, key=lambda p: ("retains" in p["text"], "proposes" in p["text"])):
            text = p["text"]
            change = re.search(r"(changes to|retains|proposes) (\d+) calendar days.*?(\d{4}-\d{2}-\d{2})", text)
            if not change:
                continue
            verb, period, start = change.groups()
            code = re.search(r"([A-Z]+-\d+) retains", text)
            parties = [aliases[code[1]]] if code else list(aliases.values())
            execution = (
                "draft"
                if "unsigned" in text.lower()
                else "executed"
                if "signed by both" in text.lower()
                else "uncertain"
            )
            kind = {"changes to": "change", "retains": "exception", "proposes": "draft"}[verb]
            if kind != "draft" and start <= as_of:
                controls.append(p["id"])
            if party in parties and start <= as_of and execution != "draft":
                days = int(period) if execution == "executed" else None
            relationships.append(
                {
                    "kind": kind,
                    "from_evidence_id": p["id"],
                    "to_evidence_id": original["id"],
                    "parties": parties,
                    "route": route,
                    "effective_from": start,
                    "effective_until": None,
                    "execution": execution,
                    "execution_evidence_ids": [p["id"]],
                }
            )
        return {
            "minimum_calendar_days": days,
            "evidence_ids": controls,
            "relationships": relationships,
            "conditions": ["written notice"],
            "gaps": [],
        }

    def read_exceptions(self, scope, packet, deadline):
        reading = ScriptedDecisions.propose_notice(self, scope, packet, deadline)
        return {k: reading[k] for k in ("minimum_calendar_days", "evidence_ids", "conditions", "gaps")}

    def verify(self, question, packet, statements, deadline):
        return ["supported"] * len(statements)


ORIGINAL = (
    "Instrument C-7. AL-17 = Alder Services; BR-88 = Birch Services. "
    "Section 8. For convenience termination, written notice original period: 30 calendar days."
)
CHANGE = (
    "Amendment to Instrument C-7, section 8. The interval changes to 45 calendar days from 2026-03-01. "
    "Signed by both counterparties."
)
SIDE = "Instrument C-7, §8. AL-17 retains 30 calendar days beginning 2026-03-01. Signed by both counterparties."
DRAFT = "Instrument C-7, §8. Working copy proposes 60 calendar days beginning 2026-03-01. Unsigned draft."


def pdf(text):
    with pymupdf.open() as doc:
        page = doc.new_page()
        assert page.insert_textbox((48, 48, 560, 112), text, fontsize=10) >= 0
        return doc.tobytes()


def load(client, store, side, *, allow_hosted=True):
    def post(path, data):
        return client.post(path, json=data).raise_for_status().json()

    mid = post("/v1/source-sets", {"name": "Synthetic contract file", "allow_hosted": allow_hosted})["source_set_id"]
    base = "/v1/source-sets/" + mid
    sources = [
        ("Framework.txt", "agreement", ORIGINAL),
        ("Variation.txt", "amendment", CHANGE),
        ("Commercial arrangements.pdf", "other", side),
        ("Working copy.txt", "other", DRAFT),
    ]
    for name, role, body in sources:
        raw = pdf(body) if name.endswith(".pdf") else body.encode()
        did = post(
            base + "/documents",
            {
                "name": name,
                "role": role,
                "content_b64": base64.b64encode(raw).decode(),
            },
        )["document_id"]
        assert work_one(store)
        job = client.get("/v1/jobs/" + did).json()
        assert job["status"] == "ready", job
    assert not client.get(base).json()["links"]
    assert not client.get(base + "/notice-rules").json()
    return base, {(name, text) for name, _, text in sources}


def ask(client, base, party, date, baseline=False):
    req = {
        "mode": "interpret",
        "question": f"What minimum notice applies to {party} for convenience termination?",
        "as_of": date,
    }
    if not baseline:
        req["notice_review"] = {"party": party, "route": "convenience"}
    return client.post(base + "/answers", json=req).raise_for_status().json()


class MissedSideLetter(ScriptedDecisions):
    """Inject one error into the first reading only. Both readers receive all originals."""

    def propose_notice(self, scope, packet, deadline):
        return super().propose_notice(scope, [p for p in packet if " retains " not in p["text"]], deadline)


def run(root, baseline=False, omit_side_letter=False):
    actor = Principal(tenant=UUID(int=1), actor=UUID(int=2), reviewer=True)
    auth = {hashlib.sha256(b"fixture").hexdigest(): actor}
    rows = []
    for case_index, side in enumerate(
        (SIDE, SIDE.replace("Signed by both counterparties.", "Signature page missing."))
    ):
        store = Store(Path(root) / str(case_index))
        with TestClient(
            create_app(store, auth, MissedSideLetter() if omit_side_letter else ScriptedDecisions()),
            headers={"Authorization": "Bearer fixture"},
        ) as client:
            base, gold = load(client, store, side)
            for party, date, days in (
                ("Alder Services", "2026-03-01", 30 if side == SIDE else None),
                ("Birch Services", "2026-03-01", 45),
                ("Birch Services", "2026-02-28", 30),
            ):
                for stage in ("first", "repeat"):
                    start = time.perf_counter()
                    a = ask(client, base, party, date, baseline)
                    elapsed = (time.perf_counter() - start) * 1000
                    finding = a.get("notice_review") or {}
                    by_id = {p["id"]: p for p in a["evidence"]}
                    cited = labels([by_id[i] for i in finding.get("evidence_ids", [])])
                    controlling = {pair for pair in gold if pair[1] in (ORIGINAL, CHANGE, side)}
                    if date < "2026-03-01":
                        controlling = {("Framework.txt", ORIGINAL)}
                    if finding:
                        proposed = finding["minimum_calendar_days"]
                    else:
                        proposals = a["proposals"]
                        period = proposals[0]["text"].split()[0] if proposals else None
                        proposed = int(period) if period and period.isdigit() else None
                        cited = labels([by_id[i] for s in proposals for i in s["evidence_ids"]])
                    relationships = {
                        (
                            r["kind"],
                            by_id[r["from_evidence_id"]]["text"],
                            by_id[r["to_evidence_id"]]["text"],
                            tuple(sorted(r["parties"])),
                            r["route"],
                            r["effective_from"],
                            r["effective_until"],
                            r["execution"],
                            frozenset(by_id[i]["text"] for i in r["execution_evidence_ids"]),
                            r["verdict"],
                        )
                        for r in finding.get("relationships", [])
                    }
                    expected_relationships = {
                        (
                            kind,
                            source,
                            ORIGINAL,
                            parties,
                            "convenience",
                            "2026-03-01",
                            None,
                            execution,
                            frozenset([source]),
                            "supported",
                        )
                        for kind, source, parties, execution in [
                            ("change", CHANGE, ("Alder Services", "Birch Services"), "executed"),
                            ("exception", side, ("Alder Services",), "executed" if side == SIDE else "uncertain"),
                            ("draft", DRAFT, ("Alder Services", "Birch Services"), "draft"),
                        ]
                    }
                    rows.append(
                        {
                            "route": "first_time_notice",
                            "pass": stage,
                            "party": party,
                            "as_of": date,
                            "execution": "signed" if side == SIDE else "uncertain",
                            "ms": round(elapsed, 3),
                            "cache_hit": a["cache_hit"],
                            "decisive_found": len(gold & labels(a["evidence"])),
                            "decisive_total": len(gold),
                            "correct_period": proposed == days,
                            "controlling_citations": controlling == cited,
                            "correct_relationships": relationships == expected_relationships,
                            "correct_scope": all(
                                finding.get(k) == v
                                for k, v in {
                                    "party": party,
                                    "route": "convenience",
                                    "as_of": date,
                                }.items()
                            ),
                            "period_abstention": proposed is None,
                            "needs_period_abstention": days is None,
                            "specific_execution_gap": any("execution" in g.lower() for g in a["gaps"]),
                            "no_clock": (a.get("service") or {}).get("clock_started") is None
                            and "service" in (a.get("service") or {}).get("gap", ""),
                            "wrong_release": bool(a["statements"]),
                            "proposed_period": proposed,
                            "wrong_numeric_proposal": proposed is not None and proposed != days,
                            "exception_check": a.get("exception_check", {}).get("status"),
                            "exception_reader_calls": a["metrics"].get("exception_reader_calls", 0),
                            "scripted_calls": a["metrics"].get("exception_reader_calls", 0)
                            + sum(
                                a["metrics"][k] for k in ("selection_calls", "reasoning_calls", "verification_calls")
                            ),
                        }
                    )
            actual = (
                client.post(
                    base + "/answers",
                    json={
                        "mode": "notice",
                        "question": "Has the clock started?",
                        "as_of": "2026-03-01",
                        "receipt_id": str(UUID(int=99)),
                    },
                )
                .raise_for_status()
                .json()
            )
    first = [r for r in rows if r["pass"] == "first"]
    return {
        "scope": "synthetic in-process HTTP with scripted models; timings exclude ingestion",
        "hosted_calls": 0,
        "first_questions": len(first),
        "decisive_passages": {
            "found": sum(r["decisive_found"] for r in first),
            "total": sum(r["decisive_total"] for r in first),
        },
        "wrong_numeric_proposals": sum(r["wrong_numeric_proposal"] for r in first),
        "numeric_proposals": sum(r["proposed_period"] is not None for r in first),
        "exception_reader_calls": sum(r["exception_reader_calls"] for r in first),
        "wrong_releases": sum(r["wrong_release"] for r in first),
        "period_abstentions": sum(r["period_abstention"] for r in first),
        "correct_periods_or_abstentions": sum(r["correct_period"] for r in first),
        "scripted_calls": sum(r["scripted_calls"] for r in rows),
        "actual_service_abstained": actual["status"] == "unresolved"
        and not actual["statements"]
        and actual["gaps"] == ["current reviewer-verified service receipt required"],
        "timings": summarize(rows),
        "cases": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--exception-reader", action="store_true", help="Enable the independent reading policy")
    parser.add_argument("--omit-side-letter", action="store_true", help="Inject a first-reader omission")
    args = parser.parse_args()
    if args.baseline and args.exception_reader:
        parser.error("the old API does not support the exception reader")
    os.environ["EVIDENCE_NOTICE_EXCEPTION_READER"] = "true" if args.exception_reader else "false"
    with tempfile.TemporaryDirectory() as root:
        print(json.dumps(run(root, args.baseline, args.omit_side_letter), indent=2))
