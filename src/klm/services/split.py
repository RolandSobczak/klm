"""Assigning each order line to a supplier, and costing the result.

This is the step the spreadsheet gets wrong. Per-supplier shipping is a *step*
function — free above a threshold — which couples the lines together: the
cheapest assignment line-by-line routinely splits a cart to just below a
free-shipping threshold and pays for delivery twice (docs/10 §3).

So: greedy seed, then local search. Move a line to the other supplier, keep the
move if the total drops, repeat until nothing improves. At tens of lines that is
instant and finds the "move these three to TME to cross the free-shipping
threshold" result that greedy cannot.

**Every money figure here is an estimate and is labelled one.** Import VAT and
duty are configuration, never code, because they change: the EU's €150 duty
exemption ended on 1 July 2026 and a €3 per-item duty replaced it, so anything
hardcoded in June was wrong in July (docs/14 Q8). What the numbers are for is
answering "TME or LCSC for this cart", and that comparison survives being
somewhat wrong in the same direction on both sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from klm.config import Config, SupplierConfig
from klm.model import Offer
from klm.services.demand import DemandLine

__all__ = [
    "Assignment",
    "SplitResult",
    "cart_cost",
    "split_order",
]


@dataclass
class Assignment:
    line: DemandLine
    supplier: str
    offer: Offer | None
    quantity: int
    unit_price: float | None
    reason: str = "cheapest"
    """Why this supplier: `cheapest`, `pinned`, `only source`, `local search`."""
    alternatives: list[tuple[str, float, str]] = field(default_factory=list)
    """``(supplier, extra cost, note)`` — the trade-offs only a user can settle."""

    @property
    def subtotal(self) -> float:
        return (self.unit_price or 0.0) * self.quantity

    @property
    def sourced(self) -> bool:
        return self.offer is not None


@dataclass
class CartCost:
    supplier: str
    currency: str = "PLN"
    subtotal: float = 0.0
    shipping: float = 0.0
    duty: float = 0.0
    vat: float = 0.0
    lines: int = 0
    assumptions: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.subtotal + self.shipping + self.duty + self.vat


@dataclass
class SplitResult:
    assignments: list[Assignment] = field(default_factory=list)
    carts: dict[str, CartCost] = field(default_factory=dict)
    unsourced: list[DemandLine] = field(default_factory=list)
    """Lines no enabled supplier stocks. Reported; klm will not guess a source."""
    improvements: list[str] = field(default_factory=list)
    """What local search found, so the result can be argued with."""

    @property
    def total(self) -> float:
        return sum(cart.total for cart in self.carts.values())

    def for_supplier(self, supplier: str) -> list[Assignment]:
        return [a for a in self.assignments if a.supplier == supplier and a.sourced]


# ---------------------------------------------------------------------------
# Costing
# ---------------------------------------------------------------------------

#: Interim EU duty on consignments below €150, per commodity code, in force
#: since 1 July 2026. A default, not a law klm asserts — it is overridable per
#: supplier and every total says it is an estimate (docs/14 Q8).
DEFAULT_SMALL_PARCEL_DUTY = 0.0


def cart_cost(
    supplier: SupplierConfig, assignments: list[Assignment], *, duty_per_parcel: float = 0.0
) -> CartCost:
    """Landed cost for one supplier's cart, with its assumptions attached."""
    cart = CartCost(supplier=supplier.name, currency=supplier.currency)
    cart.subtotal = sum(a.subtotal for a in assignments)
    cart.lines = len(assignments)
    if not assignments:
        return cart

    if supplier.free_shipping_above is not None and cart.subtotal >= supplier.free_shipping_above:
        cart.shipping = 0.0
        cart.assumptions.append(
            f"shipping free above {supplier.free_shipping_above:g} {supplier.currency}"
        )
    else:
        cart.shipping = supplier.shipping_flat
        if supplier.free_shipping_above is not None:
            short = supplier.free_shipping_above - cart.subtotal
            cart.assumptions.append(
                f"shipping {supplier.shipping_flat:g} — {short:.2f} {supplier.currency} "
                "short of free"
            )

    if supplier.import_charges:
        cart.duty = duty_per_parcel
        cart.assumptions.append(
            f"import duty estimated at {duty_per_parcel:g} {supplier.currency} per parcel"
        )
    if supplier.vat_rate:
        cart.vat = (cart.subtotal + cart.shipping + cart.duty) * supplier.vat_rate
        cart.assumptions.append(f"VAT at {supplier.vat_rate * 100:g}%")

    return cart


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def split_order(
    lines: list[DemandLine],
    config: Config,
    *,
    pins: dict[str, str] | None = None,
    assembly: set[str] | None = None,
    duty_per_parcel: float = DEFAULT_SMALL_PARCEL_DUTY,
) -> SplitResult:
    """Choose a supplier per line, then improve the choice across the whole cart.

    ``pins`` is absolute — a user pin overrides cost, always. ``assembly`` names
    parts destined for JLCPCB assembly, which must come from LCSC whatever it
    costs, because the alternative is a part the assembler does not have.
    """
    suppliers = {s.name: s for s in config.enabled_suppliers()} or dict(config.suppliers)
    result = SplitResult()
    pinned = pins or {}
    assembly_parts = assembly or set()

    for line in lines:
        candidates = _candidates(line, suppliers)
        if not candidates:
            result.unsourced.append(line)
            continue
        result.assignments.append(_seed(line, candidates, pinned, assembly_parts))

    _local_search(result, suppliers, duty_per_parcel)
    # Alternatives are recomputed against the *final* choice. Left as the seed
    # computed them, a line local search relocated would list the supplier it
    # now uses as its own alternative, which is nonsense in `--explain`.
    for assignment in result.assignments:
        _describe_alternatives(assignment, suppliers)
    _recost(result, suppliers, duty_per_parcel)
    return result


