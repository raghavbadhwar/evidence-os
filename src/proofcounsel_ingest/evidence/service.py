"""Authorization, source-set snapshots and review transactions."""

import base64
import binascii
import json
import os
from datetime import UTC, datetime

from .contracts import Receipt, Rule, Scenario
from .ingestion import source_path
from .library import packet_requirements, required_file_gaps
from .retrieval import evidence
from .store import Problem, digest, dump, uid

POLICY = "evidence-r4-review-1"
NOTICE_COVERAGE_POLICY = "notice-document-review-1"


def notice_coverage_policy():
    setting = os.getenv("EVIDENCE_NOTICE_EXCEPTION_READER", "false").lower()
    if setting in {"true", "1"}:
        return "notice-independent-exceptions-1"
    if setting in {"false", "0"}:
        return NOTICE_COVERAGE_POLICY
    raise ValueError("EVIDENCE_NOTICE_EXCEPTION_READER must be true or false")


def now():
    return datetime.now(UTC).isoformat()


def event(db, matter, actor, kind, target):
    db.execute("INSERT INTO ev_events VALUES(?,?,?,?,?,?)", (uid(), matter, str(actor), kind, target, now()))


def scoped(db, actor, matter, permission="read", lock=False):
    sql = """SELECT m.*,u.permission FROM ev_matters m JOIN ev_members u ON m.id=u.matter
             WHERE m.id=? AND m.tenant=? AND u.actor=?"""
    row = db.one(sql + (" FOR UPDATE OF m" if db.pg and lock else ""), (matter, str(actor.tenant), str(actor.actor)))
    if not row:
        raise Problem(404, "source set unavailable")
    if permission == "owner" and row["permission"] != "owner":
        raise Problem(403, "owner access required")
    if permission in {"edit", "review"} and row["permission"] not in {"owner", "editor"}:
        raise Problem(403, "editor access required")
    if permission == "review" and not actor.reviewer:
        raise Problem(403, "reviewer credential required")
    return row


def snapshot(row):
    parts = [row["id"], row["revision"], POLICY]
    if required := packet_requirements(row):
        parts.append(required)
    return digest(parts)


def current_docs(db, matter):
    return db.all(
        """SELECT id,name,role,sha,status,detail,structure FROM ev_docs
                  WHERE matter=? AND active=1 ORDER BY name""",
        (matter,),
    )


def create(store, actor, req):
    mid = uid()
    with store.tx(True) as db:
        db.execute(
            "INSERT INTO ev_matters(id,tenant,name,hosted) VALUES(?,?,?,?)",
            (mid, str(actor.tenant), req.name, int(req.allow_hosted)),
        )
        db.execute("INSERT INTO ev_members VALUES(?,?,?)", (mid, str(actor.actor), "owner"))
        event(db, mid, actor.actor, "created", mid)
    return {"source_set_id": mid}


def inventory(store, actor, mid):
    with store.tx() as db:
        row = scoped(db, actor, mid)
        docs = current_docs(db, mid)
        return {
            "source_set_id": mid,
            "name": row["name"],
            "snapshot": snapshot(row),
            "allow_hosted": bool(row["hosted"]),
            "can_edit": row["permission"] in {"owner", "editor"},
            "can_review": row["permission"] in {"owner", "editor"} and actor.reviewer,
            "documents": docs,
            "required_file_gaps": required_file_gaps(row, docs),
            "links": db.all("SELECT parent,child FROM ev_links WHERE matter=?", (mid,)),
        }


def member(store, actor, mid, req):
    with store.tx(True) as db:
        scoped(db, actor, mid, "owner", True)
        if actor.actor == req.actor:
            raise Problem(409, "owner membership is required")
        db.execute("DELETE FROM ev_members WHERE matter=? AND actor=?", (mid, str(req.actor)))
        if req.permission != "revoked":
            db.execute("INSERT INTO ev_members VALUES(?,?,?)", (mid, str(req.actor), req.permission))
        bump(db, mid)
        event(db, mid, actor.actor, req.permission, str(req.actor))
    return {"permission": req.permission}


def bump(db, mid):
    db.execute("UPDATE ev_matters SET revision=revision+1 WHERE id=?", (mid,))


