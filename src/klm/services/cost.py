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
    "BASELINE_PATH",
    "CostLine",
    "ProjectCost",
    "Regression",
    "SpendReport",
    "SpendRow",
    "compare_snapshots",
    "project_cost",
    "snapshot",
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


# ---------------------------------------------------------------------------
# Regression tracking
# ---------------------------------------------------------------------------

#: Where a project keeps its committed baseline, so CI finds it without flags.
BASELINE_PATH = ".klm/cost-baseline.json"


def snapshot(
    conn: sqlite3.Connection | None,
    project: KiCadProject,
    *,
    boards: int = 1,
    variant: Variant | None = None,
) -> dict[str, object]:
    """What this board is, and — if a catalog is at hand — what it costs.

    ``conn`` may be ``None``, and that is the case that matters: the BOM half
    works on a machine with no catalog, which is the machine the project's own
    CI runs on. The cost half simply does not appear, rather than appearing as
    zero.

    Quantities are per board so the BOM comparison is independent of how many
    boards the baseline was taken at. Output is sorted and carries no
    timestamp: a baseline that changes on every run is one nobody commits.
    """
    boards = max(int(boards), 1)
    bom = extract_bom(conn, project, variant=variant)
    document: dict[str, object] = {
        "project": project.name,
        "variant": variant.name if variant else "",
        "boards": boards,
        "bom": {_key(line.klm_id, line.mpn, line.value): line.quantity for line in bom.lines},
    }
    if conn is not None:
        cost = project_cost(conn, project, boards=boards, variant=variant)
        document["cost"] = {c: round(total, 4) for c, total in sorted(cost.totals.items())}
        document["unpriced"] = sorted(line.mpn for line in cost.unpriced)
    return document


def _key(klm_id: str | None, mpn: str, value: str) -> str:
    return klm_id or f"?{mpn or value}"


def _quantities(raw: object) -> dict[str, int]:
    """A baseline is a file on disk, so nothing in it is trusted to be shaped."""
    if not isinstance(raw, dict):
        return {}
    return {str(key): int(value) for key, value in raw.items()}


def _names(raw: object) -> set[str]:
    return {str(item) for item in raw} if isinstance(raw, list) else set()


@dataclass
class Regression:
    """What changed between a committed baseline and the board as it is now."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[tuple[str, int, int]] = field(default_factory=list)
    """key, was, now — per board."""
    cost: list[tuple[str, float, float, float]] = field(default_factory=list)
    """currency, was, now, percent change."""
    over_tolerance: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Why something could not be compared. Never silently a pass."""

    @property
    def bom_changed(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    @property
    def ok(self) -> bool:
        return not (self.bom_changed or self.over_tolerance or self.notes)


def compare_snapshots(
    baseline: dict[str, object], current: dict[str, object], *, tolerance: float = 0.0
) -> Regression:
    """Compare a snapshot against a committed one.

    ``tolerance`` is a percentage the total may rise by before it counts. A BOM
    change is always reported: it is the thing a cost change is usually caused
    by, and an unintended one is exactly what this check exists to stop. An
    intended one is a re-run of ``klm cost baseline``.
    """
    result = Regression()
    was = _quantities(baseline.get("bom"))
    now = _quantities(current.get("bom"))

    result.added = sorted(set(now) - set(was))
    result.removed = sorted(set(was) - set(now))
    result.changed = [
        (key, was[key], now[key]) for key in sorted(set(was) & set(now))
        if was[key] != now[key]
    ]

    old_cost = baseline.get("cost")
    new_cost = current.get("cost")
    if isinstance(old_cost, dict) and not isinstance(new_cost, dict):
        # The baseline was priced and this run was not — a check that could not
        # run is not a check that passed (docs/08 §5).
        result.notes.append("the baseline carries a cost and this run has no catalog to price it")
        return result
    if not isinstance(old_cost, dict) or not isinstance(new_cost, dict):
        return result

    for currency in sorted(set(old_cost) | set(new_cost)):
        before = float(old_cost.get(currency, 0.0))
        after = float(new_cost.get(currency, 0.0))
        if currency not in old_cost or currency not in new_cost:
            result.notes.append(f"{currency} appears on only one side, so it was not compared")
            continue
        percent = ((after - before) / before * 100.0) if before else 0.0
        result.cost.append((currency, before, after, percent))
        if after > before and percent > tolerance:
            result.over_tolerance.append(
                f"{currency} {before:.2f} → {after:.2f} ({percent:+.1f}%)"
            )

    for mpn in sorted(_names(current.get("unpriced")) - _names(baseline.get("unpriced"))):
        result.notes.append(f"{mpn} has no offer to price it, so the total is incomplete")
    return result
