"""Document revocation, deletion, legal hold, and local artifact cleanup."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from proofcounsel_ingest.outbox import OutboxEvent, OutboxPort
from proofcounsel_ingest.retrieval.repository import RetrievalRepository


class LifecyclePolicy(BaseModel):
    legal_hold: bool = False
    retain_audit_record: bool = True
    allow_physical_delete: bool = True


class RetentionPolicy(BaseModel):
    retention_days: int = Field(gt=0)
    legal_hold_default: bool = False
    retain_audit_record: bool = True
    allow_physical_delete: bool = True


class DeletionResult(BaseModel):
    document_id: UUID
    query_access_revoked: bool
    artifacts_deleted: bool
    blocked_by_legal_hold: bool
    retained_audit_record: bool
    deleted_paths: list[str] = Field(default_factory=list)
    cleanup_enqueued: bool = False


class ExportResult(BaseModel):
    document_id: UUID
    destination: Path
    file_count: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LifecycleRepository(Protocol):
    def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> None: ...


class PostgresLifecycleRepository:
    """Transactional tombstone, index revocation, cleanup outbox, and audit adapter."""

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
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("PostgreSQL lifecycle requires the optional 'postgres' dependency group") from exc
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

    def revoke_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        artifact_relative_path: str | None = None,
        policy: LifecyclePolicy | None = None,
    ) -> DeletionResult:
        active_policy = policy or LifecyclePolicy()
        if artifact_relative_path is not None:
            artifact_relative_path = _validate_relative_cleanup_path(artifact_relative_path)
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT legal_hold, status FROM documents "
                "WHERE tenant_id = %s AND document_id = %s FOR UPDATE",
                (str(tenant_id), str(document_id)),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("document does not exist in the requested tenant")
            database_legal_hold = bool(row[0])
            already_deleted = str(row[1]) == "deleted"
            blocked_by_hold = database_legal_hold or active_policy.legal_hold
            physical_cleanup_allowed = (
                not blocked_by_hold and active_policy.allow_physical_delete and artifact_relative_path is not None
            )
            if not already_deleted:
                cursor.execute(
                    "UPDATE documents SET status = 'deleted', deleted_at = now(), updated_at = now() "
                    "WHERE tenant_id = %s AND document_id = %s",
                    (str(tenant_id), str(document_id)),
                )
            cursor.execute(
                "DELETE FROM index_versions WHERE tenant_id = %s AND document_id = %s",
                (str(tenant_id), str(document_id)),
            )
            cleanup_enqueued = False
            if physical_cleanup_allowed:
                idempotency_key = f"delete-artifact:{tenant_id}:{document_id}"
                cursor.execute(
                    """
                    INSERT INTO outbox_events (
                        event_id, tenant_id, aggregate_type, aggregate_id, event_type,
                        payload, idempotency_key, status, attempts, available_at
                    ) VALUES (%s, %s, 'document', %s, 'delete_artifact', %s::jsonb, %s, 'pending', 0, now())
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    """,
                    (
                        str(uuid4()),
                        str(tenant_id),
                        str(document_id),
                        json.dumps({"artifact_relative_path": artifact_relative_path}, ensure_ascii=False),
                        idempotency_key,
                    ),
                )
                cursor.execute(
                    "SELECT 1 FROM outbox_events WHERE tenant_id = %s AND idempotency_key = %s",
                    (str(tenant_id), idempotency_key),
                )
                cleanup_enqueued = cursor.fetchone() is not None
            if active_policy.retain_audit_record:
                self._append_audit_event(
                    cursor,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    actor_id=self.user_id,
                    action_type="document_deleted",
                    payload={
                        "blocked_by_legal_hold": blocked_by_hold,
                        "cleanup_enqueued": cleanup_enqueued,
                        "query_access_revoked": True,
                    },
                )
        return DeletionResult(
            document_id=document_id,
            query_access_revoked=True,
            artifacts_deleted=(
                artifact_relative_path is None
                and not blocked_by_hold
                and active_policy.allow_physical_delete
            ),
            blocked_by_legal_hold=blocked_by_hold,
            retained_audit_record=active_policy.retain_audit_record,
            cleanup_enqueued=cleanup_enqueued,
        )

    def expired_document_ids(
        self,
        *,
        tenant_id: UUID,
        default_policy: RetentionPolicy,
        now: datetime | None = None,
    ) -> list[UUID]:
        cutoff = now or datetime.now(UTC)
        with self._connection(tenant_id) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.document_id
                FROM documents d
                LEFT JOIN retention_policies rp
                  ON rp.tenant_id = d.tenant_id
                 AND rp.matter_id IS NOT DISTINCT FROM d.matter_id
                WHERE d.tenant_id = %s
                  AND d.status <> 'deleted'
                  AND NOT d.legal_hold
                  AND NOT COALESCE(rp.legal_hold_default, %s)
                  AND d.created_at <= %s - (
                      COALESCE(rp.retention_days, %s) * interval '1 day'
                  )
                ORDER BY d.created_at, d.document_id
                """,
                (
                    str(tenant_id),
                    default_policy.legal_hold_default,
                    cutoff,
                    default_policy.retention_days,
                ),
            )
            rows = cursor.fetchall()
        return [UUID(str(row[0])) for row in rows]

    def expire_documents(
        self,
        *,
        tenant_id: UUID,
        default_policy: RetentionPolicy,
        artifact_paths: Mapping[UUID, str | None],
        now: datetime | None = None,
    ) -> list[DeletionResult]:
        document_ids = self.expired_document_ids(
            tenant_id=tenant_id,
            default_policy=default_policy,
            now=now,
        )
        missing_paths = [document_id for document_id in document_ids if document_id not in artifact_paths]
        if missing_paths:
            raise ValueError("retention expiry requires an artifact key for every expired document")
        lifecycle_policy = LifecyclePolicy(
            retain_audit_record=default_policy.retain_audit_record,
            allow_physical_delete=default_policy.allow_physical_delete,
        )
        return [
            self.revoke_and_enqueue(
                tenant_id=tenant_id,
                document_id=document_id,
                artifact_relative_path=artifact_paths[document_id],
                policy=lifecycle_policy,
            )
            for document_id in document_ids
        ]

    def _append_audit_event(
        self,
        cursor: Any,
        *,
        tenant_id: UUID,
        document_id: UUID,
        actor_id: UUID | None,
        action_type: str,
        payload: dict[str, object],
    ) -> None:
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        cursor.execute(
            "SELECT event_hash FROM audit_events WHERE tenant_id = %s "
            "ORDER BY created_at DESC, audit_id DESC LIMIT 1",
            (str(tenant_id),),
        )
        previous_hash = cursor.fetchone()
        previous = str(previous_hash[0]) if previous_hash else ""
        event_hash = hashlib.sha256(f"{previous}:{payload_sha256}:{document_id}".encode()).hexdigest()
        cursor.execute(
            """
            INSERT INTO audit_events (
                audit_id, tenant_id, actor_id, actor_role, action_type,
                aggregate_type, aggregate_id, payload_sha256, previous_hash, event_hash
            ) VALUES (%s, %s, %s, %s, %s, 'document', %s, %s, %s, %s)
            """,
            (
                str(uuid4()),
                str(tenant_id),
                str(actor_id) if actor_id is not None else None,
                self.actor_role,
                action_type,
                str(document_id),
                payload_sha256,
                previous_hash[0] if previous_hash else None,
                event_hash,
            ),
        )


