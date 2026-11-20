import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from benchmarks.first_time_notice import SIDE, ScriptedDecisions, ask, load
from proofcounsel_ingest.evidence.api import create_app
from proofcounsel_ingest.evidence.contracts import Principal
from proofcounsel_ingest.evidence.local_models import LocalModels
from proofcounsel_ingest.evidence.store import Store


@pytest.mark.parametrize("fault", ["none", "cloud", "changed", "partial"])
def test_firm_local_review_uses_pinned_model_without_hosted_consent(tmp_path, monkeypatch, fault):
    stages = []
    model = "test-contract:1"
    digest = "a" * 64

    class Model(BaseHTTPRequestHandler):
        def send_json(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/api/status":
                self.send_json({"cloud": {"disabled": fault != "cloud"}})
            elif self.path == "/api/tags":
                self.send_json(
                    {
                        "models": [
                            {
                                "name": model,
                                "digest": "b" * 64 if fault == "changed" else digest,
                                "details": {"format": "gguf"},
                            }
                        ]
                    }
                )
            else:
                self.send_error(404)

        def do_POST(self):
            assert self.path == "/api/chat"
            assert self.headers.get("Authorization") is None
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert data["model"] == model and data["truncate"] is False and data["shift"] is False
            packet = json.loads(data["messages"][-1]["content"])
            if "questions" in packet:
                result = {k: "supported" for k in packet["questions"]}
                stages.append("verify")
            else:
                assert any(p["text"] == SIDE for p in packet["passages"])
                assert set(data["format"]["properties"]["evidence_ids"]["items"]["enum"]) == {
                    p["id"] for p in packet["passages"]
                }
                result = ScriptedDecisions().propose_notice(packet["scope"], packet["passages"], 0)
                if "relationships" in data["format"]["properties"]:
                    stages.append("first")
                else:
                    stages.append("exception")
                    result.pop("relationships")
            self.send_json(
                {
                    "model": model,
                    "done": True,
                    "done_reason": "length" if fault == "partial" else "stop",
                    "message": {"role": "assistant", "content": json.dumps(result)},
                    "prompt_eval_count": 100,
                    "eval_count": 10,
                }
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for key, value in {
            "EVIDENCE_LOCAL_ONLY": "true",
            "EVIDENCE_OLLAMA_MODEL": model,
            "EVIDENCE_OLLAMA_DIGEST": digest,
            "EVIDENCE_OLLAMA_PORT": str(server.server_port),
            "EVIDENCE_NOTICE_EXCEPTION_READER": "true",
            "EVIDENCE_STUDY_TRACE": "true",
        }.items():
            monkeypatch.setenv(key, value)
        store = Store(tmp_path)
        principal = Principal(tenant=uuid4(), actor=uuid4(), reviewer=True)
        auth = {hashlib.sha256(b"fixture").hexdigest(): principal}
        with TestClient(create_app(store, auth, LocalModels()), headers={"Authorization": "Bearer fixture"}) as client:
            base, _ = load(client, store, SIDE, allow_hosted=False)
            result = ask(client, base, "Alder Services", "2026-03-01")
            assert not result["statements"] and result["service"]["clock_started"] is None
            if fault == "none":
                assert stages == ["first", "verify", "exception"]
                assert result["notice_review"]["minimum_calendar_days"] == 30 and result["status"] == "needs_review"
                assert result["exception_check"]["status"] == "agreed"
                assert len(result["metrics"]["provider_usage"]) == 3
                trace = next(
                    json.loads(e["target"])
                    for e in client.get(base + "/audit").json()
                    if e["event"] == "provider_dispatch_trace"
                )
                assert [c["stage"] for c in trace["calls"]] == ["notice_finding", "verify", "notice_exception_reading"]
                assert all(set(c["passage_ids"]) == {p["id"] for p in result["evidence"]} for c in trace["calls"])
            else:
                assert result["status"] == "unresolved" and not result["proposals"] and result["gaps"]
                assert stages == (["first"] if fault == "partial" else [])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_local_model_selection_requires_explicit_local_mode_and_digest(monkeypatch):
    monkeypatch.setenv("EVIDENCE_OLLAMA_MODEL", "firm:1")
    monkeypatch.delenv("EVIDENCE_OLLAMA_DIGEST", raising=False)
    monkeypatch.delenv("EVIDENCE_LOCAL_ONLY", raising=False)
    with pytest.raises(ValueError, match="LOCAL_ONLY"):
        LocalModels()
    monkeypatch.setenv("EVIDENCE_LOCAL_ONLY", "true")
    with pytest.raises(ValueError, match="digest"):
        LocalModels()
