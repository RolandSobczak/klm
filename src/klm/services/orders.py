"""Order lifecycle, cart export and receiving.

**klm does not place orders.** It produces carts a human reviews and submits.
Spending money is not something a tool should do on its own, and the export
formats exist because pasting sixty part numbers into a web form by hand is the
tedium this phase is here to end (docs/10 §4).

Receiving is the only operation that increments stock. That is what keeps
inventory connected to something physical: a count that grew because a plan said
it should is a count that means nothing. Discrepancies — fewer parts arrived
than ordered, or a substitution shipped — are recorded on the line rather than
silently reconciled, because the difference between "I received 48 of 50" and
"I received 50" is exactly what you need six weeks later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from klm.fab.profiles import render_csv
from klm.services.catalog import get_part
from klm.services.split import Assignment, SplitResult
from klm.services.stock import receive_into
from klm.store.db import transaction

__all__ = [
    "STATES",
    "Order",
    "OrderLine",
    "create_order",
    "export_cart",
    "get_order",
    "list_orders",
    "mark_placed",
    "pin_supplier",
    "receive",
]

STATES = ("draft", "placed", "partially_received", "received", "cancelled")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class OrderLine:
    supplier_pn: str
    klm_id: str
    qty_ordered: int
    qty_received: int = 0
    unit_price: float | None = None
    supplier: str = ""
    discrepancy: str | None = None

    @property
    def outstanding(self) -> int:
        return max(0, self.qty_ordered - self.qty_received)


@dataclass
class Order:
    id: str
    supplier: str
    state: str = "draft"
    currency: str | None = None
    placed_at: str | None = None
    received_at: str | None = None
    notes: str | None = None
    lines: list[OrderLine] = field(default_factory=list)

    @property
    def total_ordered(self) -> int:
        return sum(line.qty_ordered for line in self.lines)

    @property
    def estimated(self) -> float:
        return sum((line.unit_price or 0.0) * line.qty_ordered for line in self.lines)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def create_order(
    conn: sqlite3.Connection,
    supplier: str,
    assignments: list[Assignment],
    *,
    order_id: str | None = None,
    currency: str | None = None,
    notes: str | None = None,
) -> Order:
    """Record a draft order for one supplier's share of a split."""
    identifier = order_id or f"{supplier}-{now().replace(':', '').replace('-', '')}"
    order = Order(id=identifier, supplier=supplier, currency=currency, notes=notes)
    order.lines = [
        OrderLine(
            supplier_pn=a.offer.supplier_pn if a.offer else a.line.mpn,
            klm_id=a.line.klm_id,
            qty_ordered=a.quantity,
            unit_price=a.unit_price,
            supplier=supplier,
        )
        for a in assignments
        if a.sourced
    ]

    with transaction(conn):
        conn.execute(
            "INSERT INTO purchase_order (id, supplier, state, currency, notes) "
            "VALUES (?, ?, 'draft', ?, ?)",
            (order.id, supplier, currency, notes),
        )
        conn.executemany(
            """
            INSERT INTO purchase_line
                (order_id, klm_id, supplier_pn, qty_ordered, qty_received, unit_price, supplier)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            [
                (order.id, line.klm_id, line.supplier_pn, line.qty_ordered, line.unit_price,
                 supplier)
                for line in order.lines
            ],
        )
    return order


def get_order(conn: sqlite3.Connection, order_id: str) -> Order | None:
    row = conn.execute("SELECT * FROM purchase_order WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    order = Order(
        id=row["id"],
        supplier=row["supplier"],
        state=row["state"],
        currency=row["currency"],
        placed_at=row["placed_at"],
        received_at=row["received_at"],
        notes=row["notes"],
    )
    order.lines = [
        OrderLine(
            supplier_pn=line["supplier_pn"],
            klm_id=line["klm_id"],
            qty_ordered=int(line["qty_ordered"]),
            qty_received=int(line["qty_received"]),
            unit_price=line["unit_price"],
            supplier=line["supplier"] or order.supplier,
            discrepancy=line["discrepancy"],
        )
        for line in conn.execute(
            "SELECT * FROM purchase_line WHERE order_id = ? ORDER BY supplier_pn", (order_id,)
        )
    ]
    return order


def list_orders(conn: sqlite3.Connection, *, state: str | None = None) -> list[Order]:
    clause = " WHERE state = ?" if state else ""
    rows = conn.execute(
        f"SELECT id FROM purchase_order{clause} ORDER BY id DESC", ([state] if state else [])
    ).fetchall()
    return [order for order in (get_order(conn, row["id"]) for row in rows) if order]


def mark_placed(conn: sqlite3.Connection, order_id: str, *, total: float | None = None) -> Order:
    with transaction(conn):
        conn.execute(
            "UPDATE purchase_order SET state = 'placed', placed_at = ?, "
            "notes = COALESCE(?, notes) WHERE id = ?",
            (now(), f"submitted total {total:g}" if total is not None else None, order_id),
        )
    order = get_order(conn, order_id)
    if order is None:
        raise KeyError(f"no order {order_id!r}")
    return order


@dataclass
class ReceiveReport:
    order: Order
    stocked: list[tuple[str, str, int]] = field(default_factory=list)
    """``(klm_id, location, quantity)``."""
    discrepancies: list[tuple[str, str]] = field(default_factory=list)


def receive(
    conn: sqlite3.Connection,
    order_id: str,
    *,
    location: str = "unfiled",
    partial: dict[str, int] | None = None,
) -> ReceiveReport:
    """Book a delivery in, incrementing stock.

    Without ``partial`` everything outstanding is received. With it, only the
    named supplier part numbers are, and the order stays
    ``partially_received`` — the state exists because a split shipment is normal
    and pretending it arrived complete makes the next order wrong.
    """
    order = get_order(conn, order_id)
    if order is None:
        raise KeyError(f"no order {order_id!r}")

    report = ReceiveReport(order=order)
    for line in order.lines:
        wanted = (
            partial.get(line.supplier_pn, 0) if partial is not None else line.outstanding
        )
        if wanted <= 0:
            continue
        arrived = min(wanted, line.outstanding) if partial is None else wanted
        receive_into(conn, line.klm_id, location, arrived)
        report.stocked.append((line.klm_id, location, arrived))
        line.qty_received += arrived
        if line.qty_received != line.qty_ordered:
            note = f"received {line.qty_received} of {line.qty_ordered}"
            line.discrepancy = note
            report.discrepancies.append((line.supplier_pn, note))

    complete = all(line.qty_received >= line.qty_ordered for line in order.lines)
    state = "received" if complete else "partially_received"
    with transaction(conn):
        for line in order.lines:
            conn.execute(
                "UPDATE purchase_line SET qty_received = ?, discrepancy = ? "
                "WHERE order_id = ? AND supplier_pn = ?",
                (line.qty_received, line.discrepancy, order_id, line.supplier_pn),
            )
        conn.execute(
            "UPDATE purchase_order SET state = ?, received_at = ? WHERE id = ?",
            (state, now() if complete else None, order_id),
        )
    order.state = state
    return report


def pin_supplier(
    conn: sqlite3.Connection, klm_id: str, supplier: str | None, *, note: str | None = None
) -> None:
    """A user pin is absolute and overrides cost. ``None`` removes it."""
    with transaction(conn):
        if supplier is None:
            conn.execute("DELETE FROM supplier_pin WHERE klm_id = ?", (klm_id,))
        else:
            conn.execute(
                "INSERT INTO supplier_pin (klm_id, supplier, note) VALUES (?, ?, ?) "
                "ON CONFLICT(klm_id) DO UPDATE SET supplier = excluded.supplier, "
                "note = excluded.note",
                (klm_id, supplier, note),
            )


def pins(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["klm_id"]: row["supplier"]
        for row in conn.execute("SELECT klm_id, supplier FROM supplier_pin")
    }


# ---------------------------------------------------------------------------
# Cart export
# ---------------------------------------------------------------------------


def export_cart(
    conn: sqlite3.Connection, supplier: str, assignments: list[Assignment]
) -> tuple[str, str]:
    """``(filename, contents)`` in the shape that supplier's form expects."""
    rows = []
    for assignment in assignments:
        if not assignment.sourced or assignment.offer is None:
            continue
        part = get_part(conn, assignment.line.klm_id)
        rows.append(
            {
                "Part Number": assignment.offer.supplier_pn,
                "Quantity": str(assignment.quantity),
                "MPN": part.mpn if part else "",
                "Manufacturer": part.manufacturer if part else "",
                "Comment": part.description if part else "",
            }
        )

    name = supplier.lower()
    if name == "tme":
        # TME's bulk-add form takes `symbol;quantity` lines, which is far less
        # fiddly than uploading a CSV and mapping columns.
        body = "".join(f"{row['Part Number']};{row['Quantity']}\n" for row in rows)
        return ("cart-tme.txt", body)
    if name == "lcsc":
        return (
            "cart-lcsc.csv",
            render_csv(("Part Number", "Quantity", "MPN", "Manufacturer", "Comment"), rows),
        )
    return (
        f"cart-{name}.csv",
        render_csv(("Part Number", "Quantity", "MPN", "Manufacturer", "Comment"), rows),
    )


def cart_summary(result: SplitResult, supplier: str) -> list[str]:
    """The human-readable half of every export: totals and every assumption."""
    cart = result.carts.get(supplier)
    if cart is None:
        return [f"{supplier}: nothing to order"]
    lines = [
        f"{supplier}: {cart.lines} line(s)",
        f"  subtotal   {cart.subtotal:9.2f} {cart.currency}",
        f"  shipping   {cart.shipping:9.2f} {cart.currency}",
    ]
    if cart.duty:
        lines.append(f"  duty       {cart.duty:9.2f} {cart.currency}   (estimate)")
    if cart.vat:
        lines.append(f"  VAT        {cart.vat:9.2f} {cart.currency}   (estimate)")
    lines.append(f"  total      {cart.total:9.2f} {cart.currency}   (estimate)")
    lines += [f"  assumes: {assumption}" for assumption in cart.assumptions]
    return lines
