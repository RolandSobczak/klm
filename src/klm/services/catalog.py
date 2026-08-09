"""Reading and writing parts in the catalog database."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from klm.model import Confidence, Lifecycle, Parameter, Part, PartStatus, SourceKind
from klm.store.db import transaction

__all__ = [
    "CatalogError",
    "count_parts",
    "delete_part",
    "find_by_mpn",
    "get_part",
    "list_parts",
    "save_part",
]


class CatalogError(Exception):
    """Raised when an operation would leave the catalog inconsistent."""


def now() -> str:
    """UTC timestamp in the single format klm stores everywhere."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------


def normalize_manufacturer(name: str) -> str:
    """Collapse a manufacturer name to a matching key.

    'ST', 'ST Micro' and 'STMicroelectronics' stay distinct here — this only
    removes punctuation and case, which is enough to stop 'Texas Instruments'
    and 'Texas  Instruments.' becoming two companies. Genuine aliasing is a
    separate, explicit table.
    """
    return "".join(c for c in name.lower() if c.isalnum())


def _manufacturer_id(conn: sqlite3.Connection, name: str) -> int:
    normalized = normalize_manufacturer(name)
    if not normalized:
        raise CatalogError("manufacturer name cannot be empty")

    row = conn.execute(
        "SELECT manufacturer_id FROM manufacturer_alias WHERE alias = ?", (normalized,)
    ).fetchone()
    if row:
        return int(row["manufacturer_id"])

    row = conn.execute(
        "SELECT id FROM manufacturer WHERE normalized = ?", (normalized,)
    ).fetchone()
    if row:
        return int(row["id"])

    cursor = conn.execute(
        "INSERT INTO manufacturer (name, normalized) VALUES (?, ?)", (name, normalized)
    )
    return int(cursor.lastrowid or 0)


def _category_id(conn: sqlite3.Connection, path: str | None) -> int | None:
    """Resolve a category path, creating each level so the tree stays walkable."""
    if not path:
        return None
    parent_id: int | None = None
    accumulated: list[str] = []
    for segment in (s.strip() for s in path.split("/") if s.strip()):
        accumulated.append(segment)
        full = "/".join(accumulated)
        row = conn.execute("SELECT id FROM category WHERE path = ?", (full,)).fetchone()
        if row:
            parent_id = int(row["id"])
            continue
        cursor = conn.execute(
            "INSERT INTO category (path, parent_id) VALUES (?, ?)", (full, parent_id)
        )
        parent_id = int(cursor.lastrowid or 0)
    return parent_id


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


