"""Tenant-scoped persistent lexical repository ports and SQLite FTS5 adapter."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from proofcounsel_ingest.config import Settings
from proofcounsel_ingest.invariants import require_tenant_scope
from proofcounsel_ingest.models import ContentBlock, StructureNode
from proofcounsel_ingest.retrieval.lexical import normalized_terms, raw_terms


class SearchRecord(BaseModel):
    tenant_id: UUID
    document_id: UUID
    source_sha256: str
    block_id: UUID
    node_id: UUID | None = None
    ordinal: int = Field(ge=0)
    text: str
    raw_score: float
    channel: str


@dataclass(frozen=True, slots=True)
class _SparseDocumentIndex:
    """Immutable postings over one already-validated document snapshot."""

    blocks: tuple[ContentBlock, ...]
    nodes: tuple[StructureNode, ...]
    block_postings: dict[str, tuple[int, ...]]
    node_postings: dict[str, tuple[int, ...]]
    block_terms_by_trigram: dict[str, tuple[str, ...]]
    node_terms_by_trigram: dict[str, tuple[str, ...]]


class RetrievalRepository(Protocol):
    implementation: str

    def index_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config_hash: str,
        blocks: Sequence[ContentBlock],
        nodes: Sequence[StructureNode],
        original_filename: str = "indexed-document",
        detected_mime: str = "application/octet-stream",
        byte_size: int = 0,
        schema_version: str = "1.0",
        extractor_version: str = "retrieval-only",
        quality: Mapping[str, object] | None = None,
        publish_ready: bool = True,
        index_descriptor: Mapping[str, object] | None = None,
        matter_id: UUID | None = None,
    ) -> None: ...

    def mark_ready(self, *, tenant_id: UUID, document_id: UUID, source_sha256: str, config_hash: str) -> None: ...

    def search_blocks(
        self,
        *,
        tenant_id: UUID,
        query: str,
        document_id: UUID | None = None,
        limit: int = 50,
    ) -> list[SearchRecord]: ...

    def search_nodes(
        self,
        *,
        tenant_id: UUID,
        query: str,
        document_id: UUID | None = None,
        limit: int = 20,
    ) -> list[SearchRecord]: ...

    def get_blocks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> list[ContentBlock]: ...

    def get_nodes(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
    ) -> list[StructureNode]: ...

    def has_index(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config_hash: str | None = None,
    ) -> bool: ...

    def get_index_descriptor(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config_hash: str | None = None,
    ) -> dict[str, object] | None: ...

    def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> None: ...


class SQLiteRetrievalRepository:
    implementation = "sqlite-fts5"

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._cache_lock = threading.RLock()
        self._document_content_cache: OrderedDict[
            tuple[str, str],
            tuple[str | None, tuple[ContentBlock, ...], tuple[StructureNode, ...]],
        ] = OrderedDict()
        self._document_search_cache: OrderedDict[tuple[str, str], _SparseDocumentIndex] = OrderedDict()
        self._has_index_cache: OrderedDict[tuple[str, str, str, str | None], bool] = OrderedDict()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        existing = getattr(self._thread_local, "connection", None)
        if isinstance(existing, sqlite3.Connection):
            return existing
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        with self._connections_lock:
            self._connections.append(connection)
        self._thread_local.connection = connection
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Keep transaction semantics on the repository's thread-local connection."""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def close(self) -> None:
        """Close connections owned by this repository across worker threads."""
        self._clear_caches()
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()
        self._thread_local.connection = None

    def _cached_document_content(
        self,
        tenant_id: str,
        document_id: str,
    ) -> tuple[str | None, tuple[ContentBlock, ...], tuple[StructureNode, ...]]:
        key = (tenant_id, document_id)
        with self._cache_lock:
            cached = self._document_content_cache.get(key)
            if cached is not None:
                self._document_content_cache.move_to_end(key)
                return cached
        with self._connection() as connection:
            source_row = connection.execute(
                "SELECT source_sha256 FROM indexed_documents "
                "WHERE tenant_id = ? AND document_id = ? AND status = 'ready'",
                (tenant_id, document_id),
            ).fetchone()
            block_rows = connection.execute(
                "SELECT payload_json FROM blocks WHERE tenant_id = ? AND document_id = ? ORDER BY ordinal",
                (tenant_id, document_id),
            ).fetchall()
            node_rows = connection.execute(
                "SELECT payload_json FROM nodes WHERE tenant_id = ? AND document_id = ? ORDER BY ordinal",
                (tenant_id, document_id),
            ).fetchall()
        loaded = (
            str(source_row["source_sha256"]) if source_row is not None else None,
            tuple(ContentBlock.model_validate_json(row["payload_json"]) for row in block_rows),
            tuple(StructureNode.model_validate_json(row["payload_json"]) for row in node_rows),
        )
        with self._cache_lock:
            self._document_content_cache[key] = loaded
            self._document_content_cache.move_to_end(key)
            while len(self._document_content_cache) > 256:
                self._document_content_cache.popitem(last=False)
        return loaded

    def _remember_document_content(
        self,
        tenant_id: str,
        document_id: str,
        source_sha256: str,
        blocks: Sequence[ContentBlock],
        nodes: Sequence[StructureNode],
    ) -> None:
        key = (tenant_id, document_id)
        loaded = (source_sha256, tuple(blocks), tuple(nodes))
        with self._cache_lock:
            self._document_content_cache[key] = loaded
            self._document_content_cache.move_to_end(key)
            while len(self._document_content_cache) > 256:
                self._document_content_cache.popitem(last=False)

    def _cached_document_search_index(
        self,
        tenant_id: str,
        document_id: str,
    ) -> tuple[str | None, _SparseDocumentIndex]:
        key = (tenant_id, document_id)
        with self._cache_lock:
            cached = self._document_search_cache.get(key)
            if cached is not None:
                self._document_search_cache.move_to_end(key)
                source_sha256, _blocks, _nodes = self._document_content_cache.get(
                    key, (None, (), ())
                )
                return source_sha256, cached
        source_sha256, blocks, nodes = self._cached_document_content(tenant_id, document_id)
        if source_sha256 is None:
            return None, _SparseDocumentIndex((), (), {}, {}, {}, {})
        built = _build_sparse_document_index(blocks, nodes)
        with self._cache_lock:
            existing = self._document_search_cache.get(key)
            if existing is not None:
                self._document_search_cache.move_to_end(key)
                return source_sha256, existing
            self._document_search_cache[key] = built
            self._document_search_cache.move_to_end(key)
            while len(self._document_search_cache) > 64:
                self._document_search_cache.popitem(last=False)
        return source_sha256, built

    def _cached_has_index(
        self,
        tenant_id: str,
        document_id: str,
        source_sha256: str,
        config_hash: str | None,
    ) -> bool:
        key = (tenant_id, document_id, source_sha256, config_hash)
        with self._cache_lock:
            if key in self._has_index_cache:
                cached = self._has_index_cache[key]
                self._has_index_cache.move_to_end(key)
                return cached
        clauses = ["tenant_id = ?", "document_id = ?", "source_sha256 = ?", "status = 'ready'"]
        parameters: list[object] = [tenant_id, document_id, source_sha256]
        if config_hash is not None:
            clauses.append("config_hash = ?")
            parameters.append(config_hash)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM indexed_documents WHERE " + " AND ".join(clauses),
                parameters,
            ).fetchone()
        ready = row is not None
        with self._cache_lock:
            self._has_index_cache[key] = ready
            self._has_index_cache.move_to_end(key)
            while len(self._has_index_cache) > 2048:
                self._has_index_cache.popitem(last=False)
        return ready

    def _clear_caches(self) -> None:
        with self._cache_lock:
            self._document_content_cache.clear()
            self._document_search_cache.clear()
            self._has_index_cache.clear()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS indexed_documents (
                    tenant_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    descriptor_json TEXT NOT NULL DEFAULT '{}',
                    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, document_id)
                );
                CREATE TABLE IF NOT EXISTS blocks (
                    block_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS blocks_tenant_document_ordinal_idx
                    ON blocks (tenant_id, document_id, ordinal);
                CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(
                    block_id UNINDEXED,
                    text,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    start_block_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS nodes_tenant_document_ordinal_idx
                    ON nodes (tenant_id, document_id, ordinal);
                CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
                    node_id UNINDEXED,
                    title,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS index_build_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(indexed_documents)")}
            if "status" not in columns:
                connection.execute(
                    "ALTER TABLE indexed_documents ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'"
                )
            if "descriptor_json" not in columns:
                connection.execute(
                    "ALTER TABLE indexed_documents ADD COLUMN descriptor_json TEXT NOT NULL DEFAULT '{}'"
                )

    def index_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config_hash: str,
        blocks: Sequence[ContentBlock],
        nodes: Sequence[StructureNode],
        original_filename: str = "indexed-document",
        detected_mime: str = "application/octet-stream",
        byte_size: int = 0,
        schema_version: str = "1.0",
        extractor_version: str = "retrieval-only",
        quality: Mapping[str, object] | None = None,
        publish_ready: bool = True,
        index_descriptor: Mapping[str, object] | None = None,
        matter_id: UUID | None = None,
    ) -> None:
        require_tenant_scope(tenant_id)
        tenant = str(tenant_id)
        document = str(document_id)
        queryable_blocks = [block for block in blocks if block.metadata.get("queryable_evidence", True)]
        expected_descriptor = dict(index_descriptor or {})
        self._clear_caches()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT source_sha256, config_hash, status, descriptor_json FROM indexed_documents "
                "WHERE tenant_id = ? AND document_id = ?",
                (tenant, document),
            ).fetchone()
            if existing is not None and existing[0] == source_sha256 and existing[1] == config_hash:
                if json.loads(existing[3]) != expected_descriptor:
                    raise ValueError("immutable index version has a different descriptor")
                if publish_ready and existing[2] != "ready":
                    connection.execute(
                        "UPDATE indexed_documents SET status = 'ready' "
                        "WHERE tenant_id = ? AND document_id = ? AND config_hash = ?",
                        (tenant, document, config_hash),
                    )
                connection.commit()
                if publish_ready or existing[2] == "ready":
                    self._remember_document_content(
                        tenant,
                        document,
                        source_sha256,
                        queryable_blocks,
                        nodes,
                    )
                return
            if existing is not None and (existing[0] != source_sha256 or existing[1] != config_hash):
                raise ValueError("immutable document version already has a different index")
            existing_block_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT block_id FROM blocks WHERE tenant_id = ? AND document_id = ?",
                    (tenant, document),
                )
            ]
            existing_node_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT node_id FROM nodes WHERE tenant_id = ? AND document_id = ?",
                    (tenant, document),
                )
            ]
            connection.executemany("DELETE FROM block_fts WHERE block_id = ?", ((item,) for item in existing_block_ids))
            connection.executemany("DELETE FROM node_fts WHERE node_id = ?", ((item,) for item in existing_node_ids))
            connection.execute("DELETE FROM blocks WHERE tenant_id = ? AND document_id = ?", (tenant, document))
            connection.execute("DELETE FROM nodes WHERE tenant_id = ? AND document_id = ?", (tenant, document))
            connection.execute("DELETE FROM indexed_documents WHERE tenant_id = ? AND document_id = ?", (tenant, document))

            for block in queryable_blocks:
                payload = json.dumps(block.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
                connection.execute(
                    "INSERT INTO blocks (block_id, tenant_id, document_id, source_sha256, ordinal, text, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(block.block_id), tenant, document, source_sha256, block.ordinal, block.text, payload),
                )
                connection.execute(
                    "INSERT INTO block_fts (block_id, text) VALUES (?, ?)",
                    (str(block.block_id), block.text),
                )
            for node in nodes:
                payload = json.dumps(node.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
                connection.execute(
                    "INSERT INTO nodes (node_id, tenant_id, document_id, source_sha256, ordinal, start_block_id, "
                    "title, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(node.node_id),
                        tenant,
                        document,
                        source_sha256,
                        node.ordinal,
                        str(node.start_block_id),
                        node.title,
                        payload,
                    ),
                )
                connection.execute(
                    "INSERT INTO node_fts (node_id, title) VALUES (?, ?)",
                    (str(node.node_id), node.title),
                )
            connection.execute(
                "INSERT INTO indexed_documents (tenant_id, document_id, source_sha256, config_hash, status, descriptor_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tenant,
                    document,
                    source_sha256,
                    config_hash,
                    "ready" if publish_ready else "indexing",
                    json.dumps(expected_descriptor, sort_keys=True, ensure_ascii=False),
                ),
            )
            connection.execute(
                "INSERT INTO index_build_events (tenant_id, document_id, source_sha256, config_hash) VALUES (?, ?, ?, ?)",
                (tenant, document, source_sha256, config_hash),
            )
            connection.commit()
        if publish_ready:
            self._remember_document_content(
                tenant,
                document,
                source_sha256,
                queryable_blocks,
                nodes,
            )

    def search_blocks(
        self,
        *,
        tenant_id: UUID,
        query: str,
        document_id: UUID | None = None,
        limit: int = 50,
    ) -> list[SearchRecord]:
        require_tenant_scope(tenant_id)
        match_query = _fts_query(query)
        if not match_query:
            return []
        if document_id is not None:
            return self._search_document_blocks(
                tenant_id=tenant_id,
                document_id=document_id,
                query=query,
                limit=limit,
            )
        clauses = ["blocks.tenant_id = ?"]
        parameters: list[object] = [match_query, str(tenant_id)]
        parameters.append(limit)
        sql = (
            "SELECT blocks.*, bm25(block_fts) AS fts_rank FROM block_fts "
            "JOIN blocks ON blocks.block_id = block_fts.block_id "
            "JOIN indexed_documents idx ON idx.tenant_id = blocks.tenant_id "
            "AND idx.document_id = blocks.document_id AND idx.source_sha256 = blocks.source_sha256 "
            "AND idx.status = 'ready' "
            "WHERE block_fts MATCH ? AND "
            + " AND ".join(clauses)
            + " ORDER BY fts_rank LIMIT ?"
        )
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            SearchRecord(
                tenant_id=UUID(row["tenant_id"]),
                document_id=UUID(row["document_id"]),
                source_sha256=row["source_sha256"],
                block_id=UUID(row["block_id"]),
                ordinal=int(row["ordinal"]),
                text=row["text"],
                raw_score=_normalize_bm25(float(row["fts_rank"])),
                channel="lexical",
            )
            for row in rows
        ]

    def _search_document_blocks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        query: str,
        limit: int,
    ) -> list[SearchRecord]:
        source_sha256, search_index = self._cached_document_search_index(
            str(tenant_id), str(document_id)
        )
        if source_sha256 is None:
            return []
        terms = _expanded_query_terms(
            query,
            search_index.block_postings,
            search_index.block_terms_by_trigram,
        )
        ranked = _rank_sparse_blocks(search_index, query, terms, limit=limit)
        return [
            SearchRecord(
                tenant_id=tenant_id,
                document_id=document_id,
                source_sha256=source_sha256,
                block_id=block.block_id,
                ordinal=block.ordinal,
                text=block.text,
                raw_score=score,
                channel="lexical",
            )
            for score, block in ranked
        ]

    def search_nodes(
        self,
        *,
        tenant_id: UUID,
        query: str,
        document_id: UUID | None = None,
        limit: int = 20,
    ) -> list[SearchRecord]:
        require_tenant_scope(tenant_id)
        match_query = _fts_query(query)
        if not match_query:
            return []
        if document_id is not None:
            return self._search_document_nodes(
                tenant_id=tenant_id,
                document_id=document_id,
                query=query,
                limit=limit,
            )
        clauses = ["nodes.tenant_id = ?"]
        parameters: list[object] = [match_query, str(tenant_id)]
        parameters.append(limit)
        sql = (
            "SELECT nodes.*, bm25(node_fts) AS fts_rank, blocks.text AS block_text "
            "FROM node_fts JOIN nodes ON nodes.node_id = node_fts.node_id "
            "JOIN blocks ON blocks.block_id = nodes.start_block_id "
            "JOIN indexed_documents idx ON idx.tenant_id = nodes.tenant_id "
            "AND idx.document_id = nodes.document_id AND idx.source_sha256 = nodes.source_sha256 "
            "AND idx.status = 'ready' "
            "WHERE node_fts MATCH ? AND "
            + " AND ".join(clauses)
            + " ORDER BY fts_rank LIMIT ?"
        )
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            SearchRecord(
                tenant_id=UUID(row["tenant_id"]),
                document_id=UUID(row["document_id"]),
                source_sha256=row["source_sha256"],
                block_id=UUID(row["start_block_id"]),
                node_id=UUID(row["node_id"]),
                ordinal=int(row["ordinal"]),
                text=row["block_text"],
                raw_score=_normalize_bm25(float(row["fts_rank"])),
                channel="tree",
            )
            for row in rows
        ]

    def _search_document_nodes(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        query: str,
        limit: int,
    ) -> list[SearchRecord]:
        source_sha256, search_index = self._cached_document_search_index(
            str(tenant_id), str(document_id)
        )
        if source_sha256 is None:
            return []
        blocks = search_index.blocks
        block_by_id = {block.block_id: block for block in blocks}
        terms = _expanded_query_terms(
            query,
            search_index.node_postings,
            search_index.node_terms_by_trigram,
        )
        ranked = [
            item
            for item in _rank_sparse_nodes(search_index, query, terms, limit=limit)
            if item[1].start_block_id in block_by_id
        ]
        return [
            SearchRecord(
                tenant_id=tenant_id,
                document_id=document_id,
                source_sha256=source_sha256,
                block_id=node.start_block_id,
                node_id=node.node_id,
                ordinal=node.ordinal,
                text=block_by_id[node.start_block_id].text,
                raw_score=score,
                channel="tree",
            )
            for score, node in ranked
        ]

    def get_blocks(self, *, tenant_id: UUID, document_id: UUID) -> list[ContentBlock]:
        require_tenant_scope(tenant_id)
        _source_sha256, blocks, _nodes = self._cached_document_content(
            str(tenant_id), str(document_id)
        )
        return list(blocks)

    def get_nodes(self, *, tenant_id: UUID, document_id: UUID) -> list[StructureNode]:
        require_tenant_scope(tenant_id)
        _source_sha256, _blocks, nodes = self._cached_document_content(
            str(tenant_id), str(document_id)
        )
        return list(nodes)

    def has_index(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config_hash: str | None = None,
    ) -> bool:
        require_tenant_scope(tenant_id)
        return self._cached_has_index(str(tenant_id), str(document_id), source_sha256, config_hash)

    def get_index_descriptor(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config_hash: str | None = None,
    ) -> dict[str, object] | None:
        require_tenant_scope(tenant_id)
        clauses = ["tenant_id = ?", "document_id = ?", "source_sha256 = ?", "status = 'ready'"]
        parameters: list[object] = [str(tenant_id), str(document_id), source_sha256]
        if config_hash is not None:
            clauses.append("config_hash = ?")
            parameters.append(config_hash)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT descriptor_json FROM indexed_documents WHERE " + " AND ".join(clauses),
                parameters,
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        return payload if isinstance(payload, dict) else None

    def mark_ready(self, *, tenant_id: UUID, document_id: UUID, source_sha256: str, config_hash: str) -> None:
        require_tenant_scope(tenant_id)
        with self._connection() as connection:
            updated = connection.execute(
                "UPDATE indexed_documents SET status = 'ready' "
                "WHERE tenant_id = ? AND document_id = ? AND source_sha256 = ? AND config_hash = ?",
                (str(tenant_id), str(document_id), source_sha256, config_hash),
            ).rowcount
            if updated != 1:
                raise ValueError("cannot publish a missing or mismatched retrieval index")
            connection.commit()
        self._clear_caches()

    def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> None:
        require_tenant_scope(tenant_id)
        tenant, document = str(tenant_id), str(document_id)
        for attempt in range(8):
            try:
                with self._connection() as connection:
                    block_ids = [
                        row[0]
                        for row in connection.execute(
                            "SELECT block_id FROM blocks WHERE tenant_id = ? AND document_id = ?", (tenant, document)
                        )
                    ]
                    node_ids = [
                        row[0]
                        for row in connection.execute(
                            "SELECT node_id FROM nodes WHERE tenant_id = ? AND document_id = ?", (tenant, document)
                        )
                    ]
                    connection.executemany("DELETE FROM block_fts WHERE block_id = ?", ((item,) for item in block_ids))
                    connection.executemany("DELETE FROM node_fts WHERE node_id = ?", ((item,) for item in node_ids))
                    connection.execute("DELETE FROM blocks WHERE tenant_id = ? AND document_id = ?", (tenant, document))
                    connection.execute("DELETE FROM nodes WHERE tenant_id = ? AND document_id = ?", (tenant, document))
                    connection.execute(
                        "DELETE FROM indexed_documents WHERE tenant_id = ? AND document_id = ?", (tenant, document)
                    )
                    connection.commit()
                self._clear_caches()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt == 7:
                    raise
                time.sleep(0.02 * (2**attempt))

    def index_build_count(self, *, tenant_id: UUID, document_id: UUID) -> int:
        require_tenant_scope(tenant_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM index_build_events WHERE tenant_id = ? AND document_id = ?",
                (str(tenant_id), str(document_id)),
            ).fetchone()
        return int(row[0]) if row else 0


