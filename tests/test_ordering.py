"""Tests for demand planning, the supplier split, orders, stock and labels.

The split is what gets tested hardest. Greedy assignment is wrong in a specific,
predictable way — it parks a cart just below a free-shipping threshold — and a
test that only checks "each line went to its cheapest source" would pass on
exactly that bug.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.projects import RESISTOR_ID, make_project, seed_resistor

from klm.config import Config, SupplierConfig
from klm.model import Offer, Part, PartStatus, PriceBreak
from klm.services import stock
from klm.services.catalog import save_part
from klm.services.demand import (
    DemandLine,
    SparesPolicy,
    SparesRule,
    parse_build_plan,
    plan_demand,
)
from klm.services.labels import (
    Label,
    LabelSheet,
    render_png,
    resolve_short_id,
    write_pdf,
)
from klm.services.orders import (
    cart_summary,
    create_order,
    export_cart,
    get_order,
    mark_placed,
    pin_supplier,
    pins,
    receive,
)
from klm.services.split import Assignment, cart_cost, split_order
from klm.store.assets import AssetStore

CAP_ID = "01JB4K7QW8ZR3XN5M2VYT9DCFD"


def config_with(**overrides: object) -> Config:
    tme = SupplierConfig(
        name="tme",
        enabled=True,
        currency="PLN",
        shipping_flat=float(overrides.get("tme_shipping", 15.0)),  # type: ignore[arg-type]
        free_shipping_above=overrides.get("tme_free"),  # type: ignore[arg-type]
    )
    lcsc = SupplierConfig(
        name="lcsc",
        enabled=True,
        mode="manual",
        currency="PLN",
        shipping_flat=float(overrides.get("lcsc_shipping", 60.0)),  # type: ignore[arg-type]
        vat_rate=float(overrides.get("lcsc_vat", 0.0)),  # type: ignore[arg-type]
        import_charges=bool(overrides.get("lcsc_import", False)),
    )
    return Config(suppliers={"tme": tme, "lcsc": lcsc})


def line(klm_id: str, quantity: int, *offers: Offer) -> DemandLine:
    return DemandLine(
        klm_id=klm_id,
        part=Part(klm_id=klm_id, mpn=klm_id[-4:], manufacturer="X"),
        gross=quantity,
        net=quantity,
        target=quantity,
        order_qty=quantity,
        offers=list(offers),
    )


def offer(supplier: str, price: float, *, stock_qty: int | None = 1000, moq: int = 1) -> Offer:
    return Offer(
        supplier=supplier,
        supplier_pn=f"{supplier}-pn",
        klm_id="x",
        stock=stock_qty,
        moq=moq,
        price_breaks=[PriceBreak(1, price)],
    )


# ---------------------------------------------------------------------------
# Build plans and spares
# ---------------------------------------------------------------------------


def test_a_build_plan_is_read_or_refused() -> None:
    """A plan silently missing a board makes an order silently missing parts."""
    items = parse_build_plan("5x sensor-board:full, 2*psu-board")
    assert [(i.project, i.quantity, i.variant) for i in items] == [
        ("sensor-board", 5, "full"),
        ("psu-board", 2, ""),
    ]
    with pytest.raises(ValueError, match="cannot read"):
        parse_build_plan("sensor-board")


def test_spares_are_per_category_with_the_longest_glob_winning() -> None:
    policy = SparesPolicy(
        default=SparesRule(extra_pct=10, min_extra=2),
        rules={
            "passive/*": SparesRule(extra_pct=50, min_extra=10),
            "passive/capacitor/*": SparesRule(extra_pct=100, min_extra=25),
        },
    )
    assert policy.rule_for("Passive/Capacitor/Ceramic", 0.01)[0] == "passive/capacitor/*"
    assert policy.rule_for("Passive/Resistor", 0.01)[0] == "passive/*"
    assert policy.rule_for("IC/MCU", 5.0)[0] == "default"


def test_expensive_parts_never_get_automatic_spares() -> None:
    """Pays for itself the first time it stops two spare 18-euro MCUs."""
    policy = SparesPolicy(
        default=SparesRule(extra_pct=100, min_extra=10),
        price_ceiling=SparesRule(extra_pct=0, min_extra=0, applies_above=50.0),
    )
    name, rule = policy.rule_for("IC/MCU", 82.0)
    assert name == "expensive"
    assert rule.spares(4, 82.0) == 0
    assert policy.rule_for("IC/MCU", 2.0)[1].spares(4, 2.0) == 10


def test_demand_subtracts_stock_and_adds_spares(env, tmp_path: Path) -> None:
    _paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(tmp_path / "sensor-board")
    project = _project(root)
    stock.adjust(conn, RESISTOR_ID, "A/1", set_to=3)

    plan = plan_demand(
        conn,
        parse_build_plan("10x sensor-board"),
        projects_root={"sensor-board": project},
        policy=SparesPolicy(default=SparesRule(extra_pct=0, min_extra=5)),
    )
    result = plan.lines[0]
    assert result.gross == 10
    assert result.trusted_stock == 3
    assert result.net == 7
    assert result.spares == 5
    assert result.target == 12


def test_an_old_count_is_discounted_not_believed(env, tmp_path: Path) -> None:
    """Stock drifts the moment a part is taken out without being recorded."""
    _paths, conn, store = env
    seed_resistor(store, conn)
    project = _project(make_project(tmp_path / "sensor-board"))
    stock.adjust(conn, RESISTOR_ID, "A/1", set_to=10)
    conn.execute(
        "UPDATE stock_item SET last_counted = '2020-01-01T00:00:00Z' WHERE klm_id = ?",
        (RESISTOR_ID,),
    )

    plan = plan_demand(
        conn,
        parse_build_plan("20x sensor-board"),
        projects_root={"sensor-board": project},
        policy=SparesPolicy(default=SparesRule()),
    )
    assert plan.lines[0].on_hand == 10
    assert plan.lines[0].trusted_stock == 5


def test_explain_justifies_every_number(env, tmp_path: Path) -> None:
    _paths, conn, store = env
    seed_resistor(store, conn)
    project = _project(make_project(tmp_path / "sensor-board"))
    stock.adjust(conn, RESISTOR_ID, "A/1", set_to=2)

    plan = plan_demand(
        conn,
        parse_build_plan("10x sensor-board"),
        projects_root={"sensor-board": project},
        policy=SparesPolicy(default=SparesRule(min_extra=4)),
    )
    explanation = " ".join(plan.lines[0].explain())
    assert "10 needed" in explanation and "2 on hand" in explanation and "4 spares" in explanation


def _project(root: Path):  # type: ignore[no-untyped-def]
    from klm.kicad.project import find_project

    return find_project(root)


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------


def test_the_cheapest_line_wins_when_shipping_does_not_interfere() -> None:
    config = config_with(tme_shipping=10.0, lcsc_shipping=10.0)
    result = split_order([line("a", 10, offer("tme", 1.0), offer("lcsc", 0.5))], config)
    assert result.assignments[0].supplier == "lcsc"


def test_shipping_can_outweigh_a_cheaper_unit_price() -> None:
    """5 PLN saved on parts against 45 PLN more in shipping is not a saving."""
    config = config_with(tme_shipping=15.0, lcsc_shipping=60.0)
    result = split_order([line("a", 10, offer("tme", 1.0), offer("lcsc", 0.5))], config)
    assert result.assignments[0].supplier == "tme"


def test_local_search_crosses_a_free_shipping_threshold() -> None:
    """The bug greedy has, stated as a test: it parks a cart just below the line.

    Each line is individually cheaper at LCSC, but consolidating at TME crosses
    free shipping and the whole order costs less.
    """
    lines = [
        line("a", 10, offer("tme", 4.0), offer("lcsc", 3.9)),
        line("b", 10, offer("tme", 4.0), offer("lcsc", 3.9)),
        line("c", 10, offer("tme", 4.0), offer("lcsc", 3.9)),
    ]
    config = config_with(tme_shipping=15.0, tme_free=100.0, lcsc_shipping=60.0)

    result = split_order(lines, config)
    assert {a.supplier for a in result.assignments} == {"tme"}
    assert result.carts["tme"].shipping == 0.0
    assert result.improvements, "the move should be reported, not silently made"


def test_a_pin_overrides_cost_absolutely() -> None:
    lines = [line("a", 10, offer("tme", 9.0), offer("lcsc", 1.0))]
    result = split_order(lines, config_with(), pins={"a": "tme"})
    assert result.assignments[0].supplier == "tme"
    assert result.assignments[0].reason == "pinned"


def test_assembly_parts_are_pinned_to_lcsc() -> None:
    """The alternative is a part the assembler does not have."""
    lines = [line("a", 10, offer("tme", 1.0), offer("lcsc", 9.0))]
    result = split_order(lines, config_with(), assembly={"a"})
    assert result.assignments[0].supplier == "lcsc"
    assert result.assignments[0].reason == "JLCPCB assembly"


def test_an_out_of_stock_offer_is_not_a_source() -> None:
    lines = [line("a", 500, offer("tme", 1.0, stock_qty=10), offer("lcsc", 5.0))]
    assert split_order(lines, config_with()).assignments[0].supplier == "lcsc"


def test_unknown_stock_is_not_treated_as_zero() -> None:
    """It is not the same as zero, and treating it so drops every manual offer."""
    lines = [line("a", 500, offer("lcsc", 1.0, stock_qty=None))]
    result = split_order(lines, config_with())
    assert result.assignments[0].supplier == "lcsc"
    assert not result.unsourced


def test_a_line_no_supplier_stocks_is_reported_not_guessed() -> None:
    lines = [line("a", 10, offer("tme", 1.0, stock_qty=0))]
    result = split_order(lines, config_with())
    assert not result.assignments
    assert [line.klm_id for line in result.unsourced] == ["a"]


def test_alternatives_carry_the_trade_off_only_a_user_can_settle() -> None:
    """docs/10 §3's own example: cheaper per unit, three weeks slower."""
    cheap_but_slow = offer("lcsc", 0.5)
    cheap_but_slow.lead_time_days = 21
    config = config_with(tme_shipping=15.0, lcsc_shipping=60.0)

    result = split_order([line("a", 10, offer("tme", 1.0), cheap_but_slow)], config)
    assignment = result.assignments[0]
    assert assignment.supplier == "tme", "shipping outweighs the unit price here"

    name, delta, note = assignment.alternatives[0]
    assert name == "lcsc"
    assert delta < 0, "cheaper on the line, dearer once shipping is counted"
    assert note == "21 day lead time"


