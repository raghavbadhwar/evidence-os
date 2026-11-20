"""Apply additive PostgreSQL migrations with checksum drift detection."""

from __future__ import annotations

import argparse
import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


def migration_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migrations(
    dsn: str,
    directory: Path,
    *,
    connect: Callable[[str], Any] | None = None,
) -> list[str]:
    if connect is None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("migrations require the optional 'postgres' dependency group") from exc
        connect = psycopg.connect
    paths = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    connection = connect(dsn)
    applied: list[str] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    filename text NOT NULL,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        connection.commit()
        for path in paths:
            version = path.name.split("_", 1)[0]
            checksum = migration_checksum(path)
            with connection.cursor() as cursor:
                cursor.execute("SELECT filename, checksum FROM schema_migrations WHERE version = %s", (version,))
                row = cursor.fetchone()
            # psycopg starts an implicit transaction for the lookup. Commit it
            # before opening the migration transaction so the latter is not a
            # savepoint that disappears when the connection closes.
            connection.commit()
            if row is not None:
                if row[0] != path.name or row[1] != checksum:
                    raise RuntimeError(f"migration drift detected for {path.name}")
                continue
            body = _without_transaction_wrappers(path.read_text(encoding="utf-8"))
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(body)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) VALUES (%s, %s, %s)",
                    (version, path.name, checksum),
                )
            connection.commit()
            applied.append(path.name)
    finally:
        connection.close()
    return applied


def _without_transaction_wrappers(sql: str) -> str:
    sql = re.sub(r"^\s*BEGIN\s*;", "", sql, count=1, flags=re.IGNORECASE)
    sql = re.sub(r";\s*COMMIT\s*;?\s*$", ";", sql, count=1, flags=re.IGNORECASE)
    return sql.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--directory", type=Path, default=Path("migrations"))
    args = parser.parse_args()
    for filename in apply_migrations(args.dsn, args.directory):
        print(filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
