"""Generating symbols from a template (docs/08 §2, source priority 3).

For a two-terminal passive, the symbol is *entirely* determined by the category
and the package: a 4.7 kΩ 0402 resistor and a 10 kΩ 0603 resistor differ in no
way that a symbol can express. Drawing either by hand is wasted effort, and
drawing both by hand is how a library ends up with two subtly different
resistor symbols.

So klm generates them, and the generation is fully deterministic: same inputs,
byte-identical output, no timestamps, no incidental floats. That property is
load-bearing — it is what lets `klm generate` promise `git diff --exit-code`
after a no-op rebuild.

Everything here draws on KiCad's 1.27 mm (50 mil) grid. Off-grid pins make a
symbol impossible to wire cleanly, which is why the QA gate treats it as an
error rather than a matter of taste.
"""

from __future__ import annotations

from dataclasses import dataclass

from klm.kicad.sexpr import Atom, SExp

__all__ = [
    "GRID",
    "PinSpec",
    "connector_symbol",
    "ic_symbol",
    "passive_symbol",
    "power_pin_type",
]

#: KiCad's schematic grid, in mm. Everything a template emits lands on it.
GRID = 1.27

#: Pin electrical types KiCad understands, as used below.
PIN_TYPES = (
    "input",
    "output",
    "bidirectional",
    "tri_state",
    "passive",
    "free",
    "unspecified",
    "power_in",
    "power_out",
    "open_collector",
    "open_emitter",
    "no_connect",
)

_POWER_INPUTS = frozenset(
    {"VCC", "VDD", "VDDA", "VDDIO", "VBAT", "VIN", "VSS", "VSSA", "GND", "GNDA", "AGND", "DGND"}
)
_POWER_OUTPUTS = frozenset({"VOUT", "VO", "VREF"})


def power_pin_type(pin_name: str) -> str:
    """Guess a pin's electrical type from its name.

    Only rails are guessed, and only from names that are unambiguous. A guess
    that `SDA` is bidirectional would be right most of the time, and the QA
    gate's job is to complain about pins left `passive`, not to have klm invent
    types it cannot justify.
    """
    folded = pin_name.strip().upper().rstrip("+-")
    if folded in _POWER_OUTPUTS:
        return "power_out"
    if folded in _POWER_INPUTS:
        return "power_in"
    return "passive"


@dataclass(frozen=True, slots=True)
class PinSpec:
    number: str
    name: str = "~"
    type: str = "passive"

    def __post_init__(self) -> None:
        if self.type not in PIN_TYPES:
            raise ValueError(f"{self.type!r} is not a KiCad pin type")


# ---------------------------------------------------------------------------
# S-expression helpers
# ---------------------------------------------------------------------------


def _n(value: float) -> Atom:
    """A coordinate, formatted to a fixed precision.

    Fixed rather than repr-shortest because byte-stability is the whole point:
    `2.54` and `2.540000` are the same number and different bytes, and a diff
    that flickers between them is a diff nobody reads.
    """
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return Atom(text if text not in ("", "-0") else "0")


def _at(x: float, y: float, angle: int = 0) -> SExp:
    return SExp([Atom("at"), _n(x), _n(y), _n(angle)])


def _effects(size: float = GRID, *, hidden: bool = False) -> SExp:
    children: list[Atom | SExp] = [SExp([Atom("font"), SExp([Atom("size"), _n(size), _n(size)])])]
    if hidden:
        children.append(SExp([Atom("hide"), Atom("yes")]))
    return SExp([Atom("effects"), *children])


def _stroke(width: float = 0.254) -> SExp:
    return SExp(
        [Atom("stroke"), SExp([Atom("width"), _n(width)]),
         SExp([Atom("type"), Atom("default")])]
    )


def _fill(kind: str = "none") -> SExp:
    return SExp([Atom("fill"), SExp([Atom("type"), Atom(kind)])])


def _rectangle(x1: float, y1: float, x2: float, y2: float, *, fill: str = "background") -> SExp:
    return SExp(
        [
            Atom("rectangle"),
            SExp([Atom("start"), _n(x1), _n(y1)]),
            SExp([Atom("end"), _n(x2), _n(y2)]),
            _stroke(),
            _fill(fill),
        ]
    )


def _polyline(points: list[tuple[float, float]], *, width: float = 0.254) -> SExp:
    pts = SExp([Atom("pts"), *[SExp([Atom("xy"), _n(x), _n(y)]) for x, y in points]])
    return SExp([Atom("polyline"), pts, _stroke(width), _fill()])


def _pin(spec: PinSpec, x: float, y: float, angle: int, length: float) -> SExp:
    return SExp(
        [
            Atom("pin"),
            Atom(spec.type),
            Atom("line"),
            _at(x, y, angle),
            SExp([Atom("length"), _n(length)]),
            SExp([Atom("name"), Atom(spec.name, quoted=True), _effects(GRID)]),
            SExp([Atom("number"), Atom(spec.number, quoted=True), _effects(GRID)]),
        ]
    )


