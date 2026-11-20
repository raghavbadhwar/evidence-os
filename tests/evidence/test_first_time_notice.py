import base64
import hashlib
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from benchmarks.first_time_notice import SIDE, ScriptedDecisions, ask, load, run
from proofcounsel_ingest.evidence import agent_cli
from proofcounsel_ingest.evidence.api import create_app
from proofcounsel_ingest.evidence.contracts import Principal
from proofcounsel_ingest.evidence.ingestion import work_one
from proofcounsel_ingest.evidence.store import Store


def test_first_time_notice_before_reviewer_interpretation(tmp_path):
    result = run(tmp_path)
    assert result["decisive_passages"] == {"found": 24, "total": 24}
    assert result["wrong_releases"] == 0
    assert result["period_abstentions"] == 1
    assert result["actual_service_abstained"]
    for case in result["cases"]:
        assert case["correct_period"], case
        assert case["controlling_citations"], case
        assert case["correct_scope"] and case["correct_relationships"], case
        assert not case["cache_hit"], case
        assert case["scripted_calls"] == 2, case
        assert case["no_clock"], case
        if case["needs_period_abstention"]:
            assert case["specific_execution_gap"], case


@pytest.fixture
def matter(tmp_path):
    store = Store(tmp_path, os.getenv("EVIDENCE_TEST_DATABASE_URL"))
    provider = ScriptedDecisions()
    owner = Principal(tenant=uuid4(), actor=uuid4(), reviewer=True)
    reader = Principal(tenant=owner.tenant, actor=uuid4())
    auth = {hashlib.sha256(k.encode()).hexdigest(): v for k, v in {"fixture": owner, "reader": reader}.items()}
    with TestClient(create_app(store, auth, provider), headers={"Authorization": "Bearer fixture"}) as client:
        base, _ = load(client, store, SIDE)
        yield client, base, reader, provider


def test_exact_passages_and_review_lifecycle(matter):
    client, base, reader, _ = matter
    a = ask(client, base, "Alder Services", "2026-03-01")
    assert a["status"] == "needs_review" and not a["statements"]
    ids = a["notice_review"]["evidence_ids"]
    citations = [client.get("/v1/evidence/" + i).raise_for_status().json() for i in ids]
    side = next(p for p in citations if p["filename"] == "Commercial arrangements.pdf")
    assert side["text"] == SIDE and side["locator"]["page"] == 1 and side["role"] == "other"
    assert (
        client.post(base + "/answers", json={"question": "quote", "mode": "quote", "evidence_id": side["id"]})
        .raise_for_status()
        .json()["statements"][0]["text"]
        == SIDE
    )

    path = "/v1/assessments/" + a["assessment_id"]
    decision = {"decision": "approve", "reason": "Read all controlling passages."}
    reader_auth = {"Authorization": "Bearer reader"}
    assert client.get(path, headers=reader_auth).status_code == 404
    assert client.put(base + "/members", json={"actor": str(reader.actor), "permission": "reader"}).status_code == 200
    # A member change invalidates prior assessments, even for the original reviewer.
    assert client.post(path + "/reviews", json=decision).status_code == 409
    a = ask(client, base, "Alder Services", "2026-03-01")
    path = "/v1/assessments/" + a["assessment_id"]
    assert client.get(path, headers=reader_auth).status_code == 200
    assert client.post(path + "/reviews", headers=reader_auth, json=decision).status_code == 403
    reviewed = client.post(path + "/reviews", json=decision).raise_for_status().json()["assessment"]
    assert reviewed["status"] == "reviewed" and "30 calendar days" in reviewed["statements"][0]["text"]
    assert reviewed["service"]["clock_started"] is None
    assert ask(client, base, "Alder Services", "2026-03-01")["cache_hit"]
    args = agent_cli.parser().parse_args(
        ["notice-review", base.rsplit("/", 1)[-1], "Alder Services", "convenience", "--as-of", "2026-03-01"]
    )
    with TestClient(client.app, base_url="http://testserver/v1/", headers={"Authorization": "Bearer fixture"}) as cli:
        repeated = agent_cli.run(cli, args)
    assert repeated["cache_hit"] and repeated["assessment_id"] == a["assessment_id"]

    # The existing rule endpoint allows a reviewer to bind a corrected scope to original evidence.
    rule = {
        "snapshot": a["snapshot"],
        "party": "Alder Services",
        "route": "convenience",
        "effective_from": "2026-03-01",
        "minimum_calendar_days": 30,
        "conditions": ["written notice"],
        "evidence_ids": ids,
        "review_note": "Checked both entity aliases, the variation and the signed exception.",
    }
    rule["reviewed_assessment_id"] = a["assessment_id"]
    omitted = [pid for pid in ids if pid != side["id"]]
    assert client.post(base + "/notice-rules", json={**rule, "evidence_ids": omitted}).status_code == 422
    assert client.post(base + "/notice-rules", json={**rule, "party": "Birch Services"}).status_code == 422
    assert client.post(base + "/notice-rules", json=rule).status_code == 201
    assert client.post(base + "/notice-rules", json=rule).status_code == 409
    assert client.get(path).status_code == 409
    assert not ask(client, base, "Alder Services", "2026-03-01")["cache_hit"]


