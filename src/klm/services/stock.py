"""Inventory: what is on the bench, and where.

Locations are hierarchical strings — `Cabinet A/Drawer 12/Bag 3` — and
deliberately nothing more. Imposing a schema on someone's physical storage is a
losing game; a path sorts, globs and prints, which is all klm needs.

**Stock is advisory.** It drifts from reality the moment a part is taken out
without being recorded, so `last_counted` is stored on every row and ordering
discounts an old count rather than trusting or ignoring it. The only operation
that *increments* stock is receiving an order, which is what keeps the number
connected to something that actually happened (docs/10 §6).
"""

from __future__ import annotations

import fnmatch
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from klm.model import Part
from klm.services.catalog import get_part
from klm.store.db import transaction

__all__ = [
    "StockItem",
    "adjust",
    "consume",
    "list_stock",
    "low_stock",
    "move",
    "receive_into",
    "where",
]


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class StockItem:
    klm_id: str
    location: str
    quantity: int
    last_counted: str | None = None
    part: Part | None = None

    @property
    def mpn(self) -> str:
        return self.part.mpn if self.part else self.klm_id


def _row(conn: sqlite3.Connection, row: sqlite3.Row) -> StockItem:
    return StockItem(
        klm_id=row["klm_id"],
        location=row["location"],
        quantity=int(row["quantity"]),
        last_counted=row["last_counted"],
        part=get_part(conn, row["klm_id"]),
    )


def list_stock(
    conn: sqlite3.Connection, *, location: str | None = None, klm_id: str | None = None
) -> list[StockItem]:
    """Everything on hand, optionally filtered by a location glob."""
    clauses, params = [], []
    if klm_id:
        clauses.append("klm_id = ?")
        params.append(klm_id)
    where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM stock_item{where_sql} ORDER BY location, klm_id", params
    ).fetchall()
    items = [_row(conn, row) for row in rows]
    if location:
        items = [i for i in items if fnmatch.fnmatch(i.location, location)]
    return items


def where(conn: sqlite3.Connection, klm_id: str) -> list[StockItem]:
    """The question asked most often: where did I put those?"""
    return [item for item in list_stock(conn, klm_id=klm_id) if item.quantity > 0]


def adjust(
    conn: sqlite3.Connection,
    klm_id: str,
    location: str,
    *,
    set_to: int | None = None,
    delta: int = 0,
    counted: bool = True,
) -> StockItem:
    """Record a physical count (``set_to``) or a change (``delta``).

    ``counted`` stamps `last_counted`, and only a genuine count should — a
    decrement from building something tells you nothing about whether the rest
    of the drawer is what the database thinks.
    """
    row = conn.execute(
        "SELECT quantity FROM stock_item WHERE klm_id = ? AND location = ?", (klm_id, location)
    ).fetchone()
    current = int(row["quantity"]) if row else 0
    quantity = set_to if set_to is not None else current + delta
    quantity = max(0, quantity)

    with transaction(conn):
        conn.execute(
            """
            INSERT INTO stock_item (klm_id, location, quantity, last_counted)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(klm_id, location) DO UPDATE SET
                quantity = excluded.quantity,
                last_counted = COALESCE(excluded.last_counted, stock_item.last_counted)
            """,
            (klm_id, location, quantity, now() if counted else None),
        )
    return StockItem(klm_id, location, quantity, now() if counted else None, get_part(conn, klm_id))


def move(conn: sqlite3.Connection, klm_id: str, source: str, target: str, quantity: int) -> None:
    adjust(conn, klm_id, source, delta=-quantity, counted=False)
    adjust(conn, klm_id, target, delta=quantity, counted=False)


def receive_into(conn: sqlite3.Connection, klm_id: str, location: str, quantity: int) -> StockItem:
    """The one operation that increases stock, and it follows a physical parcel."""
    return adjust(conn, klm_id, location, delta=quantity, counted=False)


def consume(
    conn: sqlite3.Connection, demand: dict[str, int]
) -> tuple[list[tuple[str, str, int]], dict[str, int]]:
    """Decrement after actually building. Returns what was taken and what was short.

    Draws from the fullest location first, which is what a person does, and
    reports a shortfall rather than driving a row negative — a negative count is
    a number nobody can act on.
    """
    taken: list[tuple[str, str, int]] = []
    short: dict[str, int] = {}
    for klm_id, wanted in sorted(demand.items()):
        remaining = wanted
        for item in sorted(where(conn, klm_id), key=lambda i: -i.quantity):
            if remaining <= 0:
                break
            amount = min(item.quantity, remaining)
            adjust(conn, klm_id, item.location, delta=-amount, counted=False)
            taken.append((klm_id, item.location, amount))
            remaining -= amount
        if remaining > 0:
            short[klm_id] = remaining
    return taken, short


def low_stock(conn: sqlite3.Connection) -> list[tuple[StockItem, int]]:
    """Parts below their reorder threshold, with the threshold."""
    rows = conn.execute(
        """
        SELECT s.klm_id, SUM(s.quantity) AS total, MIN(s.last_counted) AS counted,
               r.threshold
        FROM stock_item s
        JOIN reorder_threshold r ON r.klm_id = s.klm_id
        GROUP BY s.klm_id
        HAVING total < r.threshold
        ORDER BY s.klm_id
        """
    ).fetchall()
    return [
        (
            StockItem(
                klm_id=row["klm_id"],
                location="(all)",
                quantity=int(row["total"]),
                last_counted=row["counted"],
                part=get_part(conn, row["klm_id"]),
            ),
            int(row["threshold"]),
        )
        for row in rows
    ]


def set_threshold(conn: sqlite3.Connection, klm_id: str, threshold: int) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO reorder_threshold (klm_id, threshold) VALUES (?, ?)
            ON CONFLICT(klm_id) DO UPDATE SET threshold = excluded.threshold
            """,
            (klm_id, threshold),
        )
