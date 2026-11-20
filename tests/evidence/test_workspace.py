"""HTTP regressions using canonical ingestion; optional live PostgreSQL and raw Flash."""

import base64
import hashlib
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from proofcounsel_ingest.evidence.api import create_app
from proofcounsel_ingest.evidence.contracts import Principal
from proofcounsel_ingest.evidence.ingestion import work_one
from proofcounsel_ingest.evidence.store import Store


class Decisions:
    version = ("test-decision", "test-reason")
    gap = "complete"
    verdict = "supported"
    bad_id = False
    hook = None

    def available(self, reason):
        return True

    def select(self, question, selection, mandatory, deadline):
        return [p["id"] for p in selection[:1]], self.gap

    def propose(self, question, packet, deadline):
        if self.hook:
            self.hook()
        return [
            {
                "text": "Notice is subject to the written service conditions.",
                "evidence_ids": [str(uuid4())] if self.bad_id else [p["id"] for p in packet],
            }
        ]

    def verify(self, question, packet, statements, deadline):
        return [self.verdict] * len(statements)


class Workspace:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path, os.getenv("EVIDENCE_TEST_DATABASE_URL"))
        self.models = Decisions()
        tenant = uuid4()
        self.users = {
            name: Principal(actor=uuid4(), tenant=tenant if name != "outside" else uuid4(), reviewer=name == "owner")
            for name in ["owner", "reader", "outside"]
        }
        auth = {hashlib.sha256(k.encode()).hexdigest(): v for k, v in self.users.items()}
        self.client = TestClient(create_app(self.store, auth, self.models))
        self.login("owner")
        self.mid = self.client.post("/v1/source-sets", json={"name": "matter", "allow_hosted": True}).json()[
            "source_set_id"
        ]
        self.base = "/v1/source-sets/" + self.mid

    def login(self, user):
        self.client.headers["Authorization"] = "Bearer " + user

    def put(self, name, text, role, run=True):
        raw = text.encode() if isinstance(text, str) else text
        r = self.client.post(
            self.base + "/documents", json={"name": name, "role": role, "content_b64": base64.b64encode(raw).decode()}
        )
        assert r.status_code == 202, r.text
        did = r.json()["document_id"]
        if run:
            while work_one(self.store):
                pass
            j = self.client.get("/v1/jobs/" + did).json()
            assert j["status"] == "ready", j
        return did

    def ask(self, mode, **kwargs):
        return self.client.post(self.base + "/answers", json={"question": "notice", "mode": mode, **kwargs})

    def spans(self):
        return self.ask("find").json()["evidence"]

    def promote(self, ids):
        s = self.client.get(self.base).json()["snapshot"]
        return self.client.post(
            self.base + "/notice-rules",
            json={
                "snapshot": s,
                "party": "Buyer",
                "route": "convenience",
                "effective_from": "2025-01-01",
                "minimum_calendar_days": 45,
                "conditions": ["written service"],
                "evidence_ids": ids,
                "review_note": "Reviewed agreement and controlling qualifications.",
            },
        )

    def scenario(self, days, conditions=None):
        return self.ask(
            "notice",
            as_of="2026-03-01",
            scenario={
                "party": "Buyer",
                "route": "convenience",
                "proposed_days": days,
                "assumed_conditions": ["written service"] if conditions is None else conditions,
            },
        ).json()


@pytest.fixture
def w(tmp_path):
    return Workspace(tmp_path)


def test_firm_desk_opens_without_the_practice_walkthrough(w):
    assert 'data-practice="false"' in w.client.get("/desk").text


def test_search_quote_cache_and_revocation(w):
    did = w.put(
        "Terms.txt", "Invoices are due in 30 days except when disputed. Written notice is required.", "agreement"
    )
    span = w.spans()[0]
    assert span["locator"]["line_start"] == 1 and span["document_id"] == did
    first = w.ask("quote", evidence_id=span["id"]).json()
    assert first["statements"][0]["text"] == span["text"] and "except" in span["text"]
    assert w.ask("quote", evidence_id=span["id"]).json()["cache_hit"]
    w.login("outside")
    for path in [w.base, "/v1/evidence/" + span["id"], "/v1/assessments/" + first["assessment_id"], "/v1/jobs/" + did]:
        assert w.client.get(path).status_code == 404
    w.login("owner")
    member = {"actor": str(w.users["reader"].actor), "permission": "reader"}
    assert w.client.put(w.base + "/members", json=member).status_code == 200
    w.login("reader")
    assert w.client.get("/v1/evidence/" + span["id"]).status_code == 200
    assert w.client.post(w.base + "/documents", json={"name": "a.txt", "content_b64": "YQ=="}).status_code == 403
    w.login("owner")
    w.client.put(w.base + "/members", json={**member, "permission": "revoked"})
    w.login("reader")
    assert w.client.get("/v1/evidence/" + span["id"]).status_code == 404