def register(store, actor, mid, req):
    try:
        data = base64.b64decode(req.content_b64, validate=True)
    except (ValueError, binascii.Error) as err:
        raise Problem(422, "invalid base64 document") from err
    if not 0 < len(data) <= 10 * 1024 * 1024:
        raise Problem(413, "document size outside permitted range")
    sha = digest(data)
    did = uid()
    with store.tx(True) as db:
        scoped(db, actor, mid, "edit", True)
        old = db.one("SELECT * FROM ev_docs WHERE matter=? AND name=? AND active=1", (mid, req.name))
        same = old and (old["sha"], old["role"]) == (sha, req.role) and old["status"] != "failed"
        if "expected_current_id" in req.model_fields_set:
            expected = str(req.expected_current_id) if req.expected_current_id else None
            current = old["id"] if old else None
            if expected != current and not (expected is None and same):
                raise Problem(
                    409, "name or version conflict; choose a distinct name or explicitly replace the current version"
                )
        if same:
            return {"ingestion_job_id": old["id"], "document_id": old["id"], "status": old["status"]}
        path = source_path(store.root, {"matter": mid, "id": did, "name": req.name})
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_bytes(data)
        if old:
            db.execute("UPDATE ev_docs SET active=0 WHERE id=?", (old["id"],))
            db.execute("UPDATE ev_links SET parent=? WHERE matter=? AND parent=?", (did, mid, old["id"]))
            db.execute("UPDATE ev_links SET child=? WHERE matter=? AND child=?", (did, mid, old["id"]))
        db.execute(
            "INSERT INTO ev_docs(id,matter,name,role,sha,active,status) VALUES(?,?,?,?,?,1,'queued')",
            (did, mid, req.name, req.role, sha),
        )
        bump(db, mid)
        event(db, mid, actor.actor, "registered", did)
    return {"ingestion_job_id": did, "document_id": did, "status": "queued"}


def link(store, actor, mid, req):
    pair = [str(req.parent_document), str(req.dependency_document)]
    with store.tx(True) as db:
        scoped(db, actor, mid, "edit", True)
        ids = {r["id"] for r in current_docs(db, mid)}
        if len(set(pair)) != 2 or not set(pair) <= ids:
            raise Problem(422, "both distinct documents must be current members")
        if db.execute("INSERT INTO ev_links VALUES(?,?,?) ON CONFLICT DO NOTHING", (mid, *pair)).rowcount:
            bump(db, mid)
            event(db, mid, actor.actor, "dependency", dump(pair))
    return {"parent": pair[0], "dependency": pair[1]}


def lookup(store, actor, kind, object_id, versions=None):
    with store.tx() as db:
        if kind == "job":
            row = db.one("SELECT id,matter,status,detail,active,structure FROM ev_docs WHERE id=?", (object_id,))
        elif kind == "evidence":
            row = db.one("SELECT d.matter FROM ev_blocks b JOIN ev_docs d ON b.doc=d.id WHERE b.id=?", (object_id,))
        else:
            row = db.one("SELECT * FROM ev_runs WHERE id=?", (object_id,))
        if not row:
            raise Problem(404, "resource unavailable")
        m = scoped(db, actor, row["matter"])
        if kind == "job":
            return row
        if kind == "evidence":
            return evidence(db, m["id"], object_id)
        return read_run(db, m, row, versions)