def test_unverified_findings_cannot_be_released(matter, monkeypatch):
    client, base, _, provider = matter
    propose = provider.propose_notice

    def unknown_id(scope, packet, deadline):
        assert all(set(p) == {"id", "document_id", "text"} for p in packet)
        finding = propose(scope, packet, deadline)
        finding["relationships"][0]["execution_evidence_ids"] = [str(uuid4())]
        return finding

    monkeypatch.setattr(provider, "propose_notice", unknown_id)
    a = ask(client, base, "Alder Services", "2026-03-01")
    assert a["status"] == "unresolved" and a["gaps"] == ["notice finding references unknown evidence"]
    assert a["metrics"]["verification_calls"] == 0 and not a["statements"]

    monkeypatch.setattr(provider, "propose_notice", propose)
    monkeypatch.setattr(provider, "verify", lambda question, packet, claims, deadline: ["conflict"] * len(claims))
    a = ask(client, base, "Alder Services", "2026-03-01")
    assert a["notice_review"]["minimum_calendar_days"] is None and not a["statements"]
    assert (
        client.post(
            "/v1/assessments/" + a["assessment_id"] + "/reviews",
            json={"decision": "approve", "reason": "Release should fail."},
        ).status_code
        == 409
    )


def test_first_time_notice_packet_overflow_is_explicit(matter, monkeypatch):
    client, base, _, provider = matter

    def unexpected_dispatch(*args):
        pytest.fail("partial packet reached a provider")

    monkeypatch.setattr(provider, "propose_notice", unexpected_dispatch)
    # More than 32 KB of relevant canonical text exceeds the proposal budget.
    raw = "\n\n".join(
        f"Notice appendix item {i}. " + "Ordinary text in an unrelated provision. " * 20 for i in range(50)
    )
    did = (
        client.post(
            base + "/documents",
            json={
                "name": "Appendix.txt",
                "content_b64": base64.b64encode(raw.encode()).decode(),
            },
        )
        .raise_for_status()
        .json()["document_id"]
    )
    assert work_one(client.app.state.store)
    assert client.get("/v1/jobs/" + did).json()["status"] == "ready"
    a = ask(client, base, "Alder Services", "2026-03-01")
    assert a["status"] == "unresolved" and not a["statements"] and not a["proposals"]
    assert "exceeds the first-time review budget" in a["gaps"][0]
    assert a["metrics"]["reasoning_calls"] == 0