def test_every_money_figure_carries_its_assumptions() -> None:
    """A total a user cannot audit is one they should not spend against."""
    supplier = SupplierConfig(
        name="lcsc", enabled=True, currency="PLN", shipping_flat=60.0,
        vat_rate=0.23, import_charges=True,
    )
    assignment = Assignment(
        line=line("a", 10), supplier="lcsc", offer=offer("lcsc", 2.0), quantity=10, unit_price=2.0
    )
    cart = cart_cost(supplier, [assignment], duty_per_parcel=13.0)

    assert cart.subtotal == 20.0
    assert cart.duty == 13.0
    assert cart.vat == pytest.approx((20.0 + 60.0 + 13.0) * 0.23)
    assert any("VAT at 23%" in a for a in cart.assumptions)
    assert any("duty" in a for a in cart.assumptions)


def test_a_cart_below_the_threshold_says_how_far_short_it_is() -> None:
    config = config_with(tme_shipping=15.0, tme_free=200.0)
    result = split_order([line("a", 1, offer("tme", 10.0))], config)
    assert any("short of free" in a for a in result.carts["tme"].assumptions)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def seeded(env) -> tuple[sqlite3.Connection, AssetStore]:
    _paths, conn, store = env
    seed_resistor(store, conn)
    save_part(
        conn,
        Part(klm_id=CAP_ID, mpn="CC0402", manufacturer="Yageo", status=PartStatus.APPROVED),
    )
    return conn, store


