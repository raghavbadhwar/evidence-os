"""Validate vendor wire shapes and reject missing, wrong-version or invented decisions."""

from uuid import uuid4

import pytest

from proofcounsel_ingest.evidence.providers import ProviderFailure, Providers


def test_jev_protected_context_and_pinned_revision(monkeypatch):
    provider = Providers()
    anchor = {"id": str(uuid4()), "filename": "Terms", "role": "agreement", "heading": "", "text": "Give notice."}
    dependent = {**anchor, "id": str(uuid4()), "role": "amendment", "filename": "Update", "text": "Except on renewal."}

    def dispatch(url, key, req, deadline):
        assert url.endswith("/v1/systemone") and req["model"] == provider.jev
        assert "Except on renewal" in req["state"]
        assert set(req["questions"]) == {anchor["id"], "coverage"}
        return {
            "model": provider.jev,
            "answers": {
                k: {"type": "choice", "choice": next(iter(v["criteria"]))} for k, v in req["questions"].items()
            },
        }

    monkeypatch.setattr(provider, "post", dispatch)
    assert provider.select("What notice?", [anchor], [dependent], 0) == ([anchor["id"]], "complete")
    monkeypatch.setattr(provider, "post", lambda *args: {"model": "unexpected", "answers": {}})
    with pytest.raises(ProviderFailure):
        provider.select("What notice?", [anchor], [], 0)


def test_sol_requires_completed_structured_object(monkeypatch):
    provider = Providers()
    passage = {"id": str(uuid4()), "text": "A complete passage."}

    def dispatch(url, key, req, deadline):
        import json

        assert url.endswith("/v1/responses") and req["store"] is False
        assert req["text"]["format"]["strict"] and req["max_output_tokens"] > 0
        answer = json.dumps({"statements": [{"text": passage["text"], "evidence_ids": [passage["id"]]}]})
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}],
        }

    monkeypatch.setattr(provider, "post", dispatch)
    assert provider.propose("What does it say?", [passage], 0)[0]["evidence_ids"] == [passage["id"]]
    monkeypatch.setattr(provider, "post", lambda *args: {"status": "incomplete"})
    with pytest.raises(ProviderFailure):
        provider.propose("What does it say?", [passage], 0)


def test_sol_notice_schema_and_source_only_packet(monkeypatch):
    import json

    provider = Providers()
    scope = {"party": "Alder Services", "route": "convenience", "as_of": "2026-03-01"}
    passage = {"id": str(uuid4()), "document_id": str(uuid4()), "text": "30 days of written notice."}
    finding = {
        "minimum_calendar_days": 30,
        "evidence_ids": [passage["id"]],
        "relationships": [],
        "conditions": ["written notice"],
        "gaps": [],
    }

    def dispatch(url, key, req, deadline):
        assert req["store"] is False
        assert json.loads(req["input"]) == {"scope": scope, "passages": [passage]}
        fmt = req["text"]["format"]
        assert fmt["name"] == "notice_finding" and fmt["strict"]
        schema = fmt["schema"]
        for obj in [schema, *schema["$defs"].values()]:
            assert set(obj["required"]) == set(obj["properties"]) and obj["additionalProperties"] is False
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(finding)}]}],
        }

    monkeypatch.setattr(provider, "post", dispatch)
    assert provider.propose_notice(scope, [passage], 0) == finding


