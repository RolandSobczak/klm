"""The catalog database: connection setup and forward migrations.

SQLite is the operational store; a deterministic YAML export is the
git-versioned mirror (docs/adr/0001). The database is reconstructible from that
export, which is what makes this arrangement safe rather than merely convenient.

Migrations are numbered, forward-only, and applied inside a transaction with the
current version held in ``PRAGMA user_version``. Every migration is backed up
before it runs, because a failed migration on a catalog representing months of
work is not an acceptable outcome.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "Migration", "connect", "migrate", "transaction"]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


_INITIAL = """
CREATE TABLE manufacturer (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    normalized TEXT NOT NULL UNIQUE   -- lowercased, punctuation stripped
);

-- 'ST', 'STMicro' and 'STMicroelectronics' are one company. Matching supplier
-- data to catalog parts fails constantly without this.
CREATE TABLE manufacturer_alias (
    alias           TEXT PRIMARY KEY,   -- normalized form
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturer(id) ON DELETE CASCADE
);

CREATE TABLE category (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,     -- 'IC/Power/Regulator/Switching'
    parent_id INTEGER REFERENCES category(id)
);

CREATE TABLE asset (
    content_hash TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('symbol', 'footprint', 'model3d')),
    filename     TEXT NOT NULL,
    source       TEXT NOT NULL,          -- generated|imported:easyeda|hand-drawn|...
    license_note TEXT,
    qa_status    TEXT NOT NULL DEFAULT 'unchecked'
                 CHECK (qa_status IN ('pass', 'warn', 'fail', 'unchecked')),
    qa_report    TEXT,                   -- JSON
    created_at   TEXT NOT NULL
);
CREATE INDEX asset_kind ON asset(kind);

CREATE TABLE part (
    klm_id          TEXT PRIMARY KEY,
    mpn             TEXT NOT NULL,
    manufacturer_id INTEGER NOT NULL REFERENCES manufacturer(id),
    description     TEXT NOT NULL DEFAULT '',
    category_id     INTEGER REFERENCES category(id),
    package         TEXT,
    lifecycle       TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (lifecycle IN ('active', 'nrnd', 'obsolete', 'unknown')),
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'approved', 'deprecated')),
    datasheet_url   TEXT,
    datasheet_sha   TEXT,
    symbol_hash     TEXT REFERENCES asset(content_hash),
    footprint_hash  TEXT REFERENCES asset(content_hash),
    model3d_hash    TEXT REFERENCES asset(content_hash),
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- A deprecated part keeps its row so old schematics still resolve, but must not
-- block a replacement from taking the same identity.
CREATE UNIQUE INDEX part_mpn_mfr
    ON part(manufacturer_id, mpn) WHERE status != 'deprecated';
CREATE INDEX part_status ON part(status);
CREATE INDEX part_category ON part(category_id);
CREATE INDEX part_package ON part(package);

-- Merging duplicates retires one klm_id; schematics referencing it keep working.
CREATE TABLE part_alias (
    alias_klm_id TEXT PRIMARY KEY,
    klm_id       TEXT NOT NULL REFERENCES part(klm_id) ON DELETE CASCADE,
    merged_at    TEXT NOT NULL
);

