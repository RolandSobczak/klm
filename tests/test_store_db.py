"""Tests for the catalog database: pragmas, schema and migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from klm.store.db import (
    MIGRATIONS,
    SCHEMA_VERSION,
    connect,
    migrate,
    transaction,
    user_version,
)

NOW = "2026-08-09T12:00:00Z"


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "catalog.db")
    migrate(connection)
    yield connection
    connection.close()


def _seed_part(conn: sqlite3.Connection, klm_id: str = "01JB4K7Q", mpn: str = "STM32F103C8T6"):
    conn.execute("INSERT OR IGNORE INTO manufacturer (id, name, normalized) VALUES (1, 'ST', 'st')")
    conn.execute(
        "INSERT INTO part (klm_id, mpn, manufacturer_id, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?)",
        (klm_id, mpn, NOW, NOW),
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_connect_creates_the_file_and_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "catalog.db"
    connect(target).close()
    assert target.exists()


def test_connect_can_refuse_to_create(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        connect(tmp_path / "absent.db", create=False)


def test_pragmas_are_applied(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_rows_are_accessible_by_column_name(conn: sqlite3.Connection) -> None:
    _seed_part(conn)
    row = conn.execute("SELECT mpn FROM part").fetchone()
    assert row["mpn"] == "STM32F103C8T6"


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def test_migrate_brings_a_fresh_database_to_the_current_version(tmp_path: Path) -> None:
    connection = connect(tmp_path / "catalog.db")
    applied = migrate(connection)
    assert [m.version for m in applied] == [m.version for m in MIGRATIONS]
    assert user_version(connection) == SCHEMA_VERSION
    connection.close()


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    assert migrate(conn) == []
    assert user_version(conn) == SCHEMA_VERSION


def test_migration_versions_are_sequential_and_unique() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[0] == 1


def test_a_failing_migration_leaves_the_version_untouched(tmp_path: Path) -> None:
    from klm.store import db as db_module

    connection = connect(tmp_path / "catalog.db")
    migrate(connection)
    before = user_version(connection)

    broken = db_module.Migration(SCHEMA_VERSION + 1, "broken", "THIS IS NOT SQL;")
    original = db_module.MIGRATIONS[:]
    db_module.MIGRATIONS.append(broken)
    try:
        with pytest.raises(sqlite3.Error):
            migrate(connection)
        assert user_version(connection) == before
    finally:
        db_module.MIGRATIONS[:] = original
        connection.close()


def test_migrate_writes_a_backup_when_there_is_work_to_do(tmp_path: Path) -> None:
    from klm.store import db as db_module

    db_path = tmp_path / "catalog.db"
    connection = connect(db_path)
    migrate(connection, backup_path=db_path)  # v0 → v1; nothing to back up yet
    _seed_part(connection)

    extra = db_module.Migration(
        SCHEMA_VERSION + 1, "add note", "ALTER TABLE part ADD COLUMN x TEXT;"
    )
    original = db_module.MIGRATIONS[:]
    db_module.MIGRATIONS.append(extra)
    try:
        migrate(connection, backup_path=db_path)
        backup = db_path.with_name(f"catalog.db.pre-{SCHEMA_VERSION}.bak")
        assert backup.exists()
        # The backup holds the pre-migration schema.
        with sqlite3.connect(backup) as old:
            columns = {r[1] for r in old.execute("PRAGMA table_info(part)")}
        assert "x" not in columns
    finally:
        db_module.MIGRATIONS[:] = original
        connection.close()


# ---------------------------------------------------------------------------
# Schema behaviour
# ---------------------------------------------------------------------------


def test_expected_tables_exist(conn: sqlite3.Connection) -> None:
    names = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "part",
        "part_alias",
        "parameter",
        "offer",
        "asset",
        "manufacturer",
        "manufacturer_alias",
        "category",
        "project",
        "stock_item",
        "purchase_order",
        "purchase_line",
        "rotation_correction",
        "event_log",
    } <= names


def test_duplicate_mpn_for_one_manufacturer_is_rejected(conn: sqlite3.Connection) -> None:
    _seed_part(conn, "01AAA")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_part(conn, "01BBB")


def test_a_deprecated_part_frees_its_mpn_for_a_replacement(conn: sqlite3.Connection) -> None:
    """Old schematics keep resolving, but the identity can be reissued."""
    _seed_part(conn, "01AAA")
    conn.execute("UPDATE part SET status = 'deprecated' WHERE klm_id = '01AAA'")
    _seed_part(conn, "01BBB")
    assert conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("status", "nonsense"),
        ("lifecycle", "maybe"),
    ],
)
def test_check_constraints_reject_unknown_enum_values(
    conn: sqlite3.Connection, column: str, value: str
) -> None:
    _seed_part(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE part SET {column} = ?", (value,))


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO parameter (klm_id, name, source_kind) VALUES ('ghost', 'v', 'user')"
        )


def test_deleting_a_part_cascades_to_its_children(conn: sqlite3.Connection) -> None:
    _seed_part(conn)
    conn.execute(
        "INSERT INTO parameter (klm_id, name, value_num, unit, source_kind) "
        "VALUES ('01JB4K7Q', 'vdd_max', 3.6, 'V', 'datasheet')"
    )
    conn.execute("DELETE FROM part WHERE klm_id = '01JB4K7Q'")
    assert conn.execute("SELECT COUNT(*) FROM parameter").fetchone()[0] == 0


def test_conflicting_parameter_sources_coexist(conn: sqlite3.Connection) -> None:
    """A datasheet and a supplier may disagree; both are kept (docs/02 §4)."""
    _seed_part(conn)
    for source, value in [("datasheet", 3.6), ("supplier", 3.3)]:
        conn.execute(
            "INSERT INTO parameter (klm_id, name, value_num, unit, source_kind) "
            "VALUES ('01JB4K7Q', 'vdd_max', ?, 'V', ?)",
            (value, source),
        )
    rows = conn.execute(
        "SELECT source_kind, value_num FROM parameter ORDER BY source_kind"
    ).fetchall()
    assert [(r["source_kind"], r["value_num"]) for r in rows] == [
        ("datasheet", 3.6),
        ("supplier", 3.3),
    ]


def test_event_log_autoincrements(conn: sqlite3.Connection) -> None:
    for action in ("part.created", "part.approved"):
        conn.execute(
            "INSERT INTO event_log (at, actor, action, subject) VALUES (?, 'user', ?, '01JB4K7Q')",
            (NOW, action),
        )
    ids = [r["id"] for r in conn.execute("SELECT id FROM event_log ORDER BY id")]
    assert ids == [1, 2]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def test_transaction_commits_on_success(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        _seed_part(conn)
    assert conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 1


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), transaction(conn):
        _seed_part(conn)
        raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0