def test_reviewed_rule_reuse_conflicts_and_invalidation(w):
    w.put("Clause.txt", "Thirty calendar days of notice.", "agreement")
    w.put("Amendment.txt", "The notice period is now forty-five calendar days with written service.", "amendment")
    ids = [p["id"] for p in w.spans()]
    assert w.promote(ids[:1]).status_code == 422
    r = w.promote(ids).json()["rule_id"]
    short = w.scenario(30)
    assert "falls short" in short["statements"][0]["text"]
    enough = w.scenario(50)
    assert enough["rule_id"] == r and "meets" in enough["statements"][0]["text"]
    assert enough["metrics"]["reasoning_calls"] == 0
    assert w.scenario(30, [])["status"] == "unresolved"
    second = w.promote(ids).json()["rule_id"]
    assert "overlapping" in w.scenario(45)["gaps"][0]
    assert w.client.get("/v1/assessments/" + enough["assessment_id"]).status_code == 409
    w.client.delete(w.base + "/notice-rules/" + second)
    assert w.scenario(45)["status"] == "supported"
    w.put("Amendment.txt", "A new notice period of sixty days applies.", "amendment", False)
    assert w.scenario(45)["status"] == "unresolved"
    assert any(w.client.get("/v1/evidence/" + eid).status_code == 404 for eid in ids)
    work_one(w.store)
    assert "no applicable" in w.scenario(45)["gaps"][0]


def test_joint_verification_review_and_draft(w):
    parent = w.put("Clause.txt", "Buyer shall give notice.", "agreement")
    child = w.put("Schedule.txt", "Delivery is valid only when signed for at reception.", "schedule")
    assert (
        w.client.post(w.base + "/links", json={"parent_document": parent, "dependency_document": child}).status_code
        == 200
    )
    result = w.ask("interpret").json()
    assert result["status"] == "needs_review" and not result["statements"]
    assert {p["filename"] for p in result["evidence"]} == {"Clause.txt", "Schedule.txt"}
    rid = result["assessment_id"]
    approved = w.client.post(
        "/v1/assessments/" + rid + "/reviews", json={"decision": "approve", "reason": "Read the two original passages."}
    )
    assert approved.json()["assessment"]["status"] == "reviewed"
    assert w.ask("interpret").json()["cache_hit"]
    w.models.verdict = "conflict"
    d = w.client.post(w.base + "/draft-checks", json={"statements": ["Giving notice by phone is sufficient."]}).json()
    assert d["proposals"][0]["verdict"] == "conflict" and not d["statements"]
    assert (
        w.client.post(
            "/v1/assessments/" + d["assessment_id"] + "/reviews", json={"decision": "approve", "reason": "Publish"}
        ).status_code
        == 409
    )


def test_bounded_recovery_bad_ids_and_race(w):
    w.put("Clause.txt", "Notice in writing is required.", "agreement")
    w.models.gap = "condition"
    a = w.ask("interpret").json()
    assert a["metrics"]["selection_calls"] == 2 and a["metrics"]["reasoning_calls"] == 0 and a["gaps"]
    w.models.gap = "complete"
    w.models.bad_id = True
    a = w.ask("interpret").json()
    assert a["status"] == "unresolved" and a["metrics"]["verification_calls"] == 0
    w.models.bad_id = False
    w.models.hook = lambda: w.put("Amendment.txt", "Updated exceptions to notice.", "amendment", False)
    assert w.ask("interpret").status_code == 409


def test_reviewed_service_evidence(w):
    w.put("Agreement.txt", "Written notice is required forty-five calendar days in advance.", "agreement")
    w.put("Receipt.txt", "Signed written notice served March 1 with termination requested April 10.", "receipt")
    spans = w.spans()
    w.promote([p["id"] for p in spans if p["role"] == "agreement"])
    req = {
        "snapshot": w.client.get(w.base).json()["snapshot"],
        "party": "Buyer",
        "route": "convenience",
        "served_on": "2026-03-01",
        "termination_on": "2026-04-10",
        "verified_conditions": ["written service"],
        "evidence_ids": [p["id"] for p in spans if p["role"] == "receipt"],
        "review_note": "Compared signed receipt.",
    }
    rid = w.client.post(w.base + "/receipts", json=req).json()["receipt_id"]
    assert w.ask("notice", as_of="2026-02-01", receipt_id=rid).json()["status"] == "unresolved"
    a = w.ask("notice", as_of="2026-03-02", receipt_id=rid).json()
    assert a["status"] == "supported" and "40 calendar days falls short" in a["statements"][0]["text"]
    assert {p["role"] for p in a["evidence"]} == {"agreement", "receipt"}


