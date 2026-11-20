"""HTTP entry point for the evidence catalogue, assessments and source viewer."""

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import notebook, service
from .assessment import assess
from .codex_models import CodexSol
from .contracts import (
    ActionProposal,
    Ask,
    Document,
    Draft,
    Link,
    Matter,
    Member,
    Principal,
    Receipt,
    Review,
    Rule,
    Withdrawal,
)
from .ingestion import source_path
from .retrieval import BASE, bound
from .store import Problem, Store, digest


def load_auth():
    path = os.environ.get("EVIDENCE_AUTH_FILE")
    if not path:
        raise RuntimeError("EVIDENCE_AUTH_FILE must name a server-side credential file")
    records = json.loads(Path(path).read_text())
    if not records or any(len(token) < 32 for token in records):
        raise ValueError("use randomly generated bearer tokens of at least 32 characters")
    return {hashlib.sha256(k.encode()).hexdigest(): Principal.model_validate(v) for k, v in records.items()}


def create_app(store=None, auth=None, providers=None, *, practice=False):
    store = store or Store(os.getenv("EVIDENCE_ROOT", ".evidence"), os.getenv("EVIDENCE_DATABASE_URL"))
    auth = load_auth() if auth is None else auth
    providers = providers or CodexSol()
    app = FastAPI(title="Evidence OS", version="0.4")
    app.state.store = store

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(Problem)
    async def problem(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    def identify(authorization: Annotated[str | None, Header()] = None):
        scheme, _, token = (authorization or "").partition(" ")
        hashed = hashlib.sha256(token.encode()).hexdigest()
        if scheme.lower() == "bearer":
            for stored, principal in auth.items():
                if hmac.compare_digest(stored, hashed):
                    return principal
        raise Problem(401, "valid bearer credential required")

    Actor = Annotated[Principal, Depends(identify)]

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home():
        return (Path(__file__).with_name("viewer.html")).read_text()

    @app.get("/desk", response_class=HTMLResponse, include_in_schema=False)
    def desk():
        page = Path(__file__).with_name("desk.html").read_text()
        return page.replace('data-practice="false"', 'data-practice="true"', 1) if practice else page

    @app.get("/health")
    def health():
        with store.tx() as db:
            db.one("SELECT 1 AS ok")
        return {"status": "ok"}

    @app.post("/v1/source-sets", status_code=201)
    def create(req: Matter, actor: Actor):
        return service.create(store, actor, req)

    @app.get("/v1/source-sets")
    def list_sets(actor: Actor):
        with store.tx() as db:
            return db.all(
                """SELECT m.id,m.name FROM ev_matters m JOIN ev_members u ON u.matter=m.id
                             WHERE m.tenant=? AND u.actor=? ORDER BY m.name""",
                (str(actor.tenant), str(actor.actor)),
            )

    @app.get("/v1/source-sets/{mid}")
    def inventory(mid: str, actor: Actor):
        return service.inventory(store, actor, mid)

    @app.put("/v1/source-sets/{mid}/members")
    def member(mid: str, req: Member, actor: Actor):
        return service.member(store, actor, mid, req)

    @app.post("/v1/source-sets/{mid}/documents", status_code=202)
    def upload(mid: str, req: Document, actor: Actor):
        return service.register(store, actor, mid, req)

    @app.get("/v1/source-sets/{mid}/documents/{did}/passages")
    def passages(mid: str, did: str, actor: Actor, after: int = Query(-1, ge=-1), limit: int = Query(24, ge=1, le=64)):
        with store.tx() as db:
            service.scoped(db, actor, mid)
            if not db.one("SELECT id FROM ev_docs WHERE id=? AND matter=? AND active=1 AND status='ready'", (did, mid)):
                raise Problem(404, "current source unavailable")
            rows = db.all(BASE + " AND d.id=? AND b.pos>? ORDER BY b.pos LIMIT ?", (mid, did, after, limit + 1))
            return {
                "passages": [bound(r) for r in rows[:limit]],
                "next_after": rows[limit - 1]["pos"] if len(rows) > limit else None,
            }

    @app.get("/v1/source-sets/{mid}/documents/{did}/original")
    def original(mid: str, did: str, actor: Actor):
        from urllib.parse import quote

        with store.tx() as db:
            service.scoped(db, actor, mid)
            doc = db.one("SELECT * FROM ev_docs WHERE matter=? AND id=? AND active=1 AND status='ready'", (mid, did))
            if not doc:
                raise Problem(404, "current source unavailable")
            raw = source_path(store.root, doc).read_bytes()
            if digest(raw) != doc["sha"]:
                raise Problem(409, "source integrity check failed")
            return Response(
                raw,
                media_type="application/octet-stream",
                headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(doc["name"], safe="")},
            )

    @app.post("/v1/source-sets/{mid}/links")
    def link(mid: str, req: Link, actor: Actor):
        return service.link(store, actor, mid, req)

    @app.get("/v1/jobs/{jid}")
    def job(jid: str, actor: Actor):
        return service.lookup(store, actor, "job", jid)

    @app.post("/v1/source-sets/{mid}/answers")
    def answer(mid: str, req: Ask, actor: Actor):
        return assess(store, providers, actor, mid, req)

    @app.post("/v1/source-sets/{mid}/draft-checks")
    def draft(mid: str, req: Draft, actor: Actor):
        return assess(store, providers, actor, mid, req)

    @app.get("/v1/assessments/{rid}")
    def assessment(rid: str, actor: Actor):
        return service.lookup(store, actor, "assessment", rid, providers.version)

    @app.get("/v1/evidence/{eid}")
    def passage(eid: str, actor: Actor):
        return service.lookup(store, actor, "evidence", eid)

    @app.post("/v1/assessments/{rid}/reviews")
    def review(rid: str, req: Review, actor: Actor):
        return service.review(store, actor, rid, req, providers.version)

    @app.post("/v1/source-sets/{mid}/notice-rules", status_code=201)
    def rule(mid: str, req: Rule, actor: Actor):
        return service.promote(store, actor, mid, req, providers.version)

    @app.post("/v1/source-sets/{mid}/receipts", status_code=201)
    def receipt(mid: str, req: Receipt, actor: Actor):
        return service.record_receipt(store, actor, mid, req)

    @app.get("/v1/source-sets/{mid}/receipts")
    def receipts(mid: str, actor: Actor):
        with store.tx() as db:
            matter = service.scoped(db, actor, mid)
            rows = db.all(service.CURRENT_RECEIPTS + " ORDER BY r.id", (mid, service.snapshot(matter)))
            return [{"id": row["id"], "receipt": json.loads(row["spec"])} for row in rows]

    @app.post("/v1/source-sets/{mid}/receipts/{rid}/withdraw")
    def withdraw_receipt(mid: str, rid: str, req: Withdrawal, actor: Actor):
        return service.withdraw_receipt(store, actor, mid, rid, req.reason)

    @app.get("/v1/source-sets/{mid}/notice-rules")
    def rules(mid: str, actor: Actor):
        with store.tx() as db:
            m = service.scoped(db, actor, mid)
            return [
                {"id": r["id"], "rule": json.loads(r["spec"])}
                for r in db.all(
                    "SELECT id,spec FROM ev_rules WHERE matter=? AND active=1 AND snapshot=?",
                    (mid, service.snapshot(m)),
                )
            ]

    @app.delete("/v1/source-sets/{mid}/notice-rules/{rid}")
    def revoke(mid: str, rid: str, actor: Actor):
        return service.revoke_rule(store, actor, mid, rid)

    @app.get("/v1/source-sets/{mid}/audit")
    def audit(mid: str, actor: Actor):
        with store.tx() as db:
            service.scoped(db, actor, mid, "owner")
            return db.all("SELECT * FROM ev_events WHERE matter=? ORDER BY created,id", (mid,))

    @app.post("/v1/actions/proposals")
    def proposal(req: ActionProposal, actor: Actor):
        return service.action_proposal(store, actor, req, providers.version)

    @app.post("/v1/source-sets/{mid}/search")
    def search(mid: str, req: notebook.Search, actor: Actor):
        with store.tx() as db:
            return notebook.search(db, service.scoped(db, actor, mid), req)

    @app.get("/v1/source-sets/{mid}/library")
    def library(mid: str, actor: Actor):
        with store.tx() as db:
            service.scoped(db, actor, mid)
            return sorted(notebook.library(mid))

    @app.post("/v1/source-sets/{mid}/fetch", status_code=202)
    def fetch(mid: str, req: notebook.Fetch, actor: Actor):
        return notebook.fetch(store, actor, mid, req)

    @app.post("/v1/source-sets/{mid}/gaps")
    def gap(mid: str, req: notebook.Question, actor: Actor):
        with store.tx(True) as db:
            return notebook.save(db, service.scoped(db, actor, mid, "edit", True), actor, req)

    @app.post("/v1/source-sets/{mid}/knowledge")
    def learn(mid: str, req: notebook.Proposal, actor: Actor):
        with store.tx(True) as db:
            return notebook.save(db, service.scoped(db, actor, mid, "edit", True), actor, req)

    @app.get("/v1/source-sets/{mid}/knowledge")
    def notes(mid: str, actor: Actor, after: str = "", limit: int = Query(50, ge=1, le=100)):
        with store.tx() as db:
            snap = service.snapshot(service.scoped(db, actor, mid))
            rows = db.all("SELECT * FROM ev_notes WHERE matter=? AND id>? ORDER BY id LIMIT ?", (mid, after, limit + 1))
            entries = []
            for row in rows[:limit]:
                current = row["snapshot"] == snap
                entry = {k: row[k] for k in ("id", "question", "as_of", "status")}
                entry.update(
                    current=current,
                    answer=row["answer"] if current else None,
                    reason=row["reason"] if current else None,
                    evidence_ids=json.loads(row["cites"]) if current else [],
                )
                entries.append(entry)
            return {"entries": entries, "next_after": entries[-1]["id"] if len(rows) > limit else None}

    @app.post("/v1/source-sets/{mid}/knowledge/{nid}/review")
    def review_note(mid: str, nid: str, req: notebook.Decision, actor: Actor):
        with store.tx(True) as db:
            return notebook.decide(db, service.scoped(db, actor, mid, "review", True), actor, nid, req)

    return app