def _describe_alternatives(
    assignment: Assignment, suppliers: dict[str, SupplierConfig]
) -> None:
    assignment.alternatives = []
    chosen = _price(assignment.offer, assignment.quantity) if assignment.offer else float("inf")
    for offer in sorted(assignment.line.offers, key=lambda o: o.supplier):
        if offer.supplier == assignment.supplier or offer.supplier not in suppliers:
            continue
        price = _price(offer, assignment.quantity)
        if price == float("inf"):
            continue
        note = "" if offer.lead_time_days is None else f"{offer.lead_time_days} day lead time"
        assignment.alternatives.append(
            (offer.supplier, (price - chosen) * assignment.quantity, note)
        )


def _candidates(line: DemandLine, suppliers: dict[str, SupplierConfig]) -> dict[str, Offer]:
    """Offers klm is willing to order against: known supplier, enough stock.

    Unknown stock does not disqualify an offer — it is not the same as zero, and
    treating it as zero would silently drop every manually entered LCSC part.
    """
    out: dict[str, Offer] = {}
    for offer in line.offers:
        if offer.supplier not in suppliers:
            continue
        if offer.stock is not None and offer.stock < line.order_qty:
            continue
        best = out.get(offer.supplier)
        if best is None or _price(offer, line.order_qty) < _price(best, line.order_qty):
            out[offer.supplier] = offer
    return out


def _price(offer: Offer, quantity: int) -> float:
    price = offer.unit_price(quantity)
    # An offer with no price at this quantity sorts last rather than free.
    return price if price is not None else float("inf")


def _seed(
    line: DemandLine,
    candidates: dict[str, Offer],
    pins: dict[str, str],
    assembly: set[str],
) -> Assignment:
    forced: str | None = None
    reason = "cheapest"
    if line.klm_id in pins and pins[line.klm_id] in candidates:
        forced, reason = pins[line.klm_id], "pinned"
    elif line.klm_id in assembly and "lcsc" in candidates:
        forced, reason = "lcsc", "JLCPCB assembly"
    elif len(candidates) == 1:
        forced, reason = next(iter(candidates)), "only source"

    quantity = line.order_qty
    if forced is None:
        forced = min(candidates, key=lambda name: _price(candidates[name], quantity))

    offer = candidates[forced]
    return Assignment(
        line=line,
        supplier=forced,
        offer=offer,
        quantity=offer.order_qty(line.target) if line.target else quantity,
        unit_price=offer.unit_price(quantity),
        reason=reason,
    )


