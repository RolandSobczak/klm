"""Pick-and-place rotation corrections, and how klm learns them.

JLCPCB's placement machine expects an orientation that differs from KiCad's
footprint convention for many packages. Wrong by 180° on a polarised part is a
scrapped board, not a cosmetic complaint.

**klm bundles no correction data**, which is a decision rather than an omission
([ADR-0011](../../docs/adr/0011-rotation-corrections-are-learned-not-bundled.md)).
The one community table everything else copies is GPL-3.0 while klm is MIT, and
more importantly it is keyed by footprint name — but the correct orientation is
a property of how the part sits in its *reel*, chosen per manufacturer part
number. Two parts on one 0603 land pattern can need different rotations, and a
footprint-keyed table answers that confidently and sometimes wrongly.

So klm keys on the part, which it can do because it has a catalog, and fills the
table from boards that came back. What is in it is what a physical board
demonstrated.

Resolution runs most specific first:

1. a per-part correction recorded here
2. a ``KLM_FAB_ROTATION`` field on the symbol — an explicit manual override
3. a ``user`` or ``learned`` footprint-pattern entry
4. a ``bundled`` footprint-pattern entry (klm ships none; a user may load one)
5. no correction
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from klm.store.db import transaction

__all__ = [
    "FAB_ROTATION_FIELD",
    "Correction",
    "CorrectionSource",
    "confirm_part",
    "delete_pattern",
    "learn_part",
    "list_patterns",
    "part_corrections",
    "resolve",
    "set_pattern",
]

#: A symbol field, so a one-off override travels with the design rather than
#: living in a catalog the collaborator does not have.
FAB_ROTATION_FIELD = "KLM_FAB_ROTATION"


class CorrectionSource:
    BUNDLED = "bundled"
    USER = "user"
    LEARNED = "learned"
    FIELD = "field"
    """Not stored — the origin reported when a symbol field supplied the value."""


@dataclass(frozen=True)
class Correction:
    """A rotation and offset to apply, and where it came from."""

    rotation: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    source: str = CorrectionSource.BUNDLED
    origin: str = ""
    """The pattern or part that supplied it, for the manifest."""
    confirmed_at: str | None = None

    @property
    def confirmed(self) -> bool:
        """Whether a physical board demonstrated this, rather than someone typing it."""
        return self.confirmed_at is not None

    @property
    def is_identity(self) -> bool:
        return not (self.rotation or self.offset_x or self.offset_y)

    def apply(self, rotation: float) -> float:
        return (rotation + self.rotation) % 360.0


NO_CORRECTION = Correction(source="none")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve(
    conn: sqlite3.Connection,
    *,
    klm_id: str | None,
    footprint: str,
    field_value: str | None = None,
) -> Correction:
    """The correction to apply, by the order in this module's docstring."""
    if klm_id:
        stored = _part_correction(conn, klm_id)
        if stored is not None:
            return stored

    from_field = _parse_field(field_value)
    if from_field is not None:
        return from_field

    return _pattern_correction(conn, footprint)


def _parse_field(raw: str | None) -> Correction | None:
    """Read ``KLM_FAB_ROTATION``, as ``"180"`` or ``"180,0.1,-0.2"``.

    A malformed value is ignored rather than guessed at — but it is the kind of
    thing that should be visible, so preflight reports it separately.
    """
    if not raw or not raw.strip():
        return None
    parts = [p.strip() for p in raw.split(",")]
    try:
        numbers = [float(p) for p in parts if p]
    except ValueError:
        return None
    if not numbers:
        return None
    rotation = numbers[0]
    return Correction(
        rotation=rotation,
        offset_x=numbers[1] if len(numbers) > 1 else 0.0,
        offset_y=numbers[2] if len(numbers) > 2 else 0.0,
        source=CorrectionSource.FIELD,
        origin=FAB_ROTATION_FIELD,
    )


def _part_correction(conn: sqlite3.Connection, klm_id: str) -> Correction | None:
    row = conn.execute(
        "SELECT * FROM part_rotation_correction WHERE klm_id = ?", (klm_id,)
    ).fetchone()
    if row is None:
        return None
    return Correction(
        rotation=float(row["rotation"]),
        offset_x=float(row["offset_x"]),
        offset_y=float(row["offset_y"]),
        source=str(row["source"]),
        origin=klm_id,
        confirmed_at=row["confirmed_at"],
    )