def repository_for_settings(settings: Settings) -> RetrievalRepository:
    if settings.database_url.startswith("postgresql://") or settings.database_url.startswith("postgres://"):
        from proofcounsel_ingest.db.repositories import PostgresRetrievalRepository

        return PostgresRetrievalRepository(
            settings.database_url,
            user_id=settings.user_id,
            actor_role=settings.actor_role,
            matter_id=settings.matter_id,
        )
    if settings.database_url.startswith("sqlite:///") and settings.database_url != "sqlite:///./artifacts/proofcounsel.db":
        path = Path(settings.database_url.removeprefix("sqlite:///"))
    else:
        path = settings.artifact_root / "_indexes" / "retrieval.sqlite3"
    return SQLiteRetrievalRepository(path)


def close_repository(repository: RetrievalRepository) -> None:
    """Close adapters that own persistent client resources; no-op for per-call adapters."""
    close = getattr(repository, "close", None)
    if callable(close):
        close()


def _fts_query(query: str) -> str:
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in raw_terms(query)
    )


def _lexical_terms(query: str) -> list[str]:
    return normalized_terms(query)


def _build_sparse_document_index(
    blocks: Sequence[ContentBlock],
    nodes: Sequence[StructureNode],
) -> _SparseDocumentIndex:
    block_postings: dict[str, list[int]] = defaultdict(list)
    block_vocabulary: set[str] = set()
    for position, block in enumerate(blocks):
        for term in set(_lexical_terms(block.text)):
            block_postings[term].append(position)
        block_vocabulary.update(raw_terms(block.text))
    node_postings: dict[str, list[int]] = defaultdict(list)
    node_vocabulary: set[str] = set()
    for position, node in enumerate(nodes):
        for term in set(_lexical_terms(node.title)):
            node_postings[term].append(position)
        node_vocabulary.update(raw_terms(node.title))
    return _SparseDocumentIndex(
        blocks=tuple(blocks),
        nodes=tuple(nodes),
        block_postings={term: tuple(positions) for term, positions in block_postings.items()},
        node_postings={term: tuple(positions) for term, positions in node_postings.items()},
        block_terms_by_trigram=_terms_by_trigram(block_vocabulary),
        node_terms_by_trigram=_terms_by_trigram(node_vocabulary),
    )