def read_run(db, m, row, versions=None):
    if row["snapshot"] != snapshot(m):
        raise Problem(409, "source set changed; reassessment required")
    out = json.loads(row["payload"])
    if (
        versions is not None
        and out["mode"] in {"interpret", "draft", "locate"}
        and out.get("provider_versions") != list(versions)
    ):
        raise Problem(409, "model or verification policy changed; reassessment required")
    if out.get("rule_id"):
        rid = out["request"].get("receipt_id")
        if rid and not current_receipt(db, m["id"], row["snapshot"], rid):
            raise Problem(409, "service receipt withdrawn; reassess the notice")
        active = db.one("SELECT active FROM ev_rules WHERE id=?", (out["rule_id"],))
        if not active or not active["active"]:
            raise Problem(409, "notice rule revoked")
        if out.get("rule_epoch") != m["decisions"]:
            raise Problem(409, "reviewed rules changed; rerun scenario")
    if "notice_review_epoch" in out:
        if out.get("coverage_policy") != notice_coverage_policy():
            raise Problem(409, "notice review policy changed; reassess the packet")
        if out["notice_review_epoch"] != m["decisions"]:
            raise Problem(409, "reviewed rules changed; reassess the notice finding")
        for p in out["evidence"]:
            evidence(db, m["id"], p["id"])
    rev = db.one(
        "SELECT id,decision,reason,created FROM ev_reviews WHERE assessment=? ORDER BY created DESC LIMIT 1",
        (row["id"],),
    )
    if rev:
        rev["document_notes"] = {
            n["doc"]: n["reason"] for n in db.all("SELECT doc,reason FROM ev_review_notes WHERE review=?", (rev["id"],))
        }
        out["review"] = rev
        if rev["decision"] == "approve":
            out["statements"] = out["proposals"]
            out["status"] = "reviewed"
        else:
            out["statements"] = []
            out["status"] = "rejected"
    return out


def review(store, actor, rid, req, versions=None):
    with store.tx(True) as db:
        r = db.one("SELECT * FROM ev_runs WHERE id=?", (rid,))
        if not r:
            raise Problem(404, "assessment unavailable")
        m = scoped(db, actor, r["matter"], "review", True)
        result = read_run(db, m, r, versions)
        if result["status"] != "needs_review" or not result["proposals"]:
            raise Problem(409, "assessment has no reviewable proposal")
        if req.decision == "approve" and (
            result["gaps"] or any(p.get("verdict") != "supported" for p in result["proposals"])
        ):
            raise Problem(409, "resolve missing or conflicting support before approval")
        notes = {str(k): v for k, v in req.document_notes.items()}
        required = set(result.get("unexplained_documents", []))
        if not notes.keys() <= required or (req.decision == "approve" and notes.keys() != required):
            raise Problem(409, "account for each unexplained document before approval")
        review_id = uid()
        db.execute(
            "INSERT INTO ev_reviews VALUES(?,?,?,?,?,?)",
            (review_id, rid, str(actor.actor), req.decision, req.reason, now()),
        )
        if notes:
            db.many(
                "INSERT INTO ev_review_notes VALUES(?,?,?)", [(review_id, doc, note) for doc, note in notes.items()]
            )
        event(db, m["id"], actor.actor, "review_" + req.decision, rid)
        return {"review_event_id": review_id, "assessment": read_run(db, m, r, versions)}


def promote(store, actor, mid, rule, versions=None):
    with store.tx(True) as db:
        m = scoped(db, actor, mid, "review", True)
        docs = current_docs(db, mid)
        if required_file_gaps(m, docs):
            raise Problem(409, "Fetch and index all required file versions before approving a notice rule.")
        if rule.snapshot != snapshot(m) or not docs or any(d["status"] != "ready" for d in docs):
            raise Problem(409, "review requires a current, fully ingested source set")
        if rule.reviewed_assessment_id is not None:
            row = db.one("SELECT * FROM ev_runs WHERE matter=? AND id=?", (mid, str(rule.reviewed_assessment_id)))
            if not row:
                raise Problem(404, "reviewed finding unavailable")
            result = read_run(db, m, row, versions)
            finding = result.get("notice_review")
            if result["status"] != "reviewed" or not finding:
                raise Problem(409, "current approved notice finding required")
            if any(finding[key] != getattr(rule, key) for key in ("party", "route", "minimum_calendar_days")):
                raise Problem(422, "rule scope or period differs from approved finding")
            if not set(finding["evidence_ids"]).issubset(str(eid) for eid in rule.evidence_ids):
                raise Problem(422, "rule omits approved controlling evidence")
        spans = [evidence(db, mid, eid) for eid in rule.evidence_ids]
        # A reviewer must explicitly bind every registered controlling document.
        required = {d["id"] for d in docs if d["role"] in {"agreement", "amendment", "schedule", "definition"}}
        required.update(x["child"] for x in db.all("SELECT child FROM ev_links WHERE matter=?", (mid,)))
        if not required <= {p["document_id"] for p in spans}:
            raise Problem(422, "rule lacks passages from controlling documents")
        rid = uid()
        db.execute(
            "INSERT INTO ev_rules VALUES(?,?,?,?,?,1)",
            (rid, mid, rule.snapshot, str(actor.actor), rule.model_dump_json()),
        )
        db.execute("UPDATE ev_matters SET decisions=decisions+1 WHERE id=?", (mid,))
        event(db, mid, actor.actor, "promoted_rule", rid)
        return {"rule_id": rid}


