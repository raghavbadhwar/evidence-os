"""Use the matter workspace from a terminal or an agent with a scoped credential."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx


def parser():
    p = argparse.ArgumentParser(
        prog="evidence-agent", description="Query the firm's Evidence OS workspace. Results are JSON."
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Open a private practice workspace with fictional files in your browser")
    doctor = sub.add_parser("doctor", help="Inspect the local practice installation")
    doctor.add_argument("--fix", action="store_true", help="Repair the local practice software and check again")
    connect = sub.add_parser("connect", help="Install the Evidence OS skill in an agent workspace")
    connect.add_argument("agent", choices=["codex", "claude"])
    connect.add_argument("--workspace", type=Path, default=Path.cwd(), help="Existing project directory")
    sub.add_parser("matters", help="List the matters you can open")
    for cmd, helptext in {
        "query": "Find a reviewed answer, matching passage or exact file",
        "notice-review": "Request a first review of a contractual notice period for a party, route and date",
        "receipts": "List current reviewed delivery records",
        "check-service": "Check a reviewed delivery record against the notice rule",
        "sources": "List the files the records owner has made available",
        "fetch": "Bring one approved file into the matter and wait for indexing",
        "quote": "Read the exact source passage and locator in this matter",
        "open": "Save a verified copy of an indexed original",
        "gap": "Record an unanswered question",
        "learn": "Propose a sourced answer for independent review",
        "inbox": "List questions and answers awaiting or following review",
        "review": "Approve, reject or withdraw a saved answer",
    }.items():
        c = sub.add_parser(cmd, help=helptext)
        c.add_argument("matter", type=UUID)
        if cmd in {"query", "gap", "learn"}:
            c.add_argument("question")
            c.add_argument("--as-of", type=date.fromisoformat, default=date.today())
        if cmd == "notice-review":
            c.add_argument("party")
            c.add_argument("route")
            c.add_argument("--as-of", type=date.fromisoformat, required=True)
        if cmd == "check-service":
            c.add_argument("receipt", type=UUID)
            c.add_argument("--as-of", type=date.fromisoformat, default=date.today())
        if cmd == "query":
            c.add_argument("--mode", choices=["question", "filename", "reference"], default="question")
        if cmd == "fetch":
            c.add_argument("source")
            c.add_argument("--replace", type=UUID, help="Current document ID for an intentional replacement")
        if cmd == "quote":
            c.add_argument("passage", type=UUID)
        if cmd == "open":
            c.add_argument("document", type=UUID)
            c.add_argument("destination", type=Path, help="New file path")
        if cmd == "learn":
            c.add_argument("--snapshot", required=True, help="Snapshot returned by query")
            c.add_argument("--answer", required=True)
            c.add_argument("--cite", action="append", type=UUID, required=True)
        if cmd == "inbox":
            c.add_argument("--after", default="")
        if cmd == "review":
            c.add_argument("record", type=UUID)
            c.add_argument("decision", choices=["approve", "reject", "withdraw"])
            c.add_argument("--reason", required=True)
    return p


def run(client, a):
    def call(method, path, **kwargs):
        return client.request(method, path, **kwargs).raise_for_status()

    if a.command == "matters":
        return call("GET", "source-sets").json()
    base = f"source-sets/{a.matter}/"
    if a.command == "notice-review":
        return call(
            "POST",
            base + "answers",
            json={
                "mode": "interpret",
                "as_of": a.as_of.isoformat(),
                "question": f"What minimum notice applies to {a.party} for {a.route} termination?",
                "notice_review": {"party": a.party, "route": a.route},
            },
        ).json()
    if a.command in {"query", "gap", "learn"}:
        payload = {"question": a.question, "as_of": a.as_of.isoformat()}
        if a.command == "query":
            payload["mode"] = a.mode
        elif a.command == "learn":
            payload.update(answer=a.answer, evidence_ids=list(map(str, a.cite)), snapshot=a.snapshot)
        route = {"query": "search", "gap": "gaps", "learn": "knowledge"}[a.command]
        return call("POST", base + route, json=payload).json()
    if a.command == "receipts":
        return call("GET", base + "receipts").json()
    if a.command == "check-service":
        return call(
            "POST",
            base + "answers",
            json={
                "mode": "notice",
                "receipt_id": str(a.receipt),
                "as_of": a.as_of.isoformat(),
                "question": "Does the reviewed service interval meet the notice period?",
            },
        ).json()
    if a.command == "quote":
        result = call(
            "POST",
            base + "answers",
            json={"question": "Quote the original passage.", "mode": "quote", "evidence_id": str(a.passage)},
        ).json()
        return {"status": result["status"], "snapshot": result["snapshot"], "evidence": result["evidence"]}
    if a.command == "sources":
        return call("GET", base + "library").json()
    if a.command == "inbox":
        return call("GET", base + "knowledge", params={"after": a.after}).json()
    if a.command == "review":
        return call(
            "POST", base + f"knowledge/{a.record}/review", json={"reason": a.reason, "decision": a.decision}
        ).json()
    if a.command == "fetch":
        job = call(
            "POST",
            base + "fetch",
            json={"source": a.source, "expected_current_id": str(a.replace) if a.replace else None},
        ).json()
        deadline = time.monotonic() + 60
        while job["status"] in {"queued", "processing"} and time.monotonic() < deadline:
            job = call("GET", "jobs/" + str(job.get("ingestion_job_id") or job["id"])).json()
            if job["status"] in {"queued", "processing"}:
                time.sleep(0.25)
        return job
    docs = call("GET", base.rstrip("/")).json()["documents"]
    doc = next((d for d in docs if d["id"] == str(a.document)), None)
    if not doc:
        raise ValueError("This document is unavailable in the selected matter.")
    raw = call("GET", base + f"documents/{a.document}/original").content
    if hashlib.sha256(raw).hexdigest() != doc["sha"]:
        raise ValueError("The downloaded original failed its integrity check.")
    with open(os.open(a.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fp:
        fp.write(raw)
    return {"file": str(a.destination), "sha256": doc["sha"]}


def connect(agent, workspace):
    root = workspace.resolve(strict=True)
    if not root.is_dir() or root == Path.home().resolve():
        raise ValueError("Choose a project directory for the agent workspace.")
    directory = root
    for part in (".agents" if agent == "codex" else ".claude", "skills", "evidence-os"):
        directory = directory / part
        if directory.is_symlink():
            raise ValueError(f"Choose a workspace with a local skill directory: {directory}")
        directory.mkdir(exist_ok=True)
    skill = directory / "SKILL.md"
    data = (Path(__file__).parent / "skills" / "evidence-os" / "SKILL.md").read_bytes()
    with tempfile.NamedTemporaryFile(dir=directory) as temp:
        temp.write(data)
        temp.flush()
        try:
            os.link(temp.name, skill)
            status = "installed"
        except FileExistsError:
            if skill.is_symlink() or not skill.is_file() or skill.read_bytes() != data:
                raise ValueError(f"An existing skill differs. Compare it before replacing {skill}.") from None
            status = "already_installed"
    return {
        "status": status,
        "skill": str(skill),
        "client": agent,
        "invoke": "$evidence-os" if agent == "codex" else "/evidence-os",
    }


def main():
    args = parser().parse_args()
    if args.command == "doctor":
        root = Path(__file__).resolve().parents[3]
        launcher = root / ("Start Evidence OS.cmd" if os.name == "nt" else "Start Evidence OS.command")
        if not launcher.is_file() or not (root / "scripts/practice_setup.py").is_file():
            print("Open Doctor Evidence OS in the extracted Quickstart folder.", file=sys.stderr)
            return 1
        return subprocess.call([str(launcher), "doctor", *(["--fix"] if args.fix else [])])
    if args.command == "demo":
        from .demo import main as demo

        demo()
        return 0
    try:
        if args.command == "connect":
            print(json.dumps(connect(args.agent, args.workspace)))
            return 0
        url = os.getenv("EVIDENCE_API_URL", "http://127.0.0.1:8765")
        u = urlsplit(url)
        if (
            u.scheme not in {"http", "https"}
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.path not in {"", "/"}
        ):
            raise ValueError("Set EVIDENCE_API_URL to the workspace origin.")
        if u.scheme == "http" and u.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use HTTPS for a remote workspace.")
        token = os.getenv("EVIDENCE_AGENT_TOKEN")
        if not token:
            raise ValueError("Set EVIDENCE_AGENT_TOKEN to the credential issued to you.")
        with httpx.Client(
            base_url=url.rstrip("/") + "/v1/",
            headers={"Authorization": "Bearer " + token},
            timeout=45 if args.command == "notice-review" else 25,
            trust_env=False,
            verify=os.getenv("EVIDENCE_CA_FILE") or True,
        ) as client:
            result = run(client, args)
        print(json.dumps(result, ensure_ascii=False))
        return int(args.command == "fetch" and result["status"] != "ready")
    except httpx.HTTPStatusError as exc:
        print(
            json.dumps(
                {
                    "error": exc.response.reason_phrase,
                    "status": exc.response.status_code,
                    "detail": exc.response.json().get("detail")
                    if exc.response.headers.get("content-type", "").startswith("application/json")
                    else None,
                }
            ),
            file=sys.stderr,
        )
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
