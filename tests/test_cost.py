"""Tests for cost reporting.

The estimate has to be honest in two directions: a line klm cannot price is
reported rather than treated as free, and two currencies never land in one
number. Both are the difference between a figure somebody spends money against
and a figure that merely looks like one.
"""

from __future__ import annotations

from pathlib import Path

from tests.projects import RESISTOR_ID, make_project, seed_resistor

from klm.kicad.project import find_project
from klm.model import Offer, PriceBreak
from klm.services.cost import CostLine, ProjectCost, project_cost, spend_history
from klm.services.demand import DemandLine
from klm.services.offers import save_offer
from klm.services.orders import create_order, mark_placed
from klm.services.split import Assignment


def offer_for(klm_id: str, supplier: str, price: float, *, currency: str = "PLN") -> Offer:
    return Offer(
        supplier=supplier,
        supplier_pn=f"{supplier}-1",
        klm_id=klm_id,
        currency=currency,
        stock=1000,
        price_breaks=[PriceBreak(1, price), PriceBreak(100, price / 2)],
    )


def project_at(root: Path):  # type: ignore[no-untyped-def]
    return find_project(make_project(root))


def test_a_run_is_priced_at_the_break_the_quantity_reaches(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _paths, conn, store = env
    seed_resistor(store, conn)
    save_offer(conn, offer_for(RESISTOR_ID, "tme", 1.0))

    one = project_cost(conn, project_at(tmp_path / "a"), boards=1)
    many = project_cost(conn, project_at(tmp_path / "b"), boards=200)

    assert one.lines[0].unit_price == 1.0
    assert many.lines[0].unit_price == 0.5, "the quantity reached the second break"
    assert many.per_board["PLN"] < one.per_board["PLN"]


def test_the_cheapest_offer_prices_the_line(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _paths, conn, store = env
    seed_resistor(store, conn)
    save_offer(conn, offer_for(RESISTOR_ID, "tme", 2.0))
    save_offer(conn, offer_for(RESISTOR_ID, "lcsc", 0.5))

    cost = project_cost(conn, project_at(tmp_path / "a"))

    assert cost.lines[0].supplier == "lcsc"


def test_an_unpriced_line_is_reported_not_treated_as_free(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _paths, conn, store = env
    seed_resistor(store, conn)

    cost = project_cost(conn, project_at(tmp_path / "a"))

    assert cost.lines == []
    assert [line.mpn for line in cost.unpriced] == ["RC0402FR-074K7L"]
    assert not cost.complete
    assert cost.totals == {}


def test_currencies_are_never_summed_together() -> None:
    """Adding PLN to EUR gives a number wrong by the rate and right-looking."""
    cost = ProjectCost(project="mixed", boards=2)
    cost.lines = [
        CostLine("A", "PART-A", per_board=1, quantity=2, unit_price=1.0, currency="PLN"),
        CostLine("B", "PART-B", per_board=1, quantity=2, unit_price=2.0, currency="EUR"),
    ]

    assert cost.totals == {"PLN": 2.0, "EUR": 4.0}
    assert cost.per_board == {"PLN": 1.0, "EUR": 2.0}


def test_spend_counts_placed_orders_and_ignores_drafts(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _paths, conn, store = env
    part = seed_resistor(store, conn)
    assignment = Assignment(
        line=DemandLine(klm_id=part.klm_id, part=part),
        supplier="tme",
        offer=offer_for(part.klm_id, "tme", 1.5),
        quantity=10,
        unit_price=1.5,
    )
    draft = create_order(conn, "tme", [assignment], order_id="draft-1", currency="PLN")
    placed = create_order(conn, "tme", [assignment], order_id="placed-1", currency="PLN")

    assert spend_history(conn).by_supplier == [], "a draft is a plan, not spend"

    mark_placed(conn, placed.id)
    report = spend_history(conn)

    assert [(row.key, row.currency, row.amount) for row in report.by_supplier] == [
        ("tme", "PLN", 15.0)
    ]
    assert report.by_category[0].key == "Passive"
    assert draft.id not in {row.key for row in report.by_month}