def _terms_by_trigram(vocabulary: set[str]) -> dict[str, tuple[str, ...]]:
    terms_by_trigram: dict[str, list[str]] = defaultdict(list)
    for term in sorted(vocabulary):
        if len(term) < 5 or not term.isalpha():
            continue
        for trigram in _trigrams(term):
            terms_by_trigram[trigram].append(term)
    return {trigram: tuple(terms) for trigram, terms in terms_by_trigram.items()}


def _expanded_query_terms(
    query: str,
    postings: Mapping[str, Sequence[int]],
    terms_by_trigram: Mapping[str, Sequence[str]],
) -> list[str]:
    terms = normalized_terms(query)
    expanded = list(terms)
    for raw_term in raw_terms(query):
        normalized = normalized_terms(raw_term)
        if not normalized or normalized[0] in postings:
            continue
        corrected = _nearest_index_term(raw_term, terms_by_trigram)
        if corrected is None:
            continue
        corrected_normalized = normalized_terms(corrected)
        if corrected_normalized and corrected_normalized[0] in postings:
            expanded.append(corrected_normalized[0])
    return list(dict.fromkeys(expanded))


def _nearest_index_term(
    term: str,
    terms_by_trigram: Mapping[str, Sequence[str]],
) -> str | None:
    if len(term) < 5 or not term.isalpha():
        return None
    query_trigrams = _trigrams(term)
    if not query_trigrams:
        return None
    overlap: dict[str, int] = defaultdict(int)
    for trigram in query_trigrams:
        for candidate in terms_by_trigram.get(trigram, ()):
            overlap[candidate] += 1
    best: tuple[float, str] | None = None
    for candidate, shared in sorted(overlap.items(), key=lambda item: (-item[1], item[0]))[:128]:
        candidate_trigrams = _trigrams(candidate)
        union = len(query_trigrams | candidate_trigrams)
        jaccard = shared / union if union else 0.0
        if jaccard < 0.50:
            continue
        similarity = SequenceMatcher(None, term, candidate, autojunk=False).ratio()
        if similarity < 0.88:
            continue
        score = jaccard + similarity
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def _trigrams(term: str) -> set[str]:
    return {term[index : index + 3] for index in range(len(term) - 2)}