def an_order(conn: sqlite3.Connection) -> str:
    assignments = [
        Assignment(
            line=DemandLine(klm_id=RESISTOR_ID, part=None),
            supplier="tme",
            offer=Offer(supplier="tme", supplier_pn="R-123", klm_id=RESISTOR_ID),
            quantity=50,
            unit_price=0.1,
        )
    ]
    return create_order(conn, "tme", assignments, order_id="tme-1", currency="PLN").id


def test_an_order_round_trips(env) -> None:
    conn, _store = seeded(env)
    order_id = an_order(conn)
    order = get_order(conn, order_id)
    assert order is not None
    assert order.state == "draft"
    assert order.lines[0].qty_ordered == 50
    assert order.estimated == pytest.approx(5.0)


def test_receiving_is_the_only_thing_that_increments_stock(env) -> None:
    conn, _store = seeded(env)
    order_id = an_order(conn)
    assert not stock.where(conn, RESISTOR_ID)

    mark_placed(conn, order_id, total=12.5)
    report = receive(conn, order_id, location="A/1")

    assert report.order.state == "received"
    assert stock.where(conn, RESISTOR_ID)[0].quantity == 50


def test_a_short_delivery_is_recorded_not_reconciled(env) -> None:
    """"48 of 50" is exactly what you need to know six weeks later."""
    conn, _store = seeded(env)
    order_id = an_order(conn)
    report = receive(conn, order_id, location="A/1", partial={"R-123": 48})

    assert report.order.state == "partially_received"
    assert report.discrepancies == [("R-123", "received 48 of 50")]
    reloaded = get_order(conn, order_id)
    assert reloaded is not None and reloaded.lines[0].discrepancy == "received 48 of 50"