def revoke_and_delete_local(
    *,
    repository: RetrievalRepository,
    tenant_id: UUID,
    document_id: UUID,
    artifact_path: Path | None,
    artifact_root: Path,
    policy: LifecyclePolicy | None = None,
    outbox: OutboxPort | None = None,
) -> DeletionResult:
    active_policy = policy or LifecyclePolicy()
    resolved_artifact: Path | None = None
    if artifact_path is not None:
        resolved_artifact = _validated_artifact_path(artifact_path, artifact_root)
    repository.delete_document(tenant_id=tenant_id, document_id=document_id)
    if active_policy.legal_hold or not active_policy.allow_physical_delete:
        return DeletionResult(
            document_id=document_id,
            query_access_revoked=True,
            artifacts_deleted=False,
            blocked_by_legal_hold=active_policy.legal_hold,
            retained_audit_record=active_policy.retain_audit_record,
        )

    if resolved_artifact is not None and outbox is not None:
        root = artifact_root.expanduser().resolve()
        event = outbox.enqueue(
            OutboxEvent(
                tenant_id=tenant_id,
                aggregate_type="document",
                aggregate_id=document_id,
                event_type="delete_artifact",
                idempotency_key=f"delete-artifact:{tenant_id}:{document_id}",
                payload={"artifact_relative_path": resolved_artifact.relative_to(root).as_posix()},
            )
        )
        return DeletionResult(
            document_id=document_id,
            query_access_revoked=True,
            artifacts_deleted=False,
            blocked_by_legal_hold=False,
            retained_audit_record=active_policy.retain_audit_record,
            cleanup_enqueued=event.status in {"pending", "running"},
        )

    deleted_paths: list[str] = []
    if resolved_artifact is not None and resolved_artifact.exists():
        shutil.rmtree(resolved_artifact)
        if resolved_artifact.exists():
            raise RuntimeError("artifact deletion could not be verified")
        deleted_paths.append(str(resolved_artifact))
    return DeletionResult(
        document_id=document_id,
        query_access_revoked=True,
        artifacts_deleted=True,
        blocked_by_legal_hold=False,
        retained_audit_record=active_policy.retain_audit_record,
        deleted_paths=deleted_paths,
    )