def revoke_rule(store, actor, mid, rid):
    with store.tx(True) as db:
        scoped(db, actor, mid, "review", True)
        if not db.execute("UPDATE ev_rules SET active=0 WHERE matter=? AND id=? AND active=1", (mid, rid)).rowcount:
            raise Problem(404, "active rule unavailable")
        db.execute("UPDATE ev_matters SET decisions=decisions+1 WHERE id=?", (mid,))
        event(db, mid, actor.actor, "revoked_rule", rid)
    return {"status": "revoked"}


CURRENT_RECEIPTS = """SELECT r.id,r.spec FROM ev_receipts r WHERE r.matter=? AND r.snapshot=?
                      AND NOT EXISTS (SELECT 1 FROM ev_receipt_withdrawals w WHERE w.receipt=r.id)"""


def current_receipt(db, mid, snap, rid):
    return db.one(CURRENT_RECEIPTS + " AND r.id=?", (mid, snap, str(rid)))


def withdraw_receipt(store, actor, mid, rid, reason):
    with store.tx(True) as db:
        scoped(db, actor, mid, "review", True)
        if not db.one("SELECT id FROM ev_receipts WHERE matter=? AND id=?", (mid, rid)):
            raise Problem(404, "receipt unavailable in this matter")
        existing = db.one("SELECT reason FROM ev_receipt_withdrawals WHERE receipt=?", (rid,))
        if existing and existing["reason"] != reason:
            raise Problem(409, "receipt already withdrawn with a recorded reason")
        if not existing:
            db.execute("INSERT INTO ev_receipt_withdrawals VALUES(?,?,?,?)", (rid, str(actor.actor), reason, now()))
            event(db, mid, actor.actor, "withdrawn_receipt", dump([rid, reason]))
    return {"receipt_id": rid, "status": "withdrawn"}


def notice(db, m, req, out):
    q = req.scenario
    receipt = None
    effective = req.as_of
    if req.receipt_id:
        row = current_receipt(db, m["id"], snapshot(m), req.receipt_id)
        if not row:
            withdrawn = db.one(
                """SELECT w.receipt FROM ev_receipt_withdrawals w JOIN ev_receipts r ON r.id=w.receipt
                   WHERE r.matter=? AND r.snapshot=? AND r.id=?""",
                (m["id"], snapshot(m), str(req.receipt_id)),
            )
            out["gaps"] = [
                "The reviewer withdrew this delivery record. Choose a current reviewed receipt."
                if withdrawn
                else "current reviewer-verified service receipt required"
            ]
            return
        receipt = Receipt.model_validate_json(row["spec"])
        if receipt.served_on > req.as_of:
            out["gaps"] = ["service falls after assessment date"]
            return
        effective = receipt.served_on
        q = Scenario(
            party=receipt.party,
            route=receipt.route,
            proposed_days=(receipt.termination_on - receipt.served_on).days,
            assumed_conditions=receipt.verified_conditions,
        )
    matching = []
    for row in db.all(
        "SELECT id,spec FROM ev_rules WHERE matter=? AND snapshot=? AND active=1", (m["id"], snapshot(m))
    ):
        rule = Rule.model_validate_json(row["spec"])
        if (rule.party.casefold(), rule.route.casefold()) == (q.party.casefold(), q.route.casefold()) and (
            rule.effective_from <= effective and (not rule.effective_until or effective < rule.effective_until)
        ):
            matching.append((row["id"], rule))
    if len(matching) != 1:
        out["gaps"] = ["overlapping reviewed rules" if matching else "no applicable reviewed rule"]
        return
    rid, r = matching[0]
    missing = set(r.conditions) - set(q.assumed_conditions)
    if missing:
        out["gaps"] = ["unknown condition: " + v for v in sorted(missing)]
        return
    citations = [evidence(db, m["id"], eid) for eid in r.evidence_ids]
    result = "meets" if q.proposed_days >= r.minimum_calendar_days else "falls short of"
    basis = "This hypothetical assumes: " + ", ".join(r.conditions) + "."
    if receipt:
        basis = f"Reviewed service date {receipt.served_on}, proposed termination date {receipt.termination_on}."
        citations.extend(evidence(db, m["id"], eid) for eid in receipt.evidence_ids)
    line = (
        f"For {q.party} on the {q.route} route, {q.proposed_days} calendar days {result} "
        f"the reviewed minimum of {r.minimum_calendar_days} calendar days. "
        f"{basis}"
    )
    out.update(
        status="supported",
        rule_id=rid,
        rule_epoch=m["decisions"],
        evidence=citations,
        statements=[{"text": line, "evidence_ids": [p["id"] for p in citations]}],
    )