def _candidate_positions(
    postings: Mapping[str, Sequence[int]],
    terms: Sequence[str],
    *,
    maximum_candidates: int = 2048,
) -> set[int]:
    """Select the most discriminating query terms without truncating their postings."""
    available = [(term, postings[term]) for term in terms if term in postings]
    if not available:
        return set()
    available.sort(key=lambda item: (len(item[1]), item[0]))
    candidates: set[int] = set()
    for _term, positions in available:
        if candidates and len(positions) > maximum_candidates:
            continue
        if candidates and len(candidates) + len(positions) > maximum_candidates * 2:
            continue
        merged = candidates | set(positions)
        if candidates and len(merged) > maximum_candidates:
            continue
        candidates = merged
    return candidates


def _rank_sparse_blocks(
    index: _SparseDocumentIndex,
    query: str,
    terms: Sequence[str],
    *,
    limit: int,
) -> list[tuple[float, ContentBlock]]:
    positions = _candidate_positions(index.block_postings, terms)
    ranked = sorted(
        (
            (score, block)
            for position in positions
            if (block := index.blocks[position])
            and (
                score := _weighted_lexical_score(
                    block.text,
                    query,
                    terms,
                    index.block_postings,
                    len(index.blocks),
                )
            )
            > 0
        ),
        key=lambda item: (-item[0], item[1].ordinal),
    )
    return ranked[:limit]