def process_artifact_cleanup(event: OutboxEvent, *, artifact_root: Path) -> None:
    """Execute only the bounded cleanup action represented by an outbox event."""
    if event.event_type != "delete_artifact":
        raise ValueError(f"unsupported lifecycle event: {event.event_type}")
    relative = event.payload.get("artifact_relative_path")
    if not isinstance(relative, str):
        raise ValueError("cleanup event is missing artifact_relative_path")
    path = _validated_artifact_path(artifact_root / relative, artifact_root)
    if path.exists():
        shutil.rmtree(path)
    if path.exists():
        raise RuntimeError("artifact deletion could not be verified")


def export_local_artifact(
    *,
    document_id: UUID,
    artifact_path: Path,
    artifact_root: Path,
    destination: Path,
) -> ExportResult:
    """Export immutable derived evidence with a deterministic hash manifest."""
    source = _validated_artifact_path(artifact_path, artifact_root)
    if not source.is_dir():
        raise ValueError("artifact path is not a directory")
    target = destination.expanduser().resolve()
    if target == source or source in target.parents:
        raise ValueError("export destination cannot be inside the source artifact")
    target.parent.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, str | int]] = []
    files = sorted(path for path in source.rglob("*") if path.is_file())
    for path in files:
        if path.is_symlink():
            raise ValueError("refusing to export symlinked artifact content")
        payload = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(source).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload),
            }
        )
    manifest = {
        "schema_version": "1.0",
        "document_id": str(document_id),
        "exported_at": datetime.now(UTC).isoformat(),
        "files": entries,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    temporary = target.with_name(f".{target.name}.staging")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                relative_path = str(entry["path"])
                archive.writestr(relative_path, (source / relative_path).read_bytes())
            archive.writestr("export_manifest.json", manifest_bytes)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return ExportResult(
        document_id=document_id,
        destination=target,
        file_count=len(entries),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def _validated_artifact_path(artifact_path: Path, artifact_root: Path) -> Path:
    resolved = artifact_path.expanduser().resolve()
    documents_root = (artifact_root.expanduser().resolve() / "documents").resolve()
    if resolved == documents_root or documents_root not in resolved.parents:
        raise ValueError("refusing to delete an invalid artifact path")
    return resolved


def _validate_relative_cleanup_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("cleanup path must be a relative non-traversing path")
    return normalized
