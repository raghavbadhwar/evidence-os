import hashlib
import json
from uuid import uuid4

import pytest

from proofcounsel_ingest.evidence.providers import ProviderFailure, Providers
from tests.evidence.test_workspace import Workspace


def test_missing_to_reviewed_to_stale(tmp_path, monkeypatch):
    w = Workspace(tmp_path)
    config = tmp_path / "sources.json"
    source = tmp_path / "letter.txt"
    source.write_text("Under instrument C-70, ordinary notice is 45 days. Under C-7 Subsidiary A retains 30 days.")
    key = "Signed letter.txt"
    config.write_text(
        json.dumps({w.mid: {key: {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}}})
    )
    monkeypatch.setenv("EVIDENCE_LIBRARY_CONFIG", str(config))
    q = {"question": "Which notice period applies under C-7 to Subsidiary A?", "as_of": "2026-03-01"}

    def post(route, body):
        return w.client.post(w.base + "/" + route, json=body)

    def search(**args):
        return post("search", q | args).raise_for_status().json()

    assert search()["status"] == "unresolved"
    assert post("gaps", q).json() == post("gaps", q).json()
    w.client.put(
        w.base + "/members", json={"actor": str(w.users["reader"].actor), "permission": "editor"}
    ).raise_for_status()
    w.login("reader")
    assert w.client.get(w.base + "/library").json() == [key]
    assert post("fetch", {"source": "../other"}).status_code == 404
    job = post("fetch", {"source": key}).raise_for_status().json()
    assert search()["incomplete"]
    from proofcounsel_ingest.evidence.ingestion import work_one

    assert work_one(w.store)
    assert post("fetch", {"source": key}).json()["document_id"] == job["document_id"]
    assert search(question="C-71", mode="reference")["status"] == "unresolved"
    assert search(question=key, mode="filename")["status"] == "file_found"
    source.write_text("Changed in the archive.")
    assert post("fetch", {"source": key}).status_code == 409
    v = search(question="C-7", mode="reference")
    assert len(v["evidence"]) == 1
    eid = v["evidence"][0]["id"]
    learn = q | {"answer": "30 days for Subsidiary A under C-7.", "evidence_ids": [eid], "snapshot": v["snapshot"]}
    assert post("knowledge", learn | {"evidence_ids": [str(uuid4())]}).status_code == 404
    note = post("knowledge", learn).raise_for_status().json()
    assert post("knowledge", learn).json() == note
    assert search()["answer"] is None
    decision = {"decision": "approve", "reason": "Read the signed exception."}
    review = "knowledge/" + note["id"] + "/review"
    assert post(review, decision).status_code == 403
    w.login("owner")
    note2 = post("knowledge", learn | {"answer": "Another possible reading."}).json()["id"]
    assert post("knowledge/" + note2 + "/review", decision).status_code == 403
    assert post(review, decision).json()["status"] == "approved"
    assert search()["answer"] == learn["answer"]
    assert search(as_of="2027-03-01")["answer"] is None
    assert {r["status"] for r in w.client.get(w.base + "/knowledge").json()["entries"]} == {
        "approved",
        "resolved",
        "pending",
    }
    w.put("new amendment.txt", "A later amendment changes the notice period.", "amendment")
    assert search()["answer"] is None
    history = w.client.get(w.base + "/knowledge").json()["entries"]
    assert all(not row["current"] and row["answer"] is None and not row["evidence_ids"] for row in history)
    assert post("knowledge", learn).status_code == 409
    w.login("outside")
    for route in ["library", "knowledge"]:
        assert w.client.get(w.base + "/" + route).status_code == 404
    for route, body in [
        ("search", q),
        ("gaps", q),
        ("fetch", {"source": key}),
        ("knowledge", learn),
        (review, decision),
    ]:
        assert post(route, body).status_code == 404