def _ensure_asset_rows(conn: sqlite3.Connection, part: Part) -> None:
    """Register metadata rows for any asset the part references but that is unknown.

    Assets are content-addressed files on disk; the ``asset`` table is metadata
    about them (provenance, licence, QA result). The two can legitimately be out
    of step — importing a ``part.yaml`` names hashes whose metadata has not been
    recorded yet. Rather than dropping referential integrity, a placeholder row
    is created and the QA gate enriches it later.

    ``klm doctor`` reports placeholders whose file is missing from the store,
    which is the case that actually matters.
    """
    referenced = [
        (part.symbol_hash, "symbol"),
        (part.footprint_hash, "footprint"),
        (part.model3d_hash, "model3d"),
    ]
    timestamp = now()
    for content_hash, kind in referenced:
        if not content_hash:
            continue
        conn.execute(
            """
            INSERT INTO asset (content_hash, kind, filename, source, qa_status, created_at)
            VALUES (?, ?, '', 'unknown', 'unchecked', ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            (content_hash, kind, timestamp),
        )


def save_part(conn: sqlite3.Connection, part: Part) -> Part:
    """Insert or update ``part`` and its parameters.

    ``created_at`` is preserved across updates; ``updated_at`` is refreshed only
    when something actually changed, so a no-op re-import does not churn the
    exported files.
    """
    with transaction(conn):
        _ensure_asset_rows(conn, part)
        existing = conn.execute(
            "SELECT created_at FROM part WHERE klm_id = ?", (part.klm_id,)
        ).fetchone()

        created_at = part.created_at or (existing["created_at"] if existing else now())
        updated_at = part.updated_at or now()

        manufacturer_id = _manufacturer_id(conn, part.manufacturer)
        category_id = _category_id(conn, part.category)

        conn.execute(
            """
            INSERT INTO part (klm_id, mpn, manufacturer_id, description, category_id,
                              package, lifecycle, status, datasheet_url, datasheet_sha,
                              symbol_hash, footprint_hash, model3d_hash, notes,
                              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(klm_id) DO UPDATE SET
                mpn = excluded.mpn,
                manufacturer_id = excluded.manufacturer_id,
                description = excluded.description,
                category_id = excluded.category_id,
                package = excluded.package,
                lifecycle = excluded.lifecycle,
                status = excluded.status,
                datasheet_url = excluded.datasheet_url,
                datasheet_sha = excluded.datasheet_sha,
                symbol_hash = excluded.symbol_hash,
                footprint_hash = excluded.footprint_hash,
                model3d_hash = excluded.model3d_hash,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                part.klm_id,
                part.mpn,
                manufacturer_id,
                part.description,
                category_id,
                part.package,
                str(part.lifecycle),
                str(part.status),
                part.datasheet_url,
                part.datasheet_sha,
                part.symbol_hash,
                part.footprint_hash,
                part.model3d_hash,
                part.notes,
                created_at,
                updated_at,
            ),
        )

        # Parameters are replaced wholesale: they are a set, and diffing them
        # row by row would buy nothing at this scale.
        conn.execute("DELETE FROM parameter WHERE klm_id = ?", (part.klm_id,))
        conn.executemany(
            """
            INSERT INTO parameter (klm_id, name, value_num, value_text, unit,
                                   tolerance, source_kind, source_ref, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    part.klm_id,
                    p.name,
                    p.value_num,
                    p.value_text,
                    p.unit,
                    p.tolerance,
                    str(p.source),
                    p.source_ref,
                    str(p.confidence),
                )
                for p in part.sorted_parameters()
            ],
        )

    stored = get_part(conn, part.klm_id)
    if stored is None:  # pragma: no cover - the insert above guarantees this
        raise CatalogError(f"part vanished after save: {part.klm_id}")
    return stored


_SELECT = """
SELECT p.*, m.name AS manufacturer_name, c.path AS category_path
FROM part p
JOIN manufacturer m ON m.id = p.manufacturer_id
LEFT JOIN category c ON c.id = p.category_id
"""


def get_part(conn: sqlite3.Connection, klm_id: str) -> Part | None:
    """Fetch a part, following an alias if the id was retired by a merge."""
    row = conn.execute(f"{_SELECT} WHERE p.klm_id = ?", (klm_id,)).fetchone()
    if row is None:
        alias = conn.execute(
            "SELECT klm_id FROM part_alias WHERE alias_klm_id = ?", (klm_id,)
        ).fetchone()
        if alias is None:
            return None
        row = conn.execute(f"{_SELECT} WHERE p.klm_id = ?", (alias["klm_id"],)).fetchone()
        if row is None:
            return None
    return _row_to_part(conn, row)


def find_by_mpn(conn: sqlite3.Connection, manufacturer: str, mpn: str) -> Part | None:
    """The live part with this manufacturer and MPN, if there is one.

    `(manufacturer, mpn)` is unique among non-deprecated parts, so this is the
    identity check for a part that arrives without a `KLM_ID` — importing the
    same symbol from a second project's library must find the first one rather
    than colliding with it.
    """
    row = conn.execute(
        f"{_SELECT} WHERE p.mpn = ? AND m.normalized = ? AND p.status != 'deprecated'",
        (mpn, normalize_manufacturer(manufacturer)),
    ).fetchone()
    return _row_to_part(conn, row) if row is not None else None


def list_parts(
    conn: sqlite3.Connection,
    *,
    status: PartStatus | None = None,
    category: str | None = None,
) -> list[Part]:
    """All matching parts, ordered by id so callers get a stable sequence."""
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("p.status = ?")
        params.append(str(status))
    if category is not None:
        clauses.append("(c.path = ? OR c.path LIKE ?)")
        params.extend([category, f"{category}/%"])

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"{_SELECT}{where} ORDER BY p.klm_id", params).fetchall()
    return [_row_to_part(conn, row) for row in rows]


def search_parts(
    conn: sqlite3.Connection,
    query: str | None = None,
    *,
    status: PartStatus | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> list[Part]:
    """:func:`list_parts`, narrowed by a substring of MPN, maker or description.

    Substring and nothing cleverer. The catalog is small enough that ranked
    relevance would be a guess dressed as an answer, and a search that returns
    *everything matching* lets a caller — a person, or the research agent —
    see for itself what is there.
    """
    found = list_parts(conn, status=status, category=category)
    if query:
        needle = query.strip().lower()
        found = [
            part
            for part in found
            if needle in part.mpn.lower()
            or needle in part.manufacturer.lower()
            or needle in (part.description or "").lower()
        ]
    return found[:limit] if limit is not None else found


def count_parts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM part GROUP BY status").fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def delete_part(conn: sqlite3.Connection, klm_id: str) -> bool:
    """Hard-delete a part.

    Almost always the wrong operation — deprecating keeps existing schematics
    resolvable. Provided for discarding a draft that was never used.
    """
    with transaction(conn):
        cursor = conn.execute("DELETE FROM part WHERE klm_id = ?", (klm_id,))
    return cursor.rowcount > 0


def _row_to_part(conn: sqlite3.Connection, row: sqlite3.Row) -> Part:
    parameters = [
        Parameter(
            name=p["name"],
            source=SourceKind(p["source_kind"]),
            value_num=p["value_num"],
            value_text=p["value_text"],
            unit=p["unit"],
            tolerance=p["tolerance"],
            source_ref=p["source_ref"],
            confidence=Confidence(p["confidence"]),
        )
        for p in conn.execute(
            "SELECT * FROM parameter WHERE klm_id = ? ORDER BY name, source_kind",
            (row["klm_id"],),
        )
    ]
    return Part(
        klm_id=row["klm_id"],
        mpn=row["mpn"],
        manufacturer=row["manufacturer_name"],
        description=row["description"] or "",
        category=row["category_path"],
        package=row["package"],
        lifecycle=Lifecycle(row["lifecycle"]),
        status=PartStatus(row["status"]),
        datasheet_url=row["datasheet_url"],
        datasheet_sha=row["datasheet_sha"],
        symbol_hash=row["symbol_hash"],
        footprint_hash=row["footprint_hash"],
        model3d_hash=row["model3d_hash"],
        notes=row["notes"],
        parameters=parameters,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
