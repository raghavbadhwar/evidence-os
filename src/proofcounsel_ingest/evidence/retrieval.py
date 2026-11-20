"""Matter-scoped retrieval, complete original passages, server-owned locators."""

import json

from .ingestion import terms
from .store import Problem, digest

BASE = """SELECT b.*,d.name,d.role,d.sha AS source_sha,d.matter
          FROM ev_blocks b JOIN ev_docs d ON b.doc=d.id
          WHERE d.matter=? AND d.active=1 AND d.status='ready'"""


def bound(row):
    if digest(row["body"].encode()) != row["sha"]:
        raise Problem(409, "passage integrity check failed")
    return {
        "id": row["id"],
        "document_id": row["doc"],
        "filename": row["name"],
        "role": row["role"],
        "source_sha256": row["source_sha"],
        "text_sha256": row["sha"],
        "locator": json.loads(row["locator"]),
        "heading": row["heading"],
        "text": row["body"],
    }


def find(db, matter, query, limit=16):
    words = sorted(terms(query))[:80]
    if not words:
        return []
    marks = ",".join("?" for _ in words)
    sql = (
        BASE
        + f""" AND b.id IN (SELECT block FROM ev_terms WHERE term IN ({marks}))
                      ORDER BY (SELECT count(*) FROM ev_terms t WHERE t.block=b.id
                                AND t.term IN ({marks})) DESC,d.name,b.pos LIMIT ?"""
    )
    return [bound(r) for r in db.all(sql, (matter, *words, *words, limit))]


def evidence(db, matter, eid):
    row = db.one(BASE + " AND b.id=?", (matter, str(eid)))
    if not row:
        raise Problem(404, "evidence unavailable")
    return bound(row)


def full_packet(db, matter):
    # shortcut: bounded full-packet review, add independent retrieval for larger matters.
    rows = db.all(BASE + " ORDER BY d.name,b.pos LIMIT 65", (matter,))
    if len(rows) > 64:
        return []
    return [bound(row) for row in rows]


def protect(db, matter, anchors):
    # Every marked dependency is independently fetched. Overflow is explicit.
    sql = (
        BASE
        + """ AND (b.protected=1 OR d.role IN ('amendment','schedule','definition','receipt')
         OR EXISTS(SELECT 1 FROM ev_links l WHERE l.matter=d.matter AND l.child=d.id))"""
    )
    refs = [(a["document_id"], a["id"]) for a in anchors]
    rows = db.all(sql + " ORDER BY d.name,b.pos LIMIT 65", (matter,))
    if len(rows) > 64:
        return [], "more than 64 protected passages require section review"
    by_id = {r["id"]: bound(r) for r in rows}
    if refs:
        marks = ",".join("?" for _ in refs)
        near = (
            BASE
            + f""" AND EXISTS(SELECT 1 FROM ev_blocks a WHERE a.id IN ({marks})
                    AND a.doc=b.doc AND b.pos BETWEEN a.pos-1 AND a.pos+1)"""
        )
        for row in db.all(near, (matter, *(eid for _, eid in refs))):
            by_id[row["id"]] = bound(row)
    return list(by_id.values()), ""


def packet_for(anchors, mandatory):
    items = {p["id"]: p for p in [*mandatory, *anchors]}
    return list(items.values())