def test_study_trace_reconciles_transport_passages_without_source_text(tmp_path, monkeypatch):
    import hashlib
    import io
    import json
    import urllib.error
    import urllib.request

    from fastapi.testclient import TestClient

    from proofcounsel_ingest.evidence.api import create_app
    from tests.evidence.test_workspace import Workspace

    w = Workspace(tmp_path)
    primary = w.put("Master.txt", "Buyer convenience termination requires 45 days of notice.", "agreement")
    letter = w.put("Private letter.txt", "Signed side letter preserves only 30 days for Buyer.", "other")

    def passage(did):
        return w.client.get(w.base + "/documents/" + did + "/passages").raise_for_status().json()["passages"][0]["id"]

    master_id, letter_id = passage(primary), passage(letter)
    monkeypatch.setenv("EVIDENCE_LOCAL_ONLY", "false")
    monkeypatch.setenv("EVIDENCE_STUDY_TRACE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test")
    provider = Providers()
    auth = {hashlib.sha256(k.encode()).hexdigest(): v for k, v in w.users.items()}
    w.client = TestClient(create_app(w.store, auth, provider))
    w.login("owner")
    wire_ids = []
    status = 200

    class Transport:
        def open(self, req, timeout):
            body = json.loads(req.data)
            state = json.loads(body["input"] if "input" in body else body["state"])
            ps = state["untrusted_passages"] if "untrusted_passages" in state else state["passages"]
            wire_ids.append([p["id"] for p in ps])
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "offline failure", {}, None)
            if "input" in body:
                finding = {
                    "minimum_calendar_days": 45,
                    "evidence_ids": [master_id],
                    "relationships": [],
                    "conditions": [],
                    "gaps": [],
                }
                result = {
                    "model": provider.sol,
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(finding)}]}],
                }
            else:
                result = {
                    "model": provider.jev,
                    "answers": {
                        k: {"type": "choice", "choice": next(iter(v["criteria"]))} for k, v in body["questions"].items()
                    },
                }
            raw = io.BytesIO(json.dumps(result).encode())
            raw.status = 200
            return raw

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Transport())

    def traces():
        return [
            json.loads(e["target"])
            for e in w.client.get(w.base + "/audit").raise_for_status().json()
            if e["event"] == "provider_dispatch_trace"
        ]

    scope = {"party": "Buyer", "route": "convenience"}
    result = w.ask("interpret", notice_review=scope).raise_for_status().json()
    (trace,) = traces()
    assert result["status"] == "needs_review" and result["unexplained_documents"] == [letter]
    assert "calls" not in result["metrics"]
    assert trace["assessment_id"] == result["assessment_id"] and trace["snapshot"] == result["snapshot"]
    assert [c["stage"] for c in trace["calls"]] == ["notice_finding", "verify"]
    assert [c["passage_ids"] for c in trace["calls"]] == wire_ids
    assert all(
        set(c["passage_ids"]) == {letter_id, master_id} and c["outcome"] == "response_read" and c["http_status"] == 200
        for c in trace["calls"]
    )
    assert all(s not in json.dumps(trace) for s in ["Private letter", "Signed side letter", "Buyer", "offline-test"])
    status = 429
    failed = w.ask("locate").raise_for_status().json()
    attempt = next(t for t in traces() if t["assessment_id"] == failed["assessment_id"])["calls"]
    assert failed["status"] == "unresolved" and len(attempt) == 1
    assert (
        attempt[0]["stage"] == "select" and attempt[0]["outcome"] == "http_error" and attempt[0]["http_status"] == 429
    )
    status = 200
    monkeypatch.delenv("EVIDENCE_STUDY_TRACE")
    w.ask("locate").raise_for_status()
    assert len(traces()) == 2
    w.login("outside")
    assert w.client.get(w.base + "/audit").status_code == 404


def test_exception_reader_wire_contains_only_scope_and_originals(monkeypatch):
    import json

    provider = Providers()
    scope = {"party": "Alder Services", "route": "convenience", "as_of": "2026-03-01"}
    passage = {"id": str(uuid4()), "document_id": str(uuid4()), "text": "Alder retains 30 days."}
    expected = {"minimum_calendar_days": 30, "evidence_ids": [passage["id"]], "conditions": [], "gaps": []}

    def post(url, key, payload, deadline):
        assert json.loads(payload["input"]) == {"scope": scope, "passages": [passage]}
        assert payload["text"]["format"]["name"] == "notice_exception_reading"
        assert payload["store"] is False and payload["text"]["format"]["strict"]
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(expected)}]}],
        }

    monkeypatch.setattr(provider, "post", post)
    assert provider.read_exceptions(scope, [passage], 0) == expected
