"""Local matter questions, source retrieval and independently reviewed answers."""

import base64
import json
import re
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field

from . import service
from .contracts import Contract, Document, Text
from .library import library, packet_requirements, required_file_gaps
from .retrieval import BASE, bound, evidence, find
from .store import Problem, digest, dump, uid


class Question(Contract):
    question: Text
    as_of: date = Field(default_factory=date.today)


class Search(Question):
    mode: Literal["question", "reference", "filename"] = "question"


class Proposal(Question):
    snapshot: str = Field(pattern="^[a-f0-9]{64}$")
    answer: Text
    evidence_ids: list[UUID] = Field(min_length=1, max_length=32)


class Decision(Contract):
    decision: Literal["approve", "reject", "withdraw"]
    reason: Text


class Fetch(Contract):
    source: str = Field(min_length=1, max_length=160)
    expected_current_id: UUID | None = None


def fetch(store, actor, mid, req):
    with store.tx() as db:
        service.scoped(db, actor, mid, "edit")
    source = library(mid).get(req.source)
    if source is None:
        raise Problem(404, "Source is absent from this matter's approved list.")
    try:
        if not source.path.is_absolute() or not source.path.is_file():
            raise OSError()
        with source.path.open("rb") as fp:
            data = fp.read(10 * 1024 * 1024 + 1)
    except OSError as exc:
        raise Problem(409, "The records owner must restore the approved file.") from exc
    if len(data) > 10 * 1024 * 1024:
        raise Problem(413, "File exceeds the 10 MiB limit.")
    if digest(data) != source.sha256:
        raise Problem(409, "The file changed. Ask the records owner to verify its version.")
    return service.register(
        store,
        actor,
        mid,
        Document(
            name=req.source,
            role=source.role,
            expected_current_id=req.expected_current_id,
            content_b64=base64.b64encode(data).decode(),
        ),
    )


def question_key(req):
    # Preserve case, punctuation and party identifiers in reusable answers.
    return digest([" ".join(req.question.split()), req.as_of.isoformat()])


def search(db, matter, req):
    mid, snap = matter["id"], service.snapshot(matter)
    incomplete = bool(db.one("SELECT id FROM ev_docs WHERE matter=? AND active=1 AND status!='ready' LIMIT 1", (mid,)))
    required_gaps = required_file_gaps(matter, service.current_docs(db, mid)) if packet_requirements(matter) else {}
    incomplete = incomplete or bool(required_gaps)
    out = {
        "status": "unresolved",
        "snapshot": snap,
        "as_of": req.as_of.isoformat(),
        "answer": None,
        "evidence": [],
        "documents": [],
        "incomplete": incomplete,
        "required_file_gaps": required_gaps,
        "scope": "Current indexed matter files. A matching passage is evidence for review. Only a separately reviewed receipt establishes service in the notice workflow.",
    }
    if req.mode == "question":
        note = db.one(
            "SELECT * FROM ev_notes WHERE matter=? AND snapshot=? AND qkey=? AND status='approved'",
            (mid, snap, question_key(req)),
        )
        if note and not incomplete:
            out.update(
                status="reviewed",
                answer=note["answer"],
                record_id=note["id"],
                evidence=[evidence(db, mid, eid) for eid in json.loads(note["cites"])],
            )
            return out
        hits = find(db, mid, req.question, limit=17)
        out["incomplete"] |= len(hits) > 16
        out["evidence"] = hits[:16]
    elif req.mode == "filename":
        out["documents"] = db.all(
            "SELECT id,name,sha,status FROM ev_docs WHERE matter=? AND active=1 AND name=?", (mid, req.question)
        )
    else:
        # Indexable words can narrow the scan. Single-character IDs like C-7 need a bounded fallback.
        from .ingestion import terms

        tokens = terms(req.question)
        sql, args = BASE, [mid]
        if tokens:
            sql += " AND b.id IN (SELECT block FROM ev_terms WHERE term=?)"
            args.append(max(tokens, key=lambda s: (len(s), s)))
        rows = db.all(sql + " ORDER BY d.name,b.pos LIMIT 1025", args)
        pattern = re.compile(r"(?<![\w-])" + r"\s+".join(map(re.escape, req.question.split())) + r"(?![\w-])", re.I)
        hits = [r for r in rows[:1024] if pattern.search(r["body"])]
        out["incomplete"] |= len(rows) > 1024 or len(hits) > 32
        out["evidence"] = [bound(r) for r in hits[:32]]
    if out["documents"] or out["evidence"]:
        out["status"] = "file_found" if out["documents"] else "passages_only"
    return out