def test_no_external_dispatch_without_permission(w):
    w.put("Clause.txt", "Notice means written service.", "agreement")
    w.models.available = lambda reason: False
    a = w.ask("interpret").json()
    assert a["status"] == "unresolved" and a["metrics"]["selection_calls"] == 0


def test_flash_pdf_maps_to_original_locator(w, tmp_path):
    fitz = pytest.importorskip("pymupdf")
    pytest.importorskip("pageindex.flash")
    pdf = fitz.open()
    titles = ["Contract scope", "Defined terms", "Termination rights", "Notices"]
    for i, title in enumerate(titles, 1):
        page = pdf.new_page()
        page.insert_text((72, 85), f"{i}. {title}", fontsize=20)
        for j in range(9):
            page.insert_text(
                (72, 125 + 24 * j),
                f"Paragraph {i}.{j + 1}. Written notice must be served on the named recipient before termination.",
            )
    pdf.set_toc([[1, f"{i}. {t}", i] for i, t in enumerate(titles, 1)])
    w.put("Agreement.pdf", pdf.tobytes(), "agreement")
    pdf.close()
    doc = w.client.get(w.base).json()["documents"][0]
    assert doc["structure"] == "pageindex-flash"
    spans = w.spans()
    assert all(s["locator"]["bbox"] and s["heading"] for s in spans)
    assert all(1 <= s["locator"]["page"] <= 4 for s in spans)
    assert "Notices" in w.ask("find", question="Notices").json()["evidence"][0]["heading"]


def test_passages_and_dormant_proposals(w):
    did = w.put("Agreement.txt", "Buyer gives written notice.\n\nService must identify the recipient.", "agreement")
    page = w.client.get(w.base + "/documents/" + did + "/passages?limit=1").json()
    assert len(page["passages"]) == 1 and page["next_after"] is not None
    next_page = w.client.get(w.base + "/documents/" + did + "/passages", params={"after": page["next_after"]}).json()
    assert "recipient" in next_page["passages"][0]["text"]
    w.promote([p["id"] for p in page["passages"] + next_page["passages"]])
    a = w.scenario(45)
    request = {
        "assessment_id": a["assessment_id"],
        "operation": "prepare_notice_draft",
        "target": "demo-case",
        "expected_external_version": "1",
    }
    first = w.client.post("/v1/actions/proposals", json=request)
    assert first.json()["status"] == "proposal_only"
    assert w.client.post("/v1/actions/proposals", json=request).json() == first.json()


def test_idempotent_upload_and_failed_source_blocks_answer(w):
    did = w.put("Terms.txt", "Thirty days notice before termination.", "agreement")
    assert w.put("Terms.txt", "Thirty days notice before termination.", "agreement") == did
    bad = w.put("Broken.pdf", b"not a PDF", "amendment", False)
    assert work_one(w.store)
    assert w.client.get("/v1/jobs/" + bad).json()["status"] == "failed"
    assert "Broken.pdf (failed)" in w.ask("interpret").json()["gaps"]


def test_fragment_cannot_license_additional_words():
    from pydantic import ValidationError

    from proofcounsel_ingest.citations import _support_status
    from proofcounsel_ingest.prompt_boundary import GroundedAnswer

    claim = "Invoices are due in 30 days"
    block = claim + " except when disputed."
    assert _support_status(claim, claim, block)[0].value != "verified"
    citation = {
        "tenant_id": uuid4(),
        "document_id": uuid4(),
        "source_sha256": "a" * 64,
        "block_id": uuid4(),
        "answer_span": claim,
        "quoted_text": claim,
        "locator": {"format": "text", "line_start": 1, "line_end": 1},
    }
    with pytest.raises(ValidationError, match="material text"):
        GroundedAnswer(answer=claim + " and no exceptions apply.", claims=[{"text": claim, "citation": citation}])


def test_jev_lookup_without_reasoning(w):
    w.put("Terms.txt", "Written notice must be sent to the registered office.", "agreement")
    w.models.available = lambda reason: not reason
    a = w.ask("locate").json()
    assert a["status"] == "matches" and a["metrics"]["selection_calls"] == 1
    assert a["metrics"]["reasoning_calls"] == 0 and a["metrics"]["verification_calls"] == 0