def test_a_second_delivery_completes_the_order(env) -> None:
    conn, _store = seeded(env)
    order_id = an_order(conn)
    receive(conn, order_id, location="A/1", partial={"R-123": 48})
    report = receive(conn, order_id, location="A/1")

    assert report.order.state == "received"
    assert stock.where(conn, RESISTOR_ID)[0].quantity == 50


def test_a_pin_survives_in_the_catalog(env) -> None:
    conn, _store = seeded(env)
    pin_supplier(conn, RESISTOR_ID, "tme")
    assert pins(conn) == {RESISTOR_ID: "tme"}
    pin_supplier(conn, RESISTOR_ID, None)
    assert pins(conn) == {}


def test_the_tme_cart_is_the_bulk_add_format(env) -> None:
    conn, _store = seeded(env)
    assignments = [
        Assignment(
            line=DemandLine(klm_id=RESISTOR_ID, part=None),
            supplier="tme",
            offer=Offer(supplier="tme", supplier_pn="R-123", klm_id=RESISTOR_ID),
            quantity=50,
            unit_price=0.1,
        )
    ]
    name, body = export_cart(conn, "tme", assignments)
    assert name == "cart-tme.txt"
    assert body == "R-123;50\n"


def test_the_lcsc_cart_is_a_csv(env) -> None:
    conn, _store = seeded(env)
    assignments = [
        Assignment(
            line=DemandLine(klm_id=RESISTOR_ID, part=None),
            supplier="lcsc",
            offer=Offer(supplier="lcsc", supplier_pn="C25900", klm_id=RESISTOR_ID),
            quantity=100,
            unit_price=0.01,
        )
    ]
    name, body = export_cart(conn, "lcsc", assignments)
    assert name == "cart-lcsc.csv"
    assert "C25900,100" in body


def test_the_summary_labels_every_estimate() -> None:
    result = split_order([line("a", 10, offer("tme", 2.0))], config_with())
    text = "\n".join(cart_summary(result, "tme"))
    assert "(estimate)" in text


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


def test_a_count_stamps_last_counted_but_a_decrement_does_not(env) -> None:
    """A decrement from building tells you nothing about the rest of the drawer."""
    conn, _store = seeded(env)
    stock.adjust(conn, RESISTOR_ID, "A/1", set_to=100)
    counted = stock.where(conn, RESISTOR_ID)[0].last_counted
    assert counted is not None

    stock.adjust(conn, RESISTOR_ID, "A/1", delta=-10, counted=False)
    assert stock.where(conn, RESISTOR_ID)[0].last_counted == counted