def test_unexplained_document_requires_specific_review(matter, monkeypatch):
    client, base, _, provider = matter
    propose = provider.propose_notice

    def omit_side(scope, packet, deadline):
        return propose(scope, [p for p in packet if p["text"] != SIDE], deadline)

    monkeypatch.setattr(provider, "propose_notice", omit_side)
    a = ask(client, base, "Alder Services", "2026-03-01")
    assert a["notice_review"]["minimum_calendar_days"] == 45
    side = next(p for p in a["evidence"] if p["text"] == SIDE)
    assert a["unexplained_documents"] == [side["document_id"]]
    path = "/v1/assessments/" + a["assessment_id"] + "/reviews"
    approve = {"decision": "approve", "reason": "Reviewed the proposed period."}
    assert client.post(path, json=approve).status_code == 409
    assert client.post(path, json={**approve, "document_notes": {str(uuid4()): "Wrong file"}}).status_code == 409
    assert client.post(path, json={**approve, "document_notes": {side["document_id"]: " "}}).status_code == 422
    # The correct action for this omitted signed exception is rejection.
    rejected = client.post(path, json={"decision": "reject", "reason": "Signed exception controls for Alder."})
    assert rejected.raise_for_status().json()["assessment"]["status"] == "rejected"

    # An unrelated attachment can be accounted for without fabricating a citation.
    monkeypatch.setattr(provider, "propose_notice", propose)
    raw = base64.b64encode(b"Reception hours: Monday to Friday.").decode()
    did = client.post(base + "/documents", json={"name": "Reception.txt", "role": "other", "content_b64": raw}).json()[
        "document_id"
    ]
    assert work_one(client.app.state.store)
    a = ask(client, base, "Alder Services", "2026-03-01")
    assert a["unexplained_documents"] == [did]
    notes = {did: "Office hours have no bearing on this termination route."}
    rid = "/v1/assessments/" + a["assessment_id"]
    assert client.post(rid + "/reviews", json={**approve, "document_notes": notes}).status_code == 200
    assert client.get(rid).json()["review"]["document_notes"] == notes


def test_blind_reader_blocks_omitted_side_letter_and_separates_baseline_cache(matter, monkeypatch):
    client, base, _, provider = matter
    read_full = provider.propose_notice

    def miss_side(scope, packet, deadline):
        return read_full(scope, [p for p in packet if p["text"] != SIDE], deadline)

    monkeypatch.setattr(provider, "propose_notice", miss_side)
    baseline = ask(client, base, "Alder Services", "2026-03-01")
    assert baseline["notice_review"]["minimum_calendar_days"] == 45
    # Simulate a baseline reviewer dismissing the omitted file.
    approve = {
        "decision": "approve",
        "reason": "Read the packet.",
        "document_notes": {baseline["unexplained_documents"][0]: "Assumed irrelevant."},
    }
    path = "/v1/assessments/" + baseline["assessment_id"]
    assert client.post(path + "/reviews", json=approve).status_code == 200
    monkeypatch.setenv("EVIDENCE_NOTICE_EXCEPTION_READER", "true")
    assert client.get(path).status_code == 409

    def independent(scope, packet, deadline):
        assert set(scope) == {"party", "route", "as_of"}
        assert all(set(p) == {"id", "document_id", "text"} for p in packet)
        assert any(p["text"] == SIDE for p in packet)
        finding = read_full(scope, packet, deadline)
        return {k: finding[k] for k in ("minimum_calendar_days", "evidence_ids", "conditions", "gaps")}

    monkeypatch.setattr(provider, "read_exceptions", independent, raising=False)
    result = ask(client, base, "Alder Services", "2026-03-01")
    assert result["status"] == "unresolved" and not result["proposals"] and not result["statements"]
    assert result["notice_review"]["minimum_calendar_days"] is None
    assert result["exception_check"]["first_days"] == 45
    assert result["exception_check"]["reader"]["minimum_calendar_days"] == 30
    assert result["exception_check"]["status"] == "disagreement"
    assert result["metrics"]["exception_reader_calls"] == 1
    assert client.post("/v1/assessments/" + result["assessment_id"] + "/reviews", json=approve).status_code == 409
    monkeypatch.setattr(provider, "propose_notice", read_full)
    agreed = ask(client, base, "Alder Services", "2026-03-01")
    assert agreed["exception_check"]["status"] == "agreed" and agreed["status"] == "needs_review"
    assert not agreed["statements"] and agreed["service"]["clock_started"] is None