def _rank_sparse_nodes(
    index: _SparseDocumentIndex,
    query: str,
    terms: Sequence[str],
    *,
    limit: int,
) -> list[tuple[float, StructureNode]]:
    positions = _candidate_positions(index.node_postings, terms, maximum_candidates=512)
    ranked = sorted(
        (
            (score, node)
            for position in positions
            if (node := index.nodes[position])
            and (
                score := _weighted_lexical_score(
                    node.title,
                    query,
                    terms,
                    index.node_postings,
                    len(index.nodes),
                )
            )
            > 0
        ),
        key=lambda item: (-item[0], item[1].ordinal),
    )
    return ranked[:limit]


def _weighted_lexical_score(
    text: str,
    query: str,
    terms: Sequence[str],
    postings: Mapping[str, Sequence[int]],
    population: int,
) -> float:
    if not terms or population <= 0:
        return 0.0
    text_terms = set(normalized_terms(text))
    weights = {
        term: math.log((population + 1) / (len(postings.get(term, ())) + 1)) + 1.0
        for term in terms
    }
    denominator = sum(weights.values())
    matched = sum(weight for term, weight in weights.items() if term in text_terms)
    query_phrase = " ".join(raw_terms(query))
    phrase_bonus = 0.5 if query_phrase and query_phrase in text.casefold() else 0.0
    return matched / denominator + phrase_bonus if denominator else 0.0


def _normalize_bm25(rank: float) -> float:
    relevance = max(0.0, -rank)
    return relevance / (1.0 + relevance)
