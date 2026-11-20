"""Durable leases and atomic publication of ready, canonical evidence."""

import json
import re
import time
from uuid import UUID, uuid5

from .store import digest, dump, uid

QUALIFIER = re.compile(
    r"\b(except|unless|subject to|provided that|notwithstanding|supersed\w*|amend\w*|defined|definition|schedule|proviso|effective|conditional|only if|instead)\b",
    re.I,
)
STOP = set(
    [
        "a",
        "an",
        "the",
        "for",
        "is",
        "are",
        "what",
        "does",
        "with",
        "that",
        "and",
        "or",
        "in",
        "on",
        "of",
        "to",
        "it",
        "as",
        "from",
        "can",
        "this",
        "my",
        "at",
        "how",
        "which",
    ]
)


def terms(text):
    return set(re.findall(r"[^\W_]{2,80}", text.casefold())) - STOP


def source_path(root, doc):
    return root / "uploads" / doc["matter"] / doc["id"] / doc["name"]


def flash_ranges(path):
    if path.suffix.lower() != ".pdf":
        return [], "canonical"
    try:
        from pageindex.flash import page_index_flash

        result = page_index_flash(str(path), summary=False, optimize=False)
        if result.get("toc_source") not in {"bookmarks", "detected", "hybrid"}:
            return [], "canonical:flat-or-unreadable"
        spans = []
        stack = list(result["structure"])
        while stack:
            n = stack.pop()
            a, b = n["start_index"], n["end_index"]
            if isinstance(a, int) and isinstance(b, int) and 1 <= a <= b:
                spans.append((a, b, str(n["title"])[:400]))
            stack.extend(n.get("nodes", []))
        return (
            (sorted(spans, key=lambda s: (s[1] - s[0], -s[0])), "pageindex-flash")
            if spans
            else ([], "canonical:empty-hierarchy")
        )
    except Exception:
        return [], "canonical:flash-unavailable"


def extract(root, doc, tenant):
    from proofcounsel_ingest.config import Settings
    from proofcounsel_ingest.orchestrator import ingest

    path = source_path(root, doc)
    if digest(path.read_bytes()) != doc["sha"]:
        raise ValueError("source integrity check failed")
    settings = Settings(
        tenant_id=UUID(tenant),
        matter_id=UUID(doc["matter"]),
        artifact_root=root / "canonical",
        database_url="sqlite:///" + str((root / "ingest.sqlite").resolve()),
    )
    outcome = ingest(path, settings=settings)
    if outcome.status.value.lower() != "ready" or not outcome.artifact_path:
        raise ValueError("canonical ingestion: " + outcome.status.value)
    raw = (outcome.artifact_path / "blocks.jsonl").read_text().splitlines()
    spans, method = flash_ranges(path)
    blocks = []
    for line in raw:
        b = json.loads(line)
        if not b["metadata"].get("queryable_evidence", True):
            continue
        body = b["text"]
        if digest(body.encode()) != b["text_sha256"]:
            raise ValueError("canonical passage integrity check failed")
        loc = b["locator"]
        page = loc.get("page") or 0
        heading = next((t for a, z, t in spans if a <= page <= z), "")
        block_id = str(uuid5(UUID(doc["id"]), str(b["block_id"])))
        locator = {**loc, "canonical_block_id": b["block_id"]}
        protected = bool(QUALIFIER.search(body)) or bool(loc.get("table_id") or loc.get("cell_range"))
        blocks.append(
            (block_id, doc["id"], b["ordinal"], body, b["text_sha256"], dump(locator), heading, int(protected))
        )
    if not blocks:
        raise ValueError("no queryable canonical passages")
    return blocks, method


def work_one(store):
    now = time.time()
    token = uid()
    with store.tx(write=True) as db:
        sql = """SELECT d.*,m.tenant FROM ev_docs d JOIN ev_matters m ON m.id=d.matter
                 WHERE active=1 AND (status='queued' OR (status='processing' AND lease<?))
                 ORDER BY attempts,id LIMIT 1"""
        doc = db.one(sql + (" FOR UPDATE OF d SKIP LOCKED" if db.pg else ""), (now,))
        if not doc:
            return False
        db.execute(
            "UPDATE ev_docs SET status='processing',lease=?,token=?,attempts=attempts+1 WHERE id=?",
            (now + 300, token, doc["id"]),
        )
    try:
        if doc["attempts"] >= 3:
            raise ValueError("ingestion retry limit reached")
        blocks, method = extract(store.root, doc, doc["tenant"])
        status, detail = "ready", ""
    except Exception as error:
        blocks, method = [], "none"
        status, detail = "failed", str(error)[:240] if isinstance(error, ValueError) else type(error).__name__
    with store.tx(write=True) as db:
        # Same lock order as HTTP scope changes. A superseded lease cannot publish.
        db.execute("UPDATE ev_matters SET revision=revision WHERE id=?", (doc["matter"],))
        changed = db.execute(
            """UPDATE ev_docs SET status=?,detail=?,structure=?,token=NULL
                                WHERE id=? AND active=1 AND token=?""",
            (status, detail, method, doc["id"], token),
        ).rowcount
        if changed:
            db.many("INSERT INTO ev_blocks VALUES(?,?,?,?,?,?,?,?)", blocks)
            db.many(
                "INSERT INTO ev_terms VALUES(?,?)", ((term, b[0]) for b in blocks for term in terms(b[3] + " " + b[6]))
            )
            db.execute("UPDATE ev_matters SET revision=revision+1 WHERE id=?", (doc["matter"],))
    return True
