from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
except ImportError:  # pragma: no cover - exercised only without the optional dependency
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]
    conninfo_to_dict = None  # type: ignore[assignment]
    make_conninfo = None  # type: ignore[assignment]

from scripts.migrate import apply_migrations

ROOT = Path(__file__).resolve().parents[2]
ADMIN_DSN = os.environ.get("PROOFCOUNSEL_TEST_POSTGRES_ADMIN_DSN")
pytestmark = pytest.mark.integration


def test_postgres_rls_matrix() -> None:
    if not ADMIN_DSN:
        pytest.skip("set PROOFCOUNSEL_TEST_POSTGRES_ADMIN_DSN for live RLS coverage")
    if psycopg is None:
        pytest.skip("install the optional 'postgres' dependency group for live RLS coverage")

    tenant_one = uuid4()
    tenant_two = uuid4()
    matter_one = uuid4()
    matter_two = uuid4()
    user_id = uuid4()
    role_name = f"pc_rls_{uuid4().hex}"
    role_password = uuid4().hex
    documents = {
        "matter_one": uuid4(),
        "matter_two": uuid4(),
        "deleted": uuid4(),
        "revoked": uuid4(),
        "other_tenant": uuid4(),
    }
    app_dsn = _create_test_role_and_seed(
        ADMIN_DSN,
        role_name=role_name,
        role_password=role_password,
        tenant_one=tenant_one,
        tenant_two=tenant_two,
        matter_one=matter_one,
        matter_two=matter_two,
        user_id=user_id,
        documents=documents,
    )
    try:
        assert _visible_documents(app_dsn, tenant_one, user_id, matter_one, "user") == {
            documents["matter_one"]
        }
        assert _visible_documents(app_dsn, tenant_one, user_id, matter_two, "user") == {
            documents["matter_two"]
        }
        assert _visible_documents(app_dsn, tenant_two, user_id, matter_one, "user") == {
            documents["other_tenant"]
        }
        assert _visible_documents(app_dsn, tenant_one, user_id, matter_one, "service_worker") == {
            documents[name] for name in documents if name != "other_tenant"
        }
        assert _visible_count(
            app_dsn,
            tenant_one,
            user_id,
            matter_one,
            "user",
            "content_blocks",
        ) == 0
        assert _visible_count(
            app_dsn,
            tenant_one,
            user_id,
            matter_one,
            "user",
            "retention_policies",
        ) == 1
        _assert_user_write_is_denied(app_dsn, tenant_one, user_id, matter_one, documents["matter_one"])
    finally:
        _drop_test_role_and_data(
            ADMIN_DSN,
            role_name=role_name,
            tenant_one=tenant_one,
            tenant_two=tenant_two,
        )


def _create_test_role_and_seed(
    admin_dsn: str,
    *,
    role_name: str,
    role_password: str,
    tenant_one: UUID,
    tenant_two: UUID,
    matter_one: UUID,
    matter_two: UUID,
    user_id: UUID,
    documents: dict[str, UUID],
) -> str:
    apply_migrations(admin_dsn, ROOT / "migrations")
    with psycopg.connect(admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role_name), sql.Literal(role_password)
            )
        )
        for statement in (
            "GRANT USAGE ON SCHEMA public, app TO {role}",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
        ):
            cursor.execute(sql.SQL(statement).format(role=sql.Identifier(role_name)))

        _insert_document(cursor, documents["matter_one"], tenant_one, matter_one, "ready")
        _insert_document(cursor, documents["matter_two"], tenant_one, matter_two, "ready")
        _insert_document(cursor, documents["deleted"], tenant_one, matter_one, "deleted")
        _insert_document(cursor, documents["revoked"], tenant_one, matter_one, "ready")
        _insert_document(cursor, documents["other_tenant"], tenant_two, matter_one, "ready")
        cursor.execute(
            """
            INSERT INTO content_blocks (
                block_id, tenant_id, document_id, source_sha256, ordinal,
                block_type, text, language, confidence, locator, text_sha256
            ) VALUES (%s, %s, %s, %s, 0, 'paragraph', 'deleted evidence', 'en', 1.0, '{}'::jsonb, %s)
            """,
            (
                str(uuid4()),
                str(tenant_one),
                str(documents["deleted"]),
                _hash("deleted evidence"),
                _hash("deleted evidence"),
            ),
        )
        for document_id, matter_id, access_role, revoked_at in (
            (documents["matter_one"], matter_one, "owner", None),
            (documents["matter_two"], matter_two, "reviewer", None),
            (documents["deleted"], matter_one, "reader", None),
            (documents["revoked"], matter_one, "reader", "now()"),
            (documents["other_tenant"], matter_one, "owner", None),
        ):
            if revoked_at is None:
                cursor.execute(
                    "INSERT INTO document_acl "
                    "(acl_id, tenant_id, document_id, user_id, matter_id, access_role) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (str(uuid4()), str(tenant_one if document_id != documents["other_tenant"] else tenant_two),
                     str(document_id), str(user_id), str(matter_id), access_role),
                )
            else:
                cursor.execute(
                    "INSERT INTO document_acl "
                    "(acl_id, tenant_id, document_id, user_id, matter_id, access_role, revoked_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, now())",
                    (str(uuid4()), str(tenant_one), str(document_id), str(user_id), str(matter_id), access_role),
                )
        cursor.execute(
            "INSERT INTO retention_policies "
            "(policy_id, tenant_id, matter_id, retention_days, legal_hold_default) "
            "VALUES (%s, %s, %s, 365, true)",
            (str(uuid4()), str(tenant_one), str(matter_one)),
        )
        cursor.execute(
            "INSERT INTO retention_policies "
            "(policy_id, tenant_id, matter_id, retention_days, legal_hold_default) "
            "VALUES (%s, %s, %s, 365, false)",
            (str(uuid4()), str(tenant_one), str(matter_two)),
        )
    app_info = conninfo_to_dict(admin_dsn)
    app_info["user"] = role_name
    app_info["password"] = role_password
    return make_conninfo(**app_info)