def test_consume_draws_from_the_fullest_location_and_reports_a_shortfall(env) -> None:
    conn, _store = seeded(env)
    stock.adjust(conn, RESISTOR_ID, "A/1", set_to=5)
    stock.adjust(conn, RESISTOR_ID, "B/2", set_to=20)

    taken, short = stock.consume(conn, {RESISTOR_ID: 30})
    assert taken[0][1] == "B/2", "the fullest drawer first, which is what a person does"
    assert short == {RESISTOR_ID: 5}
    assert all(item.quantity == 0 for item in stock.list_stock(conn, klm_id=RESISTOR_ID))


def test_stock_never_goes_negative(env) -> None:
    conn, _store = seeded(env)
    stock.adjust(conn, RESISTOR_ID, "A/1", set_to=3)
    stock.adjust(conn, RESISTOR_ID, "A/1", delta=-99)
    assert stock.list_stock(conn, klm_id=RESISTOR_ID)[0].quantity == 0


def test_low_stock_reports_against_a_threshold(env) -> None:
    conn, _store = seeded(env)
    stock.adjust(conn, RESISTOR_ID, "A/1", set_to=4)
    stock.set_threshold(conn, RESISTOR_ID, 25)
    assert [item.quantity for item, _ in stock.low_stock(conn)] == [4]

    stock.adjust(conn, RESISTOR_ID, "A/1", set_to=40)
    assert stock.low_stock(conn) == []


def test_locations_glob(env) -> None:
    conn, _store = seeded(env)
    stock.adjust(conn, RESISTOR_ID, "Cabinet A/Drawer 12", set_to=5)
    stock.adjust(conn, CAP_ID, "Cabinet B/Drawer 1", set_to=5)
    assert len(stock.list_stock(conn, location="Cabinet A/*")) == 1


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_a_label_carries_what_you_need_at_the_drawer(env) -> None:
    """Value and package, not the MPN — you can look an MPN up."""
    _paths, conn, store = env
    part = seed_resistor(store, conn)
    label = Label.for_part(part)
    assert label.primary == part.mpn or label.primary
    assert "0402" in label.secondary
    assert len(label.short) == 8


def test_a_short_id_resolves_back_to_its_part(env) -> None:
    _paths, conn, store = env
    part = seed_resistor(store, conn)
    label = Label.for_part(part)
    assert [p.klm_id for p in resolve_short_id(conn, label.short)] == [part.klm_id]
    assert resolve_short_id(conn, "ZZZZZZZZ") == []


def test_the_pdf_is_a_pdf_and_pages_break(tmp_path: Path) -> None:
    sheet = LabelSheet(columns=2, rows=2)
    labels = [Label(klm_id=f"{i}", short=f"S{i}", primary=f"{i}nF") for i in range(9)]
    target = write_pdf(labels, tmp_path / "labels.pdf", sheet=sheet)

    data = target.read_bytes()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") == 3


def test_labels_fill_from_the_top_of_the_page() -> None:
    """PDF's origin is bottom-left, but a person feeds a sheet from the top."""
    sheet = LabelSheet(columns=2, rows=2)
    first, second = sheet.position(0), sheet.position(2)
    assert first[1] > second[1]


def test_a_png_is_written_at_the_requested_size(tmp_path: Path) -> None:
    label = Label(klm_id="x", short="ABC12345", primary="100nF", secondary="0402 X7R")
    target = render_png(label, tmp_path / "one.png", dpi=300, size_mm=14.0)

    data = target.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width = int.from_bytes(data[16:20], "big")
    assert width == int(14.0 / 25.4 * 300)


def test_an_empty_sheet_still_produces_a_valid_pdf(tmp_path: Path) -> None:
    target = write_pdf([], tmp_path / "empty.pdf")
    assert target.read_bytes().startswith(b"%PDF")
