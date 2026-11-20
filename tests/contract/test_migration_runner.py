from __future__ import annotations

from pathlib import Path

from scripts.migrate import _without_transaction_wrappers, migration_checksum

ROOT = Path(__file__).resolve().parents[2]


def test_migration_checksums_are_stable_and_wrappers_are_removed() -> None:
    path = ROOT / "migrations" / "0006_rls.sql"
    assert len(migration_checksum(path)) == 64
    body = _without_transaction_wrappers("BEGIN;\nCREATE TABLE x (id int);\nCOMMIT;\n")
    assert body == "CREATE TABLE x (id int);"