@pytest.mark.parametrize("fault", ["timeout", "unknown_citation", "uncertain", "conditions", "missing_citation"])
def test_exception_reader_fails_closed(matter, monkeypatch, fault):
    client, base, _, provider = matter
    monkeypatch.setenv("EVIDENCE_NOTICE_EXCEPTION_READER", "true")
    propose = provider.propose_notice

    def second(scope, packet, deadline):
        if fault == "timeout":
            raise TimeoutError()
        finding = propose(scope, packet, deadline)
        result = {k: finding[k] for k in ("minimum_calendar_days", "evidence_ids", "conditions", "gaps")}
        if fault == "unknown_citation":
            result["evidence_ids"] = [str(uuid4())]
        elif fault == "uncertain":
            result["gaps"] = ["The signature is illegible."]
        elif fault == "conditions":
            result["conditions"] += ["deliver to registered office"]
        elif fault == "missing_citation":
            result["evidence_ids"] += [next(p["id"] for p in packet if "unsigned" in p["text"].lower())]
        return result

    monkeypatch.setattr(provider, "read_exceptions", second, raising=False)
    result = ask(client, base, "Alder Services", "2026-03-01")
    assert result["status"] == "unresolved" and not result["proposals"]
    assert result["notice_review"]["minimum_calendar_days"] is None and result["gaps"]
    assert result["exception_check"]["status"] in {"disagreement", "failed"}
    assert result["evidence"] and result["metrics"]["exception_reader_calls"] == 1


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Buyer may terminate for convenience on 30 days of written notice.", "skipped_simple"),
        ("Buyer may terminate on 45 days of notice unless the Buyer exception requires 30 days.", "agreed"),
        ("Buyer gives 45 days. Subsidiary A may give 30 days.", "agreed"),
    ],
)
def test_exception_routing_for_single_passage(tmp_path, monkeypatch, text, status):
    from tests.evidence.test_workspace import Workspace

    w = Workspace(tmp_path)
    w.put("Terms.txt", text, "agreement")
    monkeypatch.setenv("EVIDENCE_NOTICE_EXCEPTION_READER", "true")

    def read(scope, packet, deadline):
        return {"minimum_calendar_days": 30, "conditions": [], "gaps": [], "evidence_ids": [packet[0]["id"]]}

    monkeypatch.setattr(w.models, "propose_notice", lambda *a: {**read(*a), "relationships": []}, raising=False)
    monkeypatch.setattr(w.models, "read_exceptions", read, raising=False)
    result = w.ask("interpret", notice_review={"party": "Buyer", "route": "convenience"}).raise_for_status().json()
    assert result["exception_check"]["status"] == status
    assert result["metrics"]["exception_reader_calls"] == int(status == "agreed")
    assert result["status"] == "needs_review" and not result["statements"]


def test_scope_changed_before_independent_dispatch(matter, monkeypatch):
    client, base, reader, provider = matter
    monkeypatch.setenv("EVIDENCE_NOTICE_EXCEPTION_READER", "true")

    def verify(question, packet, claims, deadline):
        client.put(base + "/members", json={"actor": str(reader.actor), "permission": "reader"}).raise_for_status()
        return ["supported"] * len(claims)

    monkeypatch.setattr(provider, "verify", verify)
    monkeypatch.setattr(provider, "read_exceptions", lambda *args: pytest.fail("stale packet dispatched"))
    response = client.post(
        base + "/answers",
        json={
            "mode": "interpret",
            "question": "What notice applies?",
            "as_of": "2026-03-01",
            "notice_review": {"party": "Alder Services", "route": "convenience"},
        },
    )
    assert response.status_code == 409
    assert "scope changed before provider dispatch" in response.json()["detail"]