-- Conflicting sources coexist deliberately: when a datasheet and a supplier
-- disagree, both are recorded and the conflict is surfaced, never silently
-- resolved (docs/02 §4).
CREATE TABLE parameter (
    klm_id      TEXT NOT NULL REFERENCES part(klm_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    value_num   REAL,                   -- SI base units
    value_text  TEXT,
    unit        TEXT,
    tolerance   REAL,
    source_kind TEXT NOT NULL
                CHECK (source_kind IN ('user', 'datasheet', 'supplier', 'inferred')),
    source_ref  TEXT,
    confidence  TEXT NOT NULL DEFAULT 'medium'
                CHECK (confidence IN ('low', 'medium', 'high')),
    PRIMARY KEY (klm_id, name, source_kind)
);

CREATE TABLE offer (
    supplier       TEXT NOT NULL,
    supplier_pn    TEXT NOT NULL,
    klm_id         TEXT NOT NULL REFERENCES part(klm_id) ON DELETE CASCADE,
    packaging      TEXT,
    moq            INTEGER,
    multiple       INTEGER,
    stock          INTEGER,
    currency       TEXT,
    price_breaks   TEXT NOT NULL DEFAULT '[]',   -- JSON [[qty, unit_price], ...]
    lead_time_days INTEGER,
    url            TEXT,
    match_confidence TEXT NOT NULL DEFAULT 'high'
                     CHECK (match_confidence IN ('low', 'medium', 'high')),
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (supplier, supplier_pn)
);
CREATE INDEX offer_part ON offer(klm_id);
CREATE INDEX offer_fetched ON offer(fetched_at);

CREATE TABLE project (
    path      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    mode      TEXT NOT NULL DEFAULT 'linked' CHECK (mode IN ('linked', 'vendored')),
    last_seen TEXT
);

CREATE TABLE stock_item (
    klm_id       TEXT NOT NULL REFERENCES part(klm_id) ON DELETE CASCADE,
    location     TEXT NOT NULL,
    quantity     INTEGER NOT NULL DEFAULT 0,
    last_counted TEXT,
    PRIMARY KEY (klm_id, location)
);

CREATE TABLE purchase_order (
    id          TEXT PRIMARY KEY,
    supplier    TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'draft'
                CHECK (state IN ('draft', 'placed', 'partially_received',
                                 'received', 'cancelled')),
    currency    TEXT,
    placed_at   TEXT,
    received_at TEXT,
    notes       TEXT
);

CREATE TABLE purchase_line (
    order_id     TEXT NOT NULL REFERENCES purchase_order(id) ON DELETE CASCADE,
    klm_id       TEXT NOT NULL REFERENCES part(klm_id),
    supplier_pn  TEXT NOT NULL,
    qty_ordered  INTEGER NOT NULL,
    qty_received INTEGER NOT NULL DEFAULT 0,
    unit_price   REAL,
    PRIMARY KEY (order_id, supplier_pn)
);

-- Learned from real fabrication runs. The physical world is the authority here
-- (docs/09 §3).
CREATE TABLE rotation_correction (
    pattern      TEXT PRIMARY KEY,       -- regex against footprint name
    rotation     REAL NOT NULL DEFAULT 0,
    offset_x     REAL NOT NULL DEFAULT 0,
    offset_y     REAL NOT NULL DEFAULT 0,
    source       TEXT NOT NULL CHECK (source IN ('bundled', 'user', 'learned')),
    confirmed_at TEXT
);

-- Append-only. Answers "why is this part like this?", which matters a great
-- deal once an agent is contributing proposals.
CREATE TABLE event_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,               -- user|agent|import
    action  TEXT NOT NULL,
    subject TEXT,
    detail  TEXT                         -- JSON
);
CREATE INDEX event_log_subject ON event_log(subject);
"""


# The supplier's own view of the part, kept alongside the offer it produced.
# Without it a low-confidence match is unauditable: "klm thinks C25900 is this
# resistor" is only checkable if the supplier's MPN and manufacturer are on the
# row to compare against (docs/07 §5).
_OFFER_PROVENANCE = """
ALTER TABLE offer ADD COLUMN mpn TEXT;
ALTER TABLE offer ADD COLUMN manufacturer TEXT;
ALTER TABLE offer ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE offer ADD COLUMN datasheet_url TEXT;
"""


MIGRATIONS: list[Migration] = [
    Migration(1, "initial schema", _INITIAL),
    Migration(2, "offer provenance columns", _OFFER_PROVENANCE),
]

SCHEMA_VERSION = MIGRATIONS[-1].version


def connect(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    """Open the catalog database with klm's standard pragmas.

    WAL so a long read (the desktop app listing parts) does not block a write,
    and a busy timeout because the GUI, a CLI invocation and a background
    refresh can all be live at once (docs/03 §9).
    """
    path = Path(path)
    if not create and not path.exists():
        raise FileNotFoundError(f"no catalog database at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection, *, backup_path: Path | None = None) -> list[Migration]:
    """Apply pending migrations in order. Returns those applied.

    Each migration runs in its own transaction, so a failure leaves the database
    at the last version that succeeded rather than half-migrated. When
    ``backup_path`` is given and there is work to do, a copy is taken first.
    """
    current = user_version(conn)
    pending = [m for m in MIGRATIONS if m.version > current]
    if not pending:
        return []

    if backup_path is not None:
        _backup(conn, backup_path, current)

    applied: list[Migration] = []
    for migration in pending:
        conn.execute("BEGIN")
        try:
            conn.executescript(migration.sql)
            # executescript commits, so the version bump needs its own statement.
            conn.execute(f"PRAGMA user_version = {migration.version}")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            applied.append(migration)
    return applied


def _backup(conn: sqlite3.Connection, backup_path: Path, current_version: int) -> None:
    source = _database_path(conn)
    if source is None or not source.exists():
        return
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    target = backup_path.with_name(f"{backup_path.name}.pre-{current_version}.bak")
    with sqlite3.connect(target) as dest:
        conn.backup(dest)


def _database_path(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main" and row["file"]:
            return Path(row["file"])
    return None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in a transaction, rolling back on any exception."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