def test_exact_file_reference_and_local_mode(tmp_path, monkeypatch):
    w = Workspace(tmp_path)
    w.put("C-70.txt", "It concerns C-70 only.", "other")
    for mode in ["reference", "filename"]:
        r = w.client.post(w.base + "/search", json={"question": "C-7", "mode": mode}).raise_for_status().json()
        assert r["status"] == "unresolved"
    monkeypatch.setenv("EVIDENCE_LOCAL_ONLY", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unused")
    p = Providers()
    assert not p.available(False)
    with pytest.raises(ProviderFailure, match="disabled"):
        p.post("unused", "unused", {}, 1)


def test_required_library_change_before_fetch_invalidates_all_answers(tmp_path, monkeypatch):
    w = Workspace(tmp_path)
    w.put("Master.txt", "For the Buyer on the convenience route give 45 days of written notice.", "agreement")
    w.client.put(
        w.base + "/members", json={"actor": str(w.users["reader"].actor), "permission": "editor"}
    ).raise_for_status()
    span = w.spans()[0]
    w.promote([span["id"]]).raise_for_status()
    scenario = w.scenario(35, ["written service"])
    assert scenario["status"] == "supported"
    w.login("reader")
    q = {"question": "How much notice for Buyer?", "as_of": "2026-03-01"}

    def search():
        return w.client.post(w.base + "/search", json=q).raise_for_status().json()

    before = search()
    req = dict(q, answer="45 days.", snapshot=before["snapshot"], evidence_ids=[span["id"]])
    note = w.client.post(w.base + "/knowledge", json=req).raise_for_status().json()["id"]
    w.login("owner")
    review = {"decision": "approve", "reason": "Read the master."}
    w.client.post(w.base + "/knowledge/" + note + "/review", json=review).raise_for_status()
    assert search()["answer"] == "45 days."
    source = tmp_path / "letter.txt"
    source.write_text("Signed exception to Master: the Buyer needs only 30 days' written notice.")
    spec = {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    manifest = tmp_path / "approved.json"
    manifest.write_text(json.dumps({w.mid: {"Side letter.txt": spec}}))
    monkeypatch.setenv("EVIDENCE_LIBRARY_CONFIG", str(manifest))
    blocked = search()
    assert blocked["answer"] is None and blocked["incomplete"]
    assert blocked["required_file_gaps"] == {"Side letter.txt": "not fetched"}
    assert blocked["snapshot"] != before["snapshot"]
    assert w.client.get("/v1/assessments/" + scenario["assessment_id"]).status_code == 409
    assert w.client.get(w.base + "/notice-rules").json() == []
    assert w.scenario(35, ["written service"])["status"] == "unresolved"
    assert w.promote([span["id"]]).status_code == 409
    first = w.ask("interpret", notice_review={"party": "Buyer", "route": "convenience"}).raise_for_status().json()
    assert first["status"] == "unresolved" and first["metrics"]["reasoning_calls"] == 0
    assert any("Side letter.txt" in gap for gap in first["gaps"])
    w.login("reader")
    draft = (
        w.client.post(w.base + "/knowledge", json={**req, "snapshot": blocked["snapshot"]}).raise_for_status().json()
    )
    w.login("owner")
    assert w.client.post(w.base + "/knowledge/" + draft["id"] + "/review", json=review).status_code == 409
    w.put("Side letter.txt", "Unrelated draft.", "other")
    assert search()["required_file_gaps"] == {"Side letter.txt": "different version"}
    w.put("Side letter.txt", source.read_bytes(), "other")
    assert search()["required_file_gaps"] == {} and search()["answer"] is None
    ready = search()["snapshot"]
    manifest.write_text(json.dumps({w.mid: {"Side letter.txt": spec, "Research.txt": {**spec, "required": False}}}))
    assert search()["snapshot"] == ready and not search()["incomplete"]
    w.login("outside")
    assert w.client.post(w.base + "/search", json=q).status_code == 404


def test_required_file_changes_during_provider_call(tmp_path, monkeypatch):
    w = Workspace(tmp_path)
    w.put("Master.txt", "The buyer may terminate upon written notice.", "agreement")
    manifest = tmp_path / "register.json"
    manifest.write_text("{}")
    monkeypatch.setenv("EVIDENCE_LIBRARY_CONFIG", str(manifest))

    def add_required_letter():
        spec = {"path": str(tmp_path / "letter"), "sha256": "0" * 64}
        manifest.write_text(json.dumps({w.mid: {"Side letter.txt": spec}}))

    w.models.hook = add_required_letter
    response = w.ask("interpret")
    assert response.status_code == 409
    assert "scope changed" in response.json()["detail"]
    after = w.ask("interpret").raise_for_status().json()
    assert after["status"] == "unresolved"
    assert after["metrics"]["reasoning_calls"] == 0