def _local_search(
    result: SplitResult, suppliers: dict[str, SupplierConfig], duty: float, rounds: int = 12
) -> None:
    """Move a line, or a *set* of lines, and keep whatever lowers the total.

    Single-line moves alone are not enough, and the reason is the whole point of
    this step: crossing a free-shipping threshold requires several lines to move
    together, and every intermediate state — some moved, some not — costs *more*
    than either end. A hill-climber that only takes one step at a time sits in
    that valley and reports the greedy answer.

    So set moves are tried too: for each ordered pair of suppliers, move the
    cheapest k of one's lines to the other, for every k. That is O(n squared)
    evaluations, which at tens of lines is instant, and it is a result a user can
    re-derive by hand — which matters more here than optimality.
    """
    movable = [a for a in result.assignments if a.reason in ("cheapest", "local search")]
    if not movable:
        return

    current = _total(result.assignments, suppliers, duty)
    for _ in range(rounds):
        improved = False
        for target in sorted(suppliers):
            for size in range(1, len(movable) + 1):
                group = _cheapest_to_move(movable, target, size)
                if not group:
                    continue
                undo = [(a, a.supplier, a.offer, a.unit_price) for a in group]
                for assignment in group:
                    offer = next(o for o in assignment.line.offers if o.supplier == target)
                    assignment.supplier = target
                    assignment.offer = offer
                    assignment.unit_price = offer.unit_price(assignment.quantity)

                candidate = _total(result.assignments, suppliers, duty)
                if candidate < current - 1e-9:
                    names = ", ".join(a.line.mpn for a in group)
                    result.improvements.append(
                        f"moved {names} to {target}: "
                        f"saves {current - candidate:.2f} across the whole order"
                    )
                    current = candidate
                    for assignment in group:
                        assignment.reason = "local search"
                    improved = True
                else:
                    for assignment, supplier, offer_was, price in undo:
                        assignment.supplier, assignment.offer = supplier, offer_was
                        assignment.unit_price = price
        if not improved:
            return


def _cheapest_to_move(
    movable: list[Assignment], target: str, size: int
) -> list[Assignment]:
    """The ``size`` lines it costs least to relocate to ``target``.

    Cheapest *to move*, not cheapest outright: what matters is the penalty each
    line pays for leaving its current supplier, because that is what has to be
    outweighed by the shipping the move saves.
    """
    penalties: list[tuple[float, int, Assignment]] = []
    for index, assignment in enumerate(movable):
        if assignment.supplier == target:
            continue
        offer = next((o for o in assignment.line.offers if o.supplier == target), None)
        if offer is None:
            continue
        moved = _price(offer, assignment.quantity)
        if moved == float("inf"):
            continue
        penalty = (moved - (assignment.unit_price or 0.0)) * assignment.quantity
        penalties.append((penalty, index, assignment))
    if len(penalties) < size:
        return []
    penalties.sort(key=lambda item: (item[0], item[1]))
    return [assignment for _penalty, _index, assignment in penalties[:size]]


def _total(
    assignments: list[Assignment], suppliers: dict[str, SupplierConfig], duty: float
) -> float:
    grouped: dict[str, list[Assignment]] = {}
    for assignment in assignments:
        if assignment.sourced:
            grouped.setdefault(assignment.supplier, []).append(assignment)
    return sum(
        cart_cost(suppliers[name], group, duty_per_parcel=duty).total
        for name, group in grouped.items()
        if name in suppliers
    )


def _recost(result: SplitResult, suppliers: dict[str, SupplierConfig], duty: float) -> None:
    grouped: dict[str, list[Assignment]] = {}
    for assignment in result.assignments:
        if assignment.sourced:
            grouped.setdefault(assignment.supplier, []).append(assignment)
    result.carts = {
        name: cart_cost(suppliers[name], group, duty_per_parcel=duty)
        for name, group in sorted(grouped.items())
        if name in suppliers
    }