def save(db, matter, actor, req):
    mid, snap, key = matter["id"], service.snapshot(matter), question_key(req)
    pending = isinstance(req, Proposal)
    if pending:
        if req.snapshot != snap:
            raise Problem(409, "The matter changed. Search the current files again.")
        ids = sorted({str(eid) for eid in req.evidence_ids})
        for eid in ids:
            evidence(db, mid, eid)
    else:
        ids = []
    status = "pending" if pending else "open"
    cites, answer = dump(ids), req.answer if pending else ""
    row = db.one(
        """SELECT id FROM ev_notes WHERE matter=? AND qkey=? AND status=? AND answer=? AND cites=?
                    AND (status='open' OR snapshot=?)""",
        (mid, key, status, answer, cites, snap),
    )
    if row:
        return {"id": row["id"], "status": status}
    nid = uid()
    db.execute(
        """INSERT INTO ev_notes(id,matter,snapshot,question,as_of,qkey,answer,cites,status,proposer)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (nid, mid, snap, req.question, req.as_of.isoformat(), key, answer, cites, status, str(actor.actor)),
    )
    service.event(db, mid, actor.actor, "knowledge_" + status, nid)
    return {"id": nid, "status": status}


def decide(db, matter, actor, nid, req):
    mid, snap = matter["id"], service.snapshot(matter)
    row = db.one("SELECT * FROM ev_notes WHERE matter=? AND id=?", (mid, nid))
    if not row:
        raise Problem(404, "Proposal unavailable.")
    if req.decision == "withdraw":
        if row["status"] != "approved":
            raise Problem(409, "Only an approved answer can be withdrawn.")
        state = "withdrawn"
    else:
        if row["status"] != "pending" or row["snapshot"] != snap:
            raise Problem(409, "This proposal is closed or its sources changed.")
        if row["proposer"] == str(actor.actor):
            raise Problem(403, "A different reviewer must check this proposal.")
        state = "approved" if req.decision == "approve" else "rejected"
        if state == "approved":
            if required_file_gaps(matter, service.current_docs(db, mid)):
                raise Problem(409, "Fetch and index all required file versions before approving this answer.")
            if db.one("SELECT id FROM ev_docs WHERE matter=? AND active=1 AND status!='ready' LIMIT 1", (mid,)):
                raise Problem(409, "Finish indexing or reconcile unavailable files first.")
            for eid in json.loads(row["cites"]):
                evidence(db, mid, eid)
            if db.one(
                "SELECT id FROM ev_notes WHERE matter=? AND snapshot=? AND qkey=? AND status='approved'",
                (mid, snap, row["qkey"]),
            ):
                raise Problem(409, "Withdraw the existing answer before approving its replacement.")
            db.execute(
                "UPDATE ev_notes SET status='resolved' WHERE matter=? AND qkey=? AND status='open'", (mid, row["qkey"])
            )
    db.execute(
        "UPDATE ev_notes SET status=?,reviewer=?,reason=? WHERE id=?", (state, str(actor.actor), req.reason, nid)
    )
    service.event(db, mid, actor.actor, "knowledge_" + state, dump([nid, req.reason]))
    return {"id": nid, "status": state}