def _pattern_correction(conn: sqlite3.Connection, footprint: str) -> Correction:
    """The best-ranked pattern whose regex matches this footprint.

    Ties are broken by pattern length: a longer pattern is more specific, and
    `^SOIC-8_3.9x4.9mm` should beat `^SOIC-8`.
    """
    name = footprint.partition(":")[2] or footprint
    rank = {CorrectionSource.LEARNED: 0, CorrectionSource.USER: 1, CorrectionSource.BUNDLED: 2}
    best: tuple[int, int, Correction] | None = None

    for row in conn.execute("SELECT * FROM rotation_correction"):
        try:
            if not re.search(str(row["pattern"]), name):
                continue
        except re.error:
            # A user typed the pattern; a bad one must not take down the export.
            continue
        candidate = (
            rank.get(str(row["source"]), 3),
            -len(str(row["pattern"])),
            Correction(
                rotation=float(row["rotation"]),
                offset_x=float(row["offset_x"]),
                offset_y=float(row["offset_y"]),
                source=str(row["source"]),
                origin=str(row["pattern"]),
                confirmed_at=row["confirmed_at"],
            ),
        )
        if best is None or candidate[:2] < best[:2]:
            best = candidate

    return best[2] if best is not None else NO_CORRECTION


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def set_pattern(
    conn: sqlite3.Connection,
    pattern: str,
    rotation: float,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    source: str = CorrectionSource.USER,
    confirmed: bool = False,
) -> None:
    re.compile(pattern)  # fail loudly here rather than silently at export time
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO rotation_correction
                (pattern, rotation, offset_x, offset_y, source, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pattern) DO UPDATE SET
                rotation = excluded.rotation,
                offset_x = excluded.offset_x,
                offset_y = excluded.offset_y,
                source = excluded.source,
                confirmed_at = COALESCE(excluded.confirmed_at, rotation_correction.confirmed_at)
            """,
            (pattern, rotation, offset_x, offset_y, source, now() if confirmed else None),
        )


def delete_pattern(conn: sqlite3.Connection, pattern: str) -> bool:
    with transaction(conn):
        cursor = conn.execute("DELETE FROM rotation_correction WHERE pattern = ?", (pattern,))
    return cursor.rowcount > 0


def learn_part(
    conn: sqlite3.Connection,
    klm_id: str,
    rotation: float,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    source: str = CorrectionSource.LEARNED,
    note: str | None = None,
) -> None:
    """Record what a physical board showed was needed for this part.

    The rotation accumulates: a part already corrected by 90° that still came
    back 180° out needs 270°, and asking the user to do that arithmetic is how
    the second correction gets entered wrong.
    """
    existing = _part_correction(conn, klm_id)
    combined = ((existing.rotation if existing else 0.0) + rotation) % 360.0
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO part_rotation_correction
                (klm_id, rotation, offset_x, offset_y, source, confirmed_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(klm_id) DO UPDATE SET
                rotation = excluded.rotation,
                offset_x = excluded.offset_x,
                offset_y = excluded.offset_y,
                source = excluded.source,
                confirmed_at = excluded.confirmed_at,
                note = excluded.note
            """,
            (klm_id, combined, offset_x, offset_y, source, now(), note),
        )


def confirm_part(conn: sqlite3.Connection, klm_id: str, *, note: str | None = None) -> None:
    """Mark a part's current correction as verified against a real board.

    This is the more valuable half of `klm fab feedback`: it turns "untested"
    into "placed correctly on a board I am holding", including for the parts
    that needed no correction at all — which is most of them, and which is
    exactly the knowledge nothing else records.
    """
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO part_rotation_correction
                (klm_id, rotation, offset_x, offset_y, source, confirmed_at, note)
            VALUES (?, 0, 0, 0, ?, ?, ?)
            ON CONFLICT(klm_id) DO UPDATE SET
                confirmed_at = excluded.confirmed_at,
                note = COALESCE(excluded.note, part_rotation_correction.note)
            """,
            (klm_id, CorrectionSource.LEARNED, now(), note),
        )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def list_patterns(conn: sqlite3.Connection) -> list[tuple[str, Correction]]:
    return [
        (
            str(row["pattern"]),
            Correction(
                rotation=float(row["rotation"]),
                offset_x=float(row["offset_x"]),
                offset_y=float(row["offset_y"]),
                source=str(row["source"]),
                origin=str(row["pattern"]),
                confirmed_at=row["confirmed_at"],
            ),
        )
        for row in conn.execute("SELECT * FROM rotation_correction ORDER BY pattern")
    ]


def part_corrections(conn: sqlite3.Connection) -> list[tuple[str, str, Correction]]:
    """``(klm_id, mpn, correction)`` for every part-level entry."""
    rows = conn.execute(
        """
        SELECT c.*, p.mpn FROM part_rotation_correction c
        JOIN part p ON p.klm_id = c.klm_id
        ORDER BY p.mpn
        """
    )
    return [
        (
            str(row["klm_id"]),
            str(row["mpn"]),
            Correction(
                rotation=float(row["rotation"]),
                offset_x=float(row["offset_x"]),
                offset_y=float(row["offset_y"]),
                source=str(row["source"]),
                origin=str(row["mpn"]),
                confirmed_at=row["confirmed_at"],
            ),
        )
        for row in rows
    ]