def _property(name: str, value: str, x: float, y: float, *, hidden: bool) -> SExp:
    return SExp(
        [
            Atom("property"),
            Atom(name, quoted=True),
            Atom(value, quoted=True),
            _at(x, y),
            _effects(GRID, hidden=hidden),
        ]
    )


def _symbol_shell(
    name: str,
    designator: str,
    value: str,
    *,
    show_value: bool = True,
) -> SExp:
    """The `(symbol …)` node with its four KiCad-reserved fields.

    klm's own fields are *not* written here. They are injected at generation
    time from the catalog, which is the only place that knows them, and putting
    a stale copy in the stored asset would create two answers to one question.
    """
    return SExp(
        [
            Atom("symbol"),
            Atom(name, quoted=True),
            SExp([Atom("exclude_from_sim"), Atom("no")]),
            SExp([Atom("in_bom"), Atom("yes")]),
            SExp([Atom("on_board"), Atom("yes")]),
            _property("Reference", designator, 0, 3.81, hidden=False),
            _property("Value", value, 0, -3.81, hidden=not show_value),
            _property("Footprint", "", 0, -6.35, hidden=True),
            _property("Datasheet", "", 0, -8.89, hidden=True),
        ]
    )


def _unit(parent: str, body: list[SExp], *, index: str = "0_1") -> SExp:
    return SExp([Atom("symbol"), Atom(f"{parent}_{index}", quoted=True), *body])


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

#: Body graphics per designator. A resistor is a rectangle, a capacitor two
#: plates, an inductor three arcs approximated by a polyline — KiCad's own
#: conventions, because a library whose resistors look unfamiliar is a library
#: people misread.
_PASSIVE_BODIES = {
    "R": [_rectangle(-1.016, -2.54, 1.016, 2.54, fill="none")],
    "L": [_rectangle(-1.016, -2.54, 1.016, 2.54, fill="none")],
    "FB": [_rectangle(-1.016, -2.54, 1.016, 2.54, fill="none")],
    "C": [
        _polyline([(-2.032, -0.762), (2.032, -0.762)], width=0.508),
        _polyline([(-2.032, 0.762), (2.032, 0.762)], width=0.508),
    ],
}


def passive_symbol(name: str, designator: str, value: str = "") -> SExp:
    """A two-terminal symbol: pins at ±3.81 mm, body between them.

    The pin numbers are `1` and `2` and both are `passive`, which for a resistor
    or a capacitor is not a placeholder — it is the correct electrical type.
    """
    body = _PASSIVE_BODIES.get(designator.upper())
    if body is None:
        raise ValueError(
            f"no passive template for designator {designator!r} "
            f"(known: {', '.join(sorted(_PASSIVE_BODIES))})"
        )

    symbol = _symbol_shell(name, designator, value)
    symbol.children.append(_unit(name, body))
    symbol.children.append(
        _unit(
            name,
            [
                _pin(PinSpec("1"), 0, 3.81, 270, 1.27),
                _pin(PinSpec("2"), 0, -3.81, 90, 1.27),
            ],
            index="1_1",
        )
    )
    return symbol


def connector_symbol(name: str, pins: int, *, designator: str = "J") -> SExp:
    """A single-row connector: pins down the left edge, numbered top to bottom."""
    if pins < 1:
        raise ValueError("a connector needs at least one pin")

    height = pins * GRID * 2
    top = height / 2
    symbol = _symbol_shell(name, designator, f"Conn_01x{pins:02d}")
    symbol.children.append(_unit(name, [_rectangle(-1.27, top, 1.27, -top)]))

    pin_nodes = [
        _pin(
            PinSpec(str(number), f"Pin_{number}"),
            -5.08,
            top - GRID - (number - 1) * GRID * 2,
            0,
            3.81,
        )
        for number in range(1, pins + 1)
    ]
    symbol.children.append(_unit(name, pin_nodes, index="1_1"))
    return symbol


def ic_symbol(name: str, pins: list[PinSpec], value: str = "", *, designator: str = "U") -> SExp:
    """A rectangular IC: pins split evenly down the left and right edges.

    Left/right rather than all four sides because a generated four-sided symbol
    is worse than a two-sided one — the pin ordering carries no meaning either
    way, and two sides is at least readable. A hand-drawn or imported symbol is
    the right answer for anything where pin placement should say something.
    """
    if not pins:
        raise ValueError("an IC symbol needs at least one pin")

    half = (len(pins) + 1) // 2
    left, right = pins[:half], pins[half:]
    rows = max(len(left), len(right))
    height = (rows + 1) * GRID * 2
    top = height / 2
    width = 7.62

    symbol = _symbol_shell(name, designator, value)
    symbol.children.append(_unit(name, [_rectangle(-width, top, width, -top)]))

    nodes: list[SExp] = []
    for index, spec in enumerate(left):
        y = top - GRID * 2 - index * GRID * 2
        nodes.append(_pin(spec, -width - 3.81, y, 0, 3.81))
    for index, spec in enumerate(right):
        y = top - GRID * 2 - index * GRID * 2
        nodes.append(_pin(spec, width + 3.81, y, 180, 3.81))
    symbol.children.append(_unit(name, nodes, index="1_1"))
    return symbol
