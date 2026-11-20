"""Replay a corrected delivery record after restoring a disposable PostgreSQL catalogue and originals."""

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from proofcounsel_ingest.evidence.api import create_app
from proofcounsel_ingest.evidence.contracts import Principal
from proofcounsel_ingest.evidence.ingestion import work_one
from proofcounsel_ingest.evidence.store import Store


def run(root):
    # A private Unix socket and fresh cluster keep both databases independent of installed firm services.
    cluster = root / "pg"
    subprocess.run(
        ["initdb", "-D", cluster, "-A", "trust", "--locale=C", "-E", "UTF8"], check=True, stdout=subprocess.DEVNULL
    )
    subprocess.run(
        [
            "pg_ctl",
            "-D",
            cluster,
            "-l",
            root / "postgres.log",
            "-o",
            f"-k {root} -h '' -p 48729 -c unix_socket_permissions=0700",
            "-w",
            "start",
        ],
        check=True,
    )
    try:
        trial(root)
    finally:
        subprocess.run(["pg_ctl", "-D", cluster, "-w", "stop", "-m", "fast"], check=True)


def trial(root):
    for name in ("original", "recovered"):
        subprocess.run(["createdb", "-h", root, "-p", "48729", name], check=True)

    def dsn(name):
        return f"postgresql:///{name}?host={root}&port=48729"

    auth = {hashlib.sha256(b"fixture").hexdigest(): Principal(actor=uuid4(), tenant=uuid4(), reviewer=True)}
    headers = {"Authorization": "Bearer fixture"}
    store = Store(root / "sources", dsn("original"))
    originals = {}
    with TestClient(create_app(store, auth), headers=headers) as client:

        def post(route, body):
            return client.post(route, json=body).raise_for_status().json()

        mid = post("/v1/source-sets", {"name": "Fictional delivery correction"})["source_set_id"]
        base = "/v1/source-sets/" + mid
        spans = {}
        for name, role, text in [
            ("Master.txt", "agreement", "Buyer may terminate for convenience with 45 calendar days of written notice."),
            (
                "Courier.txt",
                "receipt",
                "Buyer's written convenience notice: dispatched March 1, delivered March 2, 2026. Termination proposed April 15, 2026.",
            ),
        ]:
            raw = text.encode()
            did = post(
                base + "/documents", {"name": name, "role": role, "content_b64": base64.b64encode(raw).decode()}
            )["document_id"]
            assert work_one(store)
            originals[did] = raw
            spans[role] = (
                client.get(base + "/documents/" + did + "/passages").raise_for_status().json()["passages"][0]["id"]
            )
        snap = client.get(base).raise_for_status().json()["snapshot"]
        scope = {"party": "Buyer", "route": "convenience", "snapshot": snap}
        post(
            base + "/notice-rules",
            scope
            | {
                "effective_from": "2026-01-01",
                "conditions": ["written service"],
                "minimum_calendar_days": 45,
                "evidence_ids": [spans["agreement"]],
                "review_note": "Read the signed term.",
            },
        )
        receipt = scope | {
            "termination_on": "2026-04-15",
            "verified_conditions": ["written service"],
            "evidence_ids": [spans["receipt"]],
            "review_note": "Checked the courier record.",
        }
        wrong = post(base + "/receipts", receipt | {"served_on": "2026-03-01"})["receipt_id"]
        check = {"mode": "notice", "as_of": "2026-03-03", "question": "Does the interval meet the reviewed minimum?"}
        old = post(base + "/answers", check | {"receipt_id": wrong})
        assert "45 calendar days meets" in old["statements"][0]["text"]
        post(base + "/receipts/" + wrong + "/withdraw", {"reason": "The signed delivery date was March 2."})
        right = post(base + "/receipts", receipt | {"served_on": "2026-03-02"})["receipt_id"]

    backup = root / "catalog.dump"
    subprocess.run(["pg_dump", "-Fc", "--file", backup, dsn("original")], check=True)
    shutil.copytree(root / "sources", root / "restored-sources")
    subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--dbname", dsn("recovered"), backup], check=True)
    with TestClient(create_app(Store(root / "restored-sources", dsn("recovered")), auth), headers=headers) as restored:
        for did, raw in originals.items():
            assert restored.get(base + "/documents/" + did + "/original").raise_for_status().content == raw
        assert restored.get("/v1/assessments/" + old["assessment_id"]).status_code == 409
        assert [r["id"] for r in restored.get(base + "/receipts").raise_for_status().json()] == [right]
        assert (
            restored.post(base + "/answers", json=check | {"receipt_id": wrong}).raise_for_status().json()["status"]
            == "unresolved"
        )
        result = restored.post(base + "/answers", json=check | {"receipt_id": right}).raise_for_status().json()
        assert "44 calendar days falls short" in result["statements"][0]["text"]
        assert set(spans.values()) == {p["id"] for p in result["evidence"]}
        assert any(
            e["event"] == "withdrawn_receipt" and wrong in e["target"]
            for e in restored.get(base + "/audit").raise_for_status().json()
        )
        assert restored.get(base + "/receipts", headers={"Authorization": "Bearer unknown"}).status_code == 401
    print(
        "Restore passed: both originals, withdrawal, corrected 44-day calculation, exact citations, audit and access checks."
    )


if __name__ == "__main__":
    with (
        tempfile.TemporaryDirectory(prefix="eos-recovery-", dir="/tmp") as tmp,
        patch.dict(
            os.environ,
            EVIDENCE_LOCAL_ONLY="true",
            EVIDENCE_LIBRARY_CONFIG="",
        ),
    ):
        run(Path(tmp))