def record_receipt(store, actor, mid, receipt):
    with store.tx(True) as db:
        m = scoped(db, actor, mid, "review", True)
        docs = current_docs(db, mid)
        if snapshot(m) != receipt.snapshot or required_file_gaps(m, docs) or any(d["status"] != "ready" for d in docs):
            raise Problem(409, "receipt requires a fully ingested current snapshot")
        spans = [evidence(db, mid, i) for i in receipt.evidence_ids]
        if not any(p["role"] == "receipt" for p in spans):
            raise Problem(422, "a passage from registered service evidence is required")
        if (receipt.termination_on - receipt.served_on).days > 36500:
            raise Problem(422, "dated interval exceeds notice rule limit")
        spec = receipt.model_dump_json()
        prior = db.one(
            """SELECT r.id,w.receipt AS withdrawn FROM ev_receipts r
               LEFT JOIN ev_receipt_withdrawals w ON w.receipt=r.id
               WHERE r.matter=? AND r.snapshot=? AND r.reviewer=? AND r.spec=?
               ORDER BY CASE WHEN w.receipt IS NULL THEN 0 ELSE 1 END,r.id LIMIT 1""",
            (mid, receipt.snapshot, str(actor.actor), spec),
        )
        if prior:
            if prior["withdrawn"]:
                raise Problem(409, "this delivery record was withdrawn; review the source and correction")
            return {"receipt_id": prior["id"]}
        rid = uid()
        db.execute(
            "INSERT INTO ev_receipts VALUES(?,?,?,?,?)",
            (rid, mid, receipt.snapshot, str(actor.actor), spec),
        )
        event(db, mid, actor.actor, "verified_receipt", rid)
    return {"receipt_id": rid}


def action_proposal(store, actor, request, versions=None):
    with store.tx(True) as db:
        row = db.one("SELECT * FROM ev_runs WHERE id=?", (str(request.assessment_id),))
        if not row:
            raise Problem(404, "assessment unavailable")
        matter = scoped(db, actor, row["matter"], "edit", True)
        result = read_run(db, matter, row, versions)
        if result["status"] not in {"reviewed", "supported"}:
            raise Problem(409, "a current supported or reviewed assessment is required")
        key = digest([str(actor.actor), result["snapshot"], request.model_dump(mode="json")])
        old = db.one("SELECT payload FROM ev_proposals WHERE operation_key=?", (key,))
        if old:
            return json.loads(old["payload"])
        pid = uid()
        item = {
            **request.model_dump(mode="json"),
            "proposal_id": pid,
            "status": "proposal_only",
            "snapshot": result["snapshot"],
            "idempotency_key": key,
            "actor": str(actor.actor),
            "unmet_preconditions": ["external execution disabled", "external authority and target version unverified"],
        }
        db.execute("INSERT INTO ev_proposals VALUES(?,?,?,?,?)", (pid, matter["id"], row["id"], key, dump(item)))
        event(db, matter["id"], actor.actor, "proposed_" + request.operation, pid)
        return item
