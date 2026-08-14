"""What a board costs to build, and what has been spent so far.

Two reports, both falling out of data klm already holds (docs/10 §9).

**Per-board cost at a quantity** is the number that decides whether a design
change is worth making, and it is tedious enough by hand that it usually does
not get computed. It is an *estimate*, and the estimate is honest about three
things:

* **A line klm cannot price is listed, never guessed at.** A total quietly
  missing the expensive connector is worse than no total.
* **Currencies are never mixed.** Offers arrive in whatever the supplier
  quotes; summing PLN and EUR into one figure produces a number that is wrong
  by the exchange rate and looks right. Totals come out per currency.
* **Stock is not considered.** This is what the parts cost, not what this build
  would cost you given what is already on the shelf — that is `klm order plan`'s
  question, and answering both in one number would answer neither.

**Spend history** aggregates orders that were actually placed. Drafts are
excluded: a draft is a plan, and counting plans as spend makes the figure
useless for the one thing it is for.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from klm.kicad.project import KiCadProject
from klm.services.bom import Variant, extract_bom
from klm.services.offers import list_offers

__all__ = [
    "CostLine",
    "ProjectCost",
    "SpendReport",
    "SpendRow",
    "project_cost",
    "spend_history",
]

#: Money spent, as opposed to money planned.
SPENT_STATES = ("placed", "partially_received", "received")

UNKNOWN_CURRENCY = "?"


@dataclass(frozen=True)
class CostLine:
    klm_id: str | None
    mpn: str
    per_board: int
    quantity: int
    unit_price: float | None = None
    currency: str | None = None
    supplier: str = ""

    @property
    def subtotal(self) -> float | None:
        return None if self.unit_price is None else self.unit_price * self.quantity


@dataclass
class ProjectCost:
    project: str
    boards: int = 1
    variant: str = ""
    lines: list[CostLine] = field(default_factory=list)
    unpriced: list[CostLine] = field(default_factory=list)
    """Lines with no offer to price them. Reported, never assumed free."""

    @property
    def totals(self) -> dict[str, float]:
        """Currency → total for the whole run."""
        sums: dict[str, float] = {}
        for line in self.lines:
            subtotal = line.subtotal
            if subtotal is None:  # pragma: no cover - priced lines have subtotals
                continue
            currency = line.currency or UNKNOWN_CURRENCY
            sums[currency] = sums.get(currency, 0.0) + subtotal
        return sums

    @property
    def per_board(self) -> dict[str, float]:
        boards = max(self.boards, 1)
        return {currency: total / boards for currency, total in self.totals.items()}

    @property
    def complete(self) -> bool:
        return not self.unpriced


def project_cost(
    conn: sqlite3.Connection,
    project: KiCadProject,
    *,
    boards: int = 1,
    variant: Variant | None = None,
) -> ProjectCost:
    """Price a project's BOM at ``boards`` boards.

    Each line is priced at the cheapest offer's break for the quantity the run
    needs — which is where the quantity actually changes the answer, and the
    reason this is not simply "BOM cost times N".
    """
    boards = max(int(boards), 1)
    bom = extract_bom(conn, project, variant=variant)
    result = ProjectCost(
        project=project.name, boards=boards, variant=variant.name if variant else ""
    )

    for line in bom.lines:
        quantity = line.quantity * boards
        priced = _cheapest(conn, line.klm_id, quantity)
        entry = CostLine(
            klm_id=line.klm_id,
            mpn=line.mpn or line.value,
            per_board=line.quantity,
            quantity=quantity,
            unit_price=priced[0] if priced else None,
            currency=priced[1] if priced else None,
            supplier=priced[2] if priced else "",
        )
        (result.lines if priced else result.unpriced).append(entry)

    result.lines.sort(key=lambda entry: -(entry.subtotal or 0.0))
    result.unpriced.sort(key=lambda entry: entry.mpn)
    return result


def _cheapest(
    conn: sqlite3.Connection, klm_id: str | None, quantity: int
) -> tuple[float, str | None, str] | None:
    if not klm_id:
        return None
    priced = [
        (offer.unit_price(quantity), offer.currency, offer.supplier)
        for offer in list_offers(conn, klm_id=klm_id)
    ]
    usable = [item for item in priced if item[0] is not None]
    if not usable:
        return None
    best = min(usable, key=lambda item: item[0] or 0.0)
    return (best[0] or 0.0, best[1], best[2])


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpendRow:
    key: str
    currency: str
    amount: float
    orders: int = 0


@dataclass
class SpendReport:
    by_month: list[SpendRow] = field(default_factory=list)
    by_supplier: list[SpendRow] = field(default_factory=list)
    by_category: list[SpendRow] = field(default_factory=list)
    unpriced_lines: int = 0
    """Ordered lines with no unit price recorded, and so missing from every total."""


def spend_history(conn: sqlite3.Connection) -> SpendReport:
    """What has been spent, by month, by supplier and by category.

    Quantities are what was *ordered*: a discrepancy on receiving adjusts stock
    and is recorded on the line, but the money left the account either way.
    """
    placeholders = ", ".join("?" for _ in SPENT_STATES)
    rows = conn.execute(
        f"""
        SELECT o.id            AS order_id,
               o.supplier      AS supplier,
               o.currency      AS currency,
               o.placed_at     AS placed_at,
               l.qty_ordered   AS qty,
               l.unit_price    AS unit_price,
               c.path          AS category
        FROM purchase_order o
        JOIN purchase_line l ON l.order_id = o.id
        LEFT JOIN part p ON p.klm_id = l.klm_id
        LEFT JOIN category c ON c.id = p.category_id
        WHERE o.state IN ({placeholders})
        """,
        SPENT_STATES,
    ).fetchall()

    report = SpendReport()
    months: dict[tuple[str, str], float] = {}
    suppliers: dict[tuple[str, str], float] = {}
    categories: dict[tuple[str, str], float] = {}
    orders: dict[tuple[str, str], set[str]] = {}

    for row in rows:
        if row["unit_price"] is None:
            report.unpriced_lines += 1
            continue
        amount = float(row["unit_price"]) * int(row["qty"])
        currency = row["currency"] or UNKNOWN_CURRENCY
        month = (row["placed_at"] or "")[:7] or "unplaced"
        category = (row["category"] or "uncategorised").split("/")[0]

        months[(month, currency)] = months.get((month, currency), 0.0) + amount
        suppliers[(row["supplier"], currency)] = (
            suppliers.get((row["supplier"], currency), 0.0) + amount
        )
        categories[(category, currency)] = categories.get((category, currency), 0.0) + amount
        orders.setdefault((row["supplier"], currency), set()).add(row["order_id"])

    report.by_month = _rows(months)
    report.by_supplier = _rows(suppliers, orders)
    report.by_category = _rows(categories)
    return report


def _rows(
    totals: dict[tuple[str, str], float],
    orders: dict[tuple[str, str], set[str]] | None = None,
) -> list[SpendRow]:
    return [
        SpendRow(
            key=key,
            currency=currency,
            amount=amount,
            orders=len(orders[(key, currency)]) if orders else 0,
        )
        for (key, currency), amount in sorted(totals.items())
    ]
