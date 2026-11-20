"""PostgreSQL retrieval and lifecycle adapter.

The import of psycopg is intentionally lazy so the local SQLite profile keeps
working without the optional PostgreSQL dependency group.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

from proofcounsel_ingest.models import ContentBlock, StructureNode
from proofcounsel_ingest.retrieval.repository import SearchRecord


class PostgresConfigurationError(RuntimeError):
    """Raised when the PostgreSQL adapter cannot be constructed safely."""


class PostgresRetrievalRepository:
    """Tenant-scoped PostgreSQL full-text repository.

    The adapter owns a transaction per operation. Callers must establish the
    request scope through ``set_scope`` or pass the actor context at
    construction time; database RLS remains the final authorization boundary.
    """

    implementation = "postgresql-tsvector"

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[[str], Any] | None = None,
        user_id: UUID | None = None,
        actor_role: str = "service_worker",
        matter_id: UUID | None = None,
    ) -> None:
        self.dsn = dsn
        self.user_id = user_id
        self.actor_role = actor_role
        self.matter_id = matter_id
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - depends on optional group
                raise PostgresConfigurationError(
                    "PostgreSQL support requires the optional 'postgres' dependency group"
                ) from exc
            connect = psycopg.connect
        self._connect = connect

    @contextmanager
    def _connection(self, tenant_id: UUID) -> Iterator[Any]:
        connection = self._connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
                cursor.execute("SELECT set_config('app.actor_role', %s, true)", (self.actor_role,))
                if self.user_id is not None:
                    cursor.execute("SELECT set_config('app.user_id', %s, true)", (str(self.user_id),))
                if self.matter_id is not None:
                    cursor.execute("SELECT set_config('app.matter_id', %s, true)", (str(self.matter_id),))
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
        """Publish a retrieval index atomically for one immutable source version."""
        queryable_blocks = [block for block in blocks if block.metadata.get("queryable_evidence", True)]
        stored_matter_id = matter_id if matter_id is not None else self.matter_id
        expected_descriptor = dict(index_descriptor or {})
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO documents (
                        document_id, tenant_id, matter_id, source_sha256, original_filename,
                        detected_mime, byte_size, schema_version, extractor_version,
                        parser_config_hash, quality, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (document_id) DO UPDATE SET
                        status = CASE WHEN EXCLUDED.status = 'ready' THEN 'ready' ELSE documents.status END,
                        updated_at = now()
                    WHERE documents.tenant_id = EXCLUDED.tenant_id
                      AND documents.matter_id IS NOT DISTINCT FROM EXCLUDED.matter_id
                      AND documents.source_sha256 = EXCLUDED.source_sha256
                      AND documents.parser_config_hash = EXCLUDED.parser_config_hash
                    """,
                    (
                        str(document_id),
                        str(tenant_id),
                        str(stored_matter_id) if stored_matter_id is not None else None,
                        source_sha256,
                        original_filename,
                        detected_mime,
                        byte_size,
                        schema_version,
                        extractor_version,
                        config_hash,
                        json.dumps(dict(quality or {}), ensure_ascii=False),
                        "ready" if publish_ready else "indexing",
                    ),
                )
                cursor.execute(
                    "SELECT source_sha256, parser_config_hash, status, matter_id "
                    "FROM documents WHERE document_id = %s",
                    (str(document_id),),
                )
                row = cursor.fetchone()
                expected_matter_id = str(stored_matter_id) if stored_matter_id is not None else None
                row_matter_id = str(row[3]) if row is not None and row[3] is not None else None
                if (
                    row is None
                    or row[0] != source_sha256
                    or row[1] != config_hash
                    or row[2] == "deleted"
                    or row_matter_id != expected_matter_id
                ):
                    raise ValueError("document identity/config mismatch; immutable version was not mutated")

                cursor.execute(
                    "SELECT descriptor FROM index_versions "
                    "WHERE tenant_id = %s AND document_id = %s "
                    "AND source_sha256 = %s AND config_hash = %s LIMIT 1",
                    (str(tenant_id), str(document_id), source_sha256, config_hash),
                )
                existing_index = cursor.fetchone()
                if existing_index is not None:
                    stored_descriptor = existing_index[0]
                    if isinstance(stored_descriptor, str):
                        stored_descriptor = json.loads(stored_descriptor)
                    if dict(stored_descriptor or {}) != expected_descriptor:
                        raise ValueError("immutable index version has a different descriptor")

                cursor.execute(
                    """
                    SELECT 1 FROM index_versions
                    WHERE tenant_id = %s AND document_id = %s
                      AND source_sha256 = %s AND config_hash = %s
                    LIMIT 1
                    """,
                    (str(tenant_id), str(document_id), source_sha256, config_hash),
                )
                if cursor.fetchone() is not None:
                    if publish_ready:
                        cursor.execute(
                            "UPDATE documents SET status = 'ready', updated_at = now() "
                            "WHERE tenant_id = %s AND document_id = %s AND source_sha256 = %s "
                            "AND parser_config_hash = %s",
                            (str(tenant_id), str(document_id), source_sha256, config_hash),
                        )
                    return

                # Index rows are replaceable only inside the same immutable
                # source/config version and in one transaction.
                if not publish_ready:
                    cursor.execute(
                        "UPDATE documents SET status = 'indexing', updated_at = now() "
                        "WHERE tenant_id = %s AND document_id = %s AND source_sha256 = %s "
                        "AND parser_config_hash = %s",
                        (str(tenant_id), str(document_id), source_sha256, config_hash),
                    )
                cursor.execute("DELETE FROM structure_nodes WHERE tenant_id = %s AND document_id = %s", (str(tenant_id), str(document_id)))
                cursor.execute("DELETE FROM index_versions WHERE tenant_id = %s AND document_id = %s", (str(tenant_id), str(document_id)))
                cursor.execute("DELETE FROM content_blocks WHERE tenant_id = %s AND document_id = %s", (str(tenant_id), str(document_id)))
                for block in queryable_blocks:
                    cursor.execute(
                        """
                        INSERT INTO content_blocks (
                            block_id, tenant_id, document_id, source_sha256, ordinal,
                            block_type, text, language, confidence, locator,
                            parent_block_id, text_sha256, metadata, payload_json, queryable_evidence
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                                  %s, %s, %s::jsonb, %s::jsonb, %s)
                        """,
                        (
                            str(block.block_id),
                            str(tenant_id),
                            str(document_id),
                            source_sha256,
                            block.ordinal,
                            block.block_type.value,
                            block.text,
                            block.language,
                            block.confidence,
                            json.dumps(block.locator.model_dump(mode="json")),
                            str(block.parent_block_id) if block.parent_block_id else None,
                            block.text_sha256,
                            json.dumps(block.metadata, ensure_ascii=False),
                            json.dumps(block.model_dump(mode="json"), ensure_ascii=False),
                            True,
                        ),
                    )
                for node in nodes:
                    cursor.execute(
                        """
                        INSERT INTO structure_nodes (
                            node_id, tenant_id, document_id, parent_node_id, title,
                            kind, ordinal, start_block_id, end_block_id, method,
                            confidence, verified, metadata, payload_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                        """,
                        (
                            str(node.node_id),
                            str(tenant_id),
                            str(document_id),
                            str(node.parent_node_id) if node.parent_node_id else None,
                            node.title,
                            node.kind,
                            node.ordinal,
                            str(node.start_block_id),
                            str(node.end_block_id),
                            node.method,
                            node.confidence,
                            node.verified,
                            json.dumps(node.metadata, ensure_ascii=False),
                            json.dumps(node.model_dump(mode="json"), ensure_ascii=False),
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO index_versions (
                        index_id, tenant_id, document_id, source_sha256,
                        index_type, implementation, config_hash, descriptor
                    ) VALUES (%s, %s, %s, %s, 'lexical_and_tree', %s, %s, %s::jsonb)
                    ON CONFLICT (document_id, index_type, config_hash) DO NOTHING
                    """,
                    (
                        str(uuid4()),
                        str(tenant_id),
                        str(document_id),
                        source_sha256,
                        self.implementation,
                        config_hash,
                        json.dumps(expected_descriptor, ensure_ascii=False),
                    ),
                )

    def mark_ready(self, *, tenant_id: UUID, document_id: UUID, source_sha256: str, config_hash: str) -> None:
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE documents SET status = 'ready', updated_at = now() "
                "WHERE tenant_id = %s AND document_id = %s AND source_sha256 = %s "
                "AND parser_config_hash = %s AND status <> 'deleted'",
                (str(tenant_id), str(document_id), source_sha256, config_hash),
            )
            if cursor.rowcount != 1:
                raise ValueError("cannot publish a missing or mismatched retrieval index")

    def search_blocks(
        self,
        *,
        tenant_id: UUID,
        query: str,
        document_id: UUID | None = None,
        limit: int = 50,
    ) -> list[SearchRecord]:
        search_query = _postgres_search_query(query)
        if not search_query:
            return []
        clauses = [
            "b.tenant_id = %s",
            "d.status = 'ready'",
            "b.queryable_evidence",
            "to_tsvector('simple', b.text) @@ websearch_to_tsquery('simple', %s)",
        ]
        params: list[Any] = [str(tenant_id), search_query]
        if document_id is not None:
            clauses.append("b.document_id = %s")
            params.append(str(document_id))
        params.append(limit)
        sql = (
            "SELECT b.tenant_id, b.block_id, b.document_id, b.source_sha256, b.ordinal, b.text, b.locator, "
            "ts_rank_cd(to_tsvector('simple', b.text), websearch_to_tsquery('simple', %s)) AS score "
            "FROM content_blocks b JOIN documents d ON d.document_id = b.document_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY score DESC, b.ordinal LIMIT %s"
        )
        # The rank expression has its own query placeholder before the WHERE parameters.
        params.insert(0, search_query)
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        return [self._search_record(row, channel="lexical") for row in rows]

    def search_nodes(
        self,
        *,
        tenant_id: UUID,
        query: str,
        document_id: UUID | None = None,
        limit: int = 20,
    ) -> list[SearchRecord]:
        search_query = _postgres_search_query(query)
        if not search_query:
            return []
        clauses = [
            "n.tenant_id = %s",
            "d.status = 'ready'",
            "to_tsvector('simple', n.title) @@ websearch_to_tsquery('simple', %s)",
        ]
        params: list[Any] = [str(tenant_id), search_query]
        if document_id is not None:
            clauses.append("n.document_id = %s")
            params.append(str(document_id))
        params.append(limit)
        sql = (
            "SELECT n.tenant_id, n.node_id, n.document_id, d.source_sha256, n.start_block_id, n.ordinal, "
            "b.text, ts_rank_cd(to_tsvector('simple', n.title), websearch_to_tsquery('simple', %s)) AS score "
            "FROM structure_nodes n JOIN documents d ON d.document_id = n.document_id "
            "JOIN content_blocks b ON b.block_id = n.start_block_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY score DESC, n.ordinal LIMIT %s"
        )
        params.insert(0, search_query)
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        return [self._search_record(row, channel="tree", node=True) for row in rows]

    def get_blocks(self, *, tenant_id: UUID, document_id: UUID) -> list[ContentBlock]:
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload_json FROM content_blocks WHERE tenant_id = %s AND document_id = %s ORDER BY ordinal",
                    (str(tenant_id), str(document_id)),
                )
                rows = cursor.fetchall()
        return [_validate_payload(ContentBlock, row[0]) for row in rows]

    def get_nodes(self, *, tenant_id: UUID, document_id: UUID) -> list[StructureNode]:
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload_json FROM structure_nodes WHERE tenant_id = %s AND document_id = %s ORDER BY ordinal",
                    (str(tenant_id), str(document_id)),
                )
                rows = cursor.fetchall()
        return [_validate_payload(StructureNode, row[0]) for row in rows]

    def has_index(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config_hash: str | None = None,
    ) -> bool:
        clauses = [
            "i.tenant_id = %s",
            "i.document_id = %s",
            "i.source_sha256 = %s",
            "d.status = 'ready'",
        ]
        params: list[Any] = [str(tenant_id), str(document_id), source_sha256]
        if config_hash is not None:
            clauses.append("i.config_hash = %s")
            params.append(config_hash)
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM index_versions i JOIN documents d ON d.document_id = i.document_id WHERE "
                    + " AND ".join(clauses)
                    + " LIMIT 1",
                    params,
                )
                return cursor.fetchone() is not None

    def get_index_descriptor(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        source_sha256: str,
        config_hash: str | None = None,
    ) -> dict[str, object] | None:
        clauses = [
            "i.tenant_id = %s",
            "i.document_id = %s",
            "i.source_sha256 = %s",
            "d.status = 'ready'",
        ]
        params: list[Any] = [str(tenant_id), str(document_id), source_sha256]
        if config_hash is not None:
            clauses.append("i.config_hash = %s")
            params.append(config_hash)
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT i.descriptor FROM index_versions i "
                "JOIN documents d ON d.document_id = i.document_id WHERE "
                + " AND ".join(clauses)
                + " LIMIT 1",
                params,
            )
            row = cursor.fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return dict(payload) if isinstance(payload, dict) else None

    def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> None:
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE documents SET status = 'deleted', deleted_at = now(), updated_at = now() "
                    "WHERE tenant_id = %s AND document_id = %s",
                    (str(tenant_id), str(document_id)),
                )
                cursor.execute(
                    "DELETE FROM index_versions WHERE tenant_id = %s AND document_id = %s",
                    (str(tenant_id), str(document_id)),
                )

    def _search_record(self, row: Sequence[Any], *, channel: str, node: bool = False) -> SearchRecord:
        if node:
            tenant_index, block_index, document_index, source_index, ordinal_index, text_index, score_index = (
                0,
                4,
                2,
                3,
                5,
                6,
                7,
            )
        else:
            tenant_index, block_index, document_index, source_index, ordinal_index, text_index, score_index = (
                0,
                1,
                2,
                3,
                4,
                5,
                7,
            )
        return SearchRecord(
            tenant_id=UUID(str(row[tenant_index])),
            document_id=UUID(str(row[document_index])),
            source_sha256=str(row[source_index]),
            block_id=UUID(str(row[block_index])),
            node_id=UUID(str(row[1])) if node else None,
            ordinal=int(row[ordinal_index]),
            text=str(row[text_index]),
            raw_score=float(row[score_index]),
            channel=channel,
        )


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def json_payload(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _validate_payload(model: Any, payload: Any) -> Any:
    return model.model_validate(payload) if isinstance(payload, dict) else model.model_validate_json(payload)


def _postgres_search_query(query: str) -> str:
    terms = re.findall(r"[\w]{2,}", query.casefold(), flags=re.UNICODE)
    return " OR ".join(terms[:64])