def test_original_download_and_stale_model_review(w):
    text = "Written notice must be sent to the registered office."
    did = w.put("Terms.txt", text, "agreement")
    original = w.client.get(w.base + "/documents/" + did + "/original")
    assert original.content == text.encode() and original.headers["cache-control"] == "no-store"
    rid = w.ask("interpret").json()["assessment_id"]
    w.models.version = ("another-decision", "test-reason")
    assert w.client.get("/v1/assessments/" + rid).status_code == 409
    assert (
        w.client.post(
            "/v1/assessments/" + rid + "/reviews", json={"decision": "approve", "reason": "Reviewed"}
        ).status_code
        == 409
    )


def test_upload_preconditions_keep_distinct_same_named_files(w):
    original = w.put("scan.txt", "First document.", "other")
    url = w.base + "/documents"
    req = {"name": "scan.txt", "role": "other", "content_b64": base64.b64encode(b"New document.").decode()}
    # Explicit null means create-only; absent retains the existing versioned API behavior.
    assert w.client.post(url, json={**req, "expected_current_id": None}).status_code == 409
    assert w.client.get(w.base).json()["documents"][0]["id"] == original
    next_version = (
        w.client.post(url, json={**req, "expected_current_id": original}).raise_for_status().json()["document_id"]
    )
    assert next_version != original
    assert w.client.post(url, json={**req, "expected_current_id": str(uuid4())}).status_code == 409
    other = w.client.post(url, json={**req, "name": "Supplier B scan.txt", "expected_current_id": None})
    assert other.status_code == 202
    assert len(w.client.get(w.base).json()["documents"]) == 2
    while work_one(w.store):
        pass


def test_service_receipt_correction_and_reuse(w):
    w.client.put(
        w.base + "/members", json={"actor": str(w.users["reader"].actor), "permission": "reader"}
    ).raise_for_status()
    master = w.put(
        "Terms.txt", "Buyer convenience termination requires forty-five days of written notice.", "agreement"
    )
    signed = w.put("Proof.txt", "Courier receipt for Buyer: signed March 2, 2026. Notice requests April 15.", "receipt")

    def passage(did):
        return w.client.get(w.base + "/documents/" + did + "/passages").raise_for_status().json()["passages"][0]["id"]

    w.promote([passage(master)]).raise_for_status()
    req = dict(
        snapshot=w.client.get(w.base).json()["snapshot"],
        party="Buyer",
        route="convenience",
        served_on="2026-03-01",
        termination_on="2026-04-15",
        verified_conditions=["written service"],
        evidence_ids=[passage(signed)],
        review_note="Reviewed dispatch slip.",
    )
    create = w.base + "/receipts"
    wrong = w.client.post(create, json=req).raise_for_status().json()["receipt_id"]
    assert w.client.post(create, json=req).raise_for_status().json()["receipt_id"] == wrong

    def calculate(rid, day="2026-03-03"):
        return w.ask("notice", as_of=day, receipt_id=rid).raise_for_status().json()

    original = calculate(wrong)
    assert original["status"] == "supported" and "45 calendar days meets" in original["statements"][0]["text"]
    assert calculate(wrong)["cache_hit"]
    reason = {"reason": "March 1 was dispatch. The signed delivery receipt records March 2."}
    corrected = w.client.post(create, json=req | {"served_on": "2026-03-02"}).raise_for_status().json()["receipt_id"]
    unrelated = calculate(corrected)
    withdraw = create + "/" + wrong + "/withdraw"
    assert "44 calendar days falls short" in unrelated["statements"][0]["text"]
    w.login("reader")
    assert w.client.post(withdraw, json=reason).status_code == 403
    w.login("outside")
    assert w.client.post(withdraw, json=reason).status_code == 404
    assert w.client.get(create).status_code == 404
    w.login("owner")
    assert w.client.post(withdraw, json={"reason": " "}).status_code == 422
    assert w.client.post(withdraw, json=reason).status_code == 200
    assert w.client.post(withdraw, json=reason).status_code == 200
    assert w.client.get("/v1/assessments/" + original["assessment_id"]).status_code == 409
    after = calculate(wrong)
    assert after["status"] == "unresolved" and "withdrew" in after["gaps"][0]
    request = {
        "assessment_id": original["assessment_id"],
        "operation": "prepare_notice_draft",
        "target": "synthetic matter",
        "expected_external_version": "1",
    }
    assert w.client.post("/v1/actions/proposals", json=request).status_code == 409
    assert w.client.post(create, json=req).status_code == 409
    assert w.client.post(withdraw, json={"reason": "Replace the reason."}).status_code == 409
    assert calculate(corrected)["cache_hit"]
    assert w.client.get("/v1/assessments/" + unrelated["assessment_id"]).status_code == 200
    assert [r["id"] for r in w.client.get(create).raise_for_status().json()] == [corrected]
    assert sum(e["event"] == "withdrawn_receipt" for e in w.client.get(w.base + "/audit").json()) == 1
