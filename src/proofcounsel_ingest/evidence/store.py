"""Transactional matter catalogue. The same schema runs on SQLite and PostgreSQL."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def uid():
    return str(uuid4())


def dump(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else dump(data).encode()).hexdigest()


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


SCHEMA = """
CREATE TABLE IF NOT EXISTS ev_matters (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 decisions INTEGER NOT NULL DEFAULT 0, hosted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ev_members (
 matter TEXT NOT NULL REFERENCES ev_matters(id), actor TEXT NOT NULL, permission TEXT NOT NULL,
 PRIMARY KEY(matter,actor));
CREATE TABLE IF NOT EXISTS ev_docs (
 id TEXT PRIMARY KEY, matter TEXT NOT NULL REFERENCES ev_matters(id), name TEXT NOT NULL,
 role TEXT NOT NULL, sha TEXT NOT NULL, active INTEGER NOT NULL, status TEXT NOT NULL,
 token TEXT, lease DOUBLE PRECISION NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 detail TEXT NOT NULL DEFAULT '', structure TEXT NOT NULL DEFAULT 'none');
CREATE UNIQUE INDEX IF NOT EXISTS ev_active_name ON ev_docs(matter,name) WHERE active=1;
CREATE TABLE IF NOT EXISTS ev_blocks (
 id TEXT PRIMARY KEY, doc TEXT NOT NULL REFERENCES ev_docs(id), pos INTEGER NOT NULL,
 body TEXT NOT NULL, sha TEXT NOT NULL, locator TEXT NOT NULL, heading TEXT NOT NULL,
 protected INTEGER NOT NULL, UNIQUE(doc,pos));
CREATE TABLE IF NOT EXISTS ev_terms (
 term TEXT NOT NULL, block TEXT NOT NULL REFERENCES ev_blocks(id), PRIMARY KEY(term,block));
CREATE TABLE IF NOT EXISTS ev_links (
 matter TEXT NOT NULL REFERENCES ev_matters(id), parent TEXT NOT NULL, child TEXT NOT NULL,
 PRIMARY KEY(matter,parent,child));
CREATE TABLE IF NOT EXISTS ev_runs (
 id TEXT PRIMARY KEY, matter TEXT NOT NULL REFERENCES ev_matters(id), snapshot TEXT NOT NULL,
 cache_key TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ev_cache ON ev_runs(matter,cache_key);
CREATE TABLE IF NOT EXISTS ev_snapshots (
 id TEXT PRIMARY KEY, matter TEXT NOT NULL REFERENCES ev_matters(id), manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ev_receipts (
 id TEXT PRIMARY KEY, matter TEXT NOT NULL REFERENCES ev_matters(id), snapshot TEXT NOT NULL,
 reviewer TEXT NOT NULL, spec TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ev_receipt_withdrawals (
 receipt TEXT PRIMARY KEY REFERENCES ev_receipts(id), reviewer TEXT NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ev_reviews (
 id TEXT PRIMARY KEY, assessment TEXT NOT NULL REFERENCES ev_runs(id), actor TEXT NOT NULL,
 decision TEXT NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ev_review_notes (
 review TEXT NOT NULL REFERENCES ev_reviews(id), doc TEXT NOT NULL REFERENCES ev_docs(id),
 reason TEXT NOT NULL, PRIMARY KEY(review,doc));
CREATE TABLE IF NOT EXISTS ev_rules (
 id TEXT PRIMARY KEY, matter TEXT NOT NULL REFERENCES ev_matters(id), snapshot TEXT NOT NULL,
 reviewer TEXT NOT NULL, spec TEXT NOT NULL, active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ev_proposals (
 id TEXT PRIMARY KEY, matter TEXT NOT NULL REFERENCES ev_matters(id), assessment TEXT NOT NULL REFERENCES ev_runs(id),
 operation_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ev_notes (
 id TEXT PRIMARY KEY, matter TEXT NOT NULL REFERENCES ev_matters(id), snapshot TEXT NOT NULL, question TEXT NOT NULL,
 as_of TEXT NOT NULL, qkey TEXT NOT NULL, answer TEXT NOT NULL, cites TEXT NOT NULL, status TEXT NOT NULL,
 proposer TEXT NOT NULL, reviewer TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ev_notes_lookup ON ev_notes(matter,qkey,snapshot,status);
CREATE TABLE IF NOT EXISTS ev_events (
 id TEXT PRIMARY KEY, matter TEXT NOT NULL REFERENCES ev_matters(id), actor TEXT NOT NULL,
 event TEXT NOT NULL, target TEXT NOT NULL, created TEXT NOT NULL);
"""


class Session:
    def __init__(self, conn, pg):
        self.conn, self.pg = conn, pg

    def execute(self, sql, args=()):
        return self.conn.execute(sql.replace("?", "%s") if self.pg else sql, args)

    def all(self, sql, args=()):
        return [dict(r) for r in self.execute(sql, args)]

    def one(self, sql, args=()):
        row = self.execute(sql, args).fetchone()
        return dict(row) if row else None

    def many(self, sql, rows):
        cur = self.conn.cursor()
        try:
            cur.executemany(sql.replace("?", "%s") if self.pg else sql, rows)
        finally:
            cur.close()


class Store:
    def __init__(self, root, dsn=None):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.dsn = dsn
        with self.tx(write=True) as db:
            if dsn:
                db.execute("SELECT pg_advisory_xact_lock(637580014)")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    @contextmanager
    def tx(self, write=False):
        if self.dsn:
            import psycopg
            from psycopg.rows import dict_row

            conn = psycopg.connect(self.dsn, row_factory=dict_row, client_encoding="utf8")
            if not write:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        else:
            conn = sqlite3.connect(self.root / "catalog.sqlite", timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield Session(conn, bool(self.dsn))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