def _insert_document(cursor, document_id: UUID, tenant_id: UUID, matter_id: UUID, status: str) -> None:
    source_hash = _hash(str(document_id))
    cursor.execute(
        """
        INSERT INTO documents (
            document_id, tenant_id, matter_id, source_sha256, original_filename,
            detected_mime, byte_size, schema_version, extractor_version,
            parser_config_hash, status
        ) VALUES (%s, %s, %s, %s, %s, 'text/plain', 1, '1.0', 'test', %s, %s)
        """,
        (str(document_id), str(tenant_id), str(matter_id), source_hash, f"{document_id}.txt", _hash("test"), status),
    )


def _visible_documents(
    app_dsn: str,
    tenant_id: UUID,
    user_id: UUID,
    matter_id: UUID,
    actor_role: str,
) -> set[UUID]:
    with psycopg.connect(app_dsn) as connection, connection.cursor() as cursor:
        _set_scope(cursor, tenant_id, user_id, matter_id, actor_role)
        cursor.execute("SELECT document_id FROM documents ORDER BY document_id")
        return {UUID(str(row[0])) for row in cursor.fetchall()}


def _visible_count(
    app_dsn: str,
    tenant_id: UUID,
    user_id: UUID,
    matter_id: UUID,
    actor_role: str,
    table: str,
) -> int:
    with psycopg.connect(app_dsn) as connection, connection.cursor() as cursor:
        _set_scope(cursor, tenant_id, user_id, matter_id, actor_role)
        cursor.execute(sql.SQL("SELECT count(*) FROM {} ").format(sql.Identifier(table)))
        return int(cursor.fetchone()[0])


def _assert_user_write_is_denied(
    app_dsn: str,
    tenant_id: UUID,
    user_id: UUID,
    matter_id: UUID,
    document_id: UUID,
) -> None:
    with psycopg.connect(app_dsn) as connection, connection.cursor() as cursor:
        _set_scope(cursor, tenant_id, user_id, matter_id, "user")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cursor.execute(
                "UPDATE documents SET status = 'deleted' WHERE document_id = %s",
                (str(document_id),),
            )


def _set_scope(cursor, tenant_id: UUID, user_id: UUID, matter_id: UUID, actor_role: str) -> None:
    cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
    cursor.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
    cursor.execute("SELECT set_config('app.matter_id', %s, true)", (str(matter_id),))
    cursor.execute("SELECT set_config('app.actor_role', %s, true)", (actor_role,))


def _drop_test_role_and_data(admin_dsn: str, *, role_name: str, tenant_one: UUID, tenant_two: UUID) -> None:
    with psycopg.connect(admin_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM retention_policies WHERE tenant_id IN (%s, %s)", (str(tenant_one), str(tenant_two)))
        cursor.execute("DELETE FROM documents WHERE tenant_id IN (%s, %s)", (str(tenant_one), str(tenant_two)))
        cursor.execute(sql.SQL("DROP OWNED BY {} ").format(sql.Identifier(role_name)))
        cursor.execute(sql.SQL("DROP ROLE {} ").format(sql.Identifier(role_name)))
        connection.commit()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
