"""Rendering symbols and footprints to SVG.

The desktop app shows a picture of what a part will drop into a schematic, and
a picture is the only review that catches a footprint whose pads are on the
wrong layer or a symbol whose pins collide. Two ways to get one:

* **Shell out to `kicad-cli`.** Exact, and unavailable — the preview would
  vanish on any machine without KiCad, which is the machine most likely to be
  looking at a part it has not seen.
* **Render the S-expression here.** klm already parses both formats completely
  (:mod:`klm.kicad.sexpr`), so this is a traversal, not a parser.

The second, for the same reason `klm bom` reads `.kicad_sch` directly: a
capability that depends on an external tool is a capability that disappears.

**This is a preview, not a plot.** It draws the geometry that carries meaning —
outlines, pads, pins, the reference designator — and deliberately not KiCad's
full graphical vocabulary: no gradients, no bitmaps, no text justification
model, no font metrics. A pad that is in the wrong place is visible here; a
label that sits two millimetres left of where KiCad would put it is not a
defect this drawing exists to find. The QA gate (:mod:`klm.assets.qa`) remains
the thing that *checks*; this only shows.

Unknown nodes are skipped rather than approximated. A future KiCad shape klm
has never seen renders as nothing, which reads as "something is missing" — a
made-up rectangle in its place would read as "this is what you get".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.sexpr import Atom, Document, Node, SExp

__all__ = [
    "LAYER_COLOURS",
    "Drawing",
    "footprint_svg",
    "symbol_svg",
]

#: Approximately KiCad's own dark theme, for the layers a preview draws.
LAYER_COLOURS = {
    "F.Cu": "#c83434",
    "B.Cu": "#4d7fc4",
    "F.SilkS": "#f2f2f2",
    "B.SilkS": "#aaaaaa",
    "F.Mask": "#8b5ba0",
    "B.Mask": "#6f4a80",
    "F.Paste": "#b0b0b0",
    "B.Paste": "#8f8f8f",
    "F.CrtYd": "#d67aa8",
    "B.CrtYd": "#a8628a",
    "F.Fab": "#c2a24b",
    "B.Fab": "#8b7433",
    "Edge.Cuts": "#d0d02a",
}
_SYMBOL_OUTLINE = "#c83434"
_SYMBOL_FILL = "#ffffc2"
_PIN_COLOUR = "#c83434"
_TEXT_COLOUR = "#4d7fc4"
_BACKGROUND = "#1a1a1a"

#: A symbol with no graphics at all still needs a box to draw, in millimetres.
_EMPTY_EXTENT = 5.08


def _f(value: float) -> str:
    """Fixed precision, so the same input always produces the same bytes."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


@dataclass
class Drawing:
    """Accumulated SVG elements and the bounds they occupy.

    Shapes are appended already converted to SVG coordinates, so bounds are a
    running min/max rather than a second traversal that has to repeat every
    transform and get it identically wrong.
    """

    elements: list[str] = field(default_factory=list)
    min_x: float = math.inf
    min_y: float = math.inf
    max_x: float = -math.inf
    max_y: float = -math.inf

    @property
    def empty(self) -> bool:
        return self.min_x is math.inf or not self.elements

    def cover(self, x: float, y: float, margin: float = 0.0) -> None:
        self.min_x = min(self.min_x, x - margin)
        self.min_y = min(self.min_y, y - margin)
        self.max_x = max(self.max_x, x + margin)
        self.max_y = max(self.max_y, y + margin)

    def add(self, element: str) -> None:
        self.elements.append(element)

    def svg(self, *, padding: float = 1.0, scale: float = 12.0, title: str = "") -> str:
        """Wrap the elements in a viewBox that fits them.

        ``scale`` is pixels per millimetre at the nominal size; the SVG still
        scales freely, so it only sets how big the preview is by default.
        """
        if self.empty:
            box = (-_EMPTY_EXTENT, -_EMPTY_EXTENT, _EMPTY_EXTENT * 2, _EMPTY_EXTENT * 2)
            body = [
                f'<text x="0" y="0" fill="{_TEXT_COLOUR}" font-size="2" '
                'text-anchor="middle" font-family="sans-serif">nothing to draw</text>'
            ]
        else:
            box = (
                self.min_x - padding,
                self.min_y - padding,
                (self.max_x - self.min_x) + padding * 2,
                (self.max_y - self.min_y) + padding * 2,
            )
            body = self.elements

        width, height = box[2] * scale, box[3] * scale
        head = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{_f(width)}" height="{_f(height)}" '
            f'viewBox="{_f(box[0])} {_f(box[1])} {_f(box[2])} {_f(box[3])}">'
        )
        parts = [head]
        if title:
            parts.append(f"<title>{_escape(title)}</title>")
        parts.append(
            f'<rect x="{_f(box[0])}" y="{_f(box[1])}" width="{_f(box[2])}" '
            f'height="{_f(box[3])}" fill="{_BACKGROUND}"/>'
        )
        parts.append('<g stroke-linecap="round" stroke-linejoin="round">')
        parts.extend(body)
        parts.append("</g></svg>")
        return "\n".join(parts)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Reading geometry out of the tree
# ---------------------------------------------------------------------------


def _num(node: SExp | None, index: int) -> float:
    if node is None or len(node) <= index:
        return 0.0
    item = node[index]
    if not isinstance(item, Atom):
        return 0.0
    try:
        return float(item.value)
    except ValueError:
        return 0.0


def _atom(item: Node | None) -> str:
    return item.value if isinstance(item, Atom) else ""


def _point(parent: SExp, tag: str) -> tuple[float, float] | None:
    node = parent.find(tag, recursive=False)
    if node is None or len(node) < 3:
        return None
    return (_num(node, 1), _num(node, 2))


def _points(parent: SExp) -> list[tuple[float, float]]:
    pts = parent.find("pts", recursive=False)
    if pts is None:
        return []
    return [(_num(xy, 1), _num(xy, 2)) for xy in pts.find_all("xy")]


def _stroke_width(parent: SExp, default: float) -> float:
    stroke = parent.find("stroke", recursive=False)
    width = stroke.find("width", recursive=False) if stroke is not None else None
    value = _num(width, 1)
    # A zero width means "the default" in KiCad, not "invisible".
    return value if value > 0 else default


def _filled(parent: SExp) -> bool:
    """Whether a symbol shape asks to be filled with the body colour."""
    fill = parent.find("fill", recursive=False)
    if fill is None:
        return False
    kind = fill.find("type", recursive=False)
    return kind is not None and _atom(kind[1] if len(kind) > 1 else None) in (
        "background",
        "outline",
    )


# ---------------------------------------------------------------------------
# Arcs
# ---------------------------------------------------------------------------


def _arc_path(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]
) -> tuple[str, tuple[float, float]] | None:
    """An SVG path for the arc through three points, plus its centre.

    KiCad stores arcs as start/mid/end, which is unambiguous and says nothing
    directly about the two flags SVG wants. Both are derived from the *mid*
    point, so an arc curves the way its file says rather than the way the
    shorter interpretation would.

    Returns ``None`` when the three points are collinear — a degenerate arc has
    no centre, and the caller draws a line instead.
    """
    (x0, y0), (xm, ym), (x1, y1) = start, mid, end
    d = 2 * (x0 * (ym - y1) + xm * (y1 - y0) + x1 * (y0 - ym))
    if abs(d) < 1e-9:
        return None

    s0, sm, s1 = x0 * x0 + y0 * y0, xm * xm + ym * ym, x1 * x1 + y1 * y1
    cx = (s0 * (ym - y1) + sm * (y1 - y0) + s1 * (y0 - ym)) / d
    cy = (s0 * (x1 - xm) + sm * (x0 - x1) + s1 * (xm - x0)) / d
    radius = math.hypot(x0 - cx, y0 - cy)

    # Which way round: the sign of the cross product tells whether the mid
    # point sits left or right of the straight chord.
    cross = (xm - x0) * (y1 - y0) - (ym - y0) * (x1 - x0)
    sweep = 1 if cross > 0 else 0

    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    travel = (a1 - a0) if sweep else (a0 - a1)
    travel %= 2 * math.pi
    large = 1 if travel > math.pi else 0

    path = (
        f"M {_f(x0)} {_f(y0)} A {_f(radius)} {_f(radius)} 0 {large} {sweep} {_f(x1)} {_f(y1)}"
    )
    return path, (cx, cy)


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


def symbol_svg(symbol: Document | SExp, *, scale: float = 12.0) -> str:
    """Render one symbol, pins and all.

    Accepts either a whole ``kicad_symbol_lib`` (the first symbol is drawn) or a
    bare ``(symbol ...)`` node, because an asset in the store may be either.
    """
    extracted = sym.extract_symbols(symbol)
    if not extracted:
        return Drawing().svg(scale=scale)
    node = extracted[0]

    drawing = Drawing()
    # KiCad's symbol space is Y-up and SVG's is Y-down, so every y is negated
    # on the way in. Doing it here rather than with an SVG transform keeps the
    # bounds arithmetic and the arc flags in one coordinate system.
    for shape in _symbol_shapes(node):
        _draw_symbol_shape(drawing, shape)
    for pin in _symbol_pins(node):
        _draw_pin(drawing, pin)

    name = sym.symbol_name(node) or ""
    return drawing.svg(scale=scale, padding=1.27, title=name)


def _symbol_shapes(symbol: SExp) -> list[SExp]:
    """Graphic items of a symbol, including those inside its unit sub-symbols.

    KiCad puts a symbol's graphics in a child ``(symbol "NAME_0_1" ...)``, so a
    search that stops at the top level finds nothing on a real symbol — the
    same trap :func:`klm.kicad.symbols.iter_pins` documents.
    """
    kinds = ("rectangle", "polyline", "circle", "arc", "bezier", "text")
    return [node for kind in kinds for node in symbol.find_all(kind)]


def _draw_symbol_shape(drawing: Drawing, node: SExp) -> None:
    width = _stroke_width(node, 0.254)
    fill = _SYMBOL_FILL if _filled(node) else "none"
    common = (
        f'fill="{fill}" stroke="{_SYMBOL_OUTLINE}" stroke-width="{_f(width)}"'
    )

    if node.name == "rectangle":
        start, end = _point(node, "start"), _point(node, "end")
        if start is None or end is None:
            return
        x0, y0 = start[0], -start[1]
        x1, y1 = end[0], -end[1]
        drawing.cover(min(x0, x1), min(y0, y1), width / 2)
        drawing.cover(max(x0, x1), max(y0, y1), width / 2)
        drawing.add(
            f'<rect x="{_f(min(x0, x1))}" y="{_f(min(y0, y1))}" '
            f'width="{_f(abs(x1 - x0))}" height="{_f(abs(y1 - y0))}" {common}/>'
        )

    elif node.name in ("polyline", "bezier"):
        # A bezier is drawn through its control points rather than as a curve:
        # the shape is approximate but present, and the alternative is a symbol
        # with a hole in it.
        points = [(x, -y) for x, y in _points(node)]
        if len(points) < 2:
            return
        for x, y in points:
            drawing.cover(x, y, width / 2)
        path = " ".join(f"{_f(x)},{_f(y)}" for x, y in points)
        drawing.add(f'<polyline points="{path}" {common}/>')

    elif node.name == "circle":
        centre = _point(node, "center")
        radius = _num(node.find("radius", recursive=False), 1)
        if centre is None or radius <= 0:
            return
        cx, cy = centre[0], -centre[1]
        drawing.cover(cx, cy, radius + width / 2)
        drawing.add(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(radius)}" {common}/>')

    elif node.name == "arc":
        start, mid, end = (_point(node, tag) for tag in ("start", "mid", "end"))
        if start is None or mid is None or end is None:
            return
        flipped = [(p[0], -p[1]) for p in (start, mid, end)]
        for x, y in flipped:
            drawing.cover(x, y, width / 2)
        arc = _arc_path(flipped[0], flipped[1], flipped[2])
        if arc is None:
            drawing.add(
                f'<line x1="{_f(flipped[0][0])}" y1="{_f(flipped[0][1])}" '
                f'x2="{_f(flipped[2][0])}" y2="{_f(flipped[2][1])}" '
                f'stroke="{_SYMBOL_OUTLINE}" stroke-width="{_f(width)}"/>'
            )
            return
        drawing.add(f'<path d="{arc[0]}" {common}/>')

    elif node.name == "text":
        value = _atom(node[1] if len(node) > 1 else None)
        at = node.find("at", recursive=False)
        if not value or at is None:
            return
        x, y = _num(at, 1), -_num(at, 2)
        size = _text_size(node)
        drawing.cover(x, y, size)
        drawing.add(
            f'<text x="{_f(x)}" y="{_f(y)}" fill="{_TEXT_COLOUR}" '
            f'font-size="{_f(size)}" font-family="sans-serif" '
            f'text-anchor="middle">{_escape(value)}</text>'
        )


def _text_size(node: SExp) -> float:
    effects = node.find("effects", recursive=False)
    font = effects.find("font", recursive=False) if effects is not None else None
    size = font.find("size", recursive=False) if font is not None else None
    value = _num(size, 2)
    return value if value > 0 else 1.27


def _symbol_pins(symbol: SExp) -> list[SExp]:
    return list(symbol.find_all("pin"))


def _draw_pin(drawing: Drawing, node: SExp) -> None:
    at = node.find("at", recursive=False)
    if at is None:
        return
    length = _num(node.find("length", recursive=False), 1)
    angle = math.radians(_num(at, 3))
    x, y = _num(at, 1), -_num(at, 2)
    # `at` is the connection point and the angle points *towards the body*, so
    # the line runs from the free end inwards. Getting this backwards puts every
    # pin inside the symbol outline, which looks plausible and is wrong.
    dx, dy = math.cos(angle) * length, -math.sin(angle) * length
    bx, by = x + dx, y + dy

    drawing.cover(x, y, 0.3)
    drawing.cover(bx, by, 0.3)
    drawing.add(
        f'<line x1="{_f(x)}" y1="{_f(y)}" x2="{_f(bx)}" y2="{_f(by)}" '
        f'stroke="{_PIN_COLOUR}" stroke-width="0.152"/>'
    )
    drawing.add(
        f'<circle cx="{_f(x)}" cy="{_f(y)}" r="0.25" fill="none" '
        f'stroke="{_PIN_COLOUR}" stroke-width="0.1"/>'
    )

    number = _sub_text(node, "number")
    if number:
        # Just off the line, on the side the pin runs along.
        ox, oy = (-dy / length * 0.6, dx / length * 0.6) if length else (0.0, -0.6)
        drawing.add(
            f'<text x="{_f((x + bx) / 2 + ox)}" y="{_f((y + by) / 2 + oy)}" '
            f'fill="{_PIN_COLOUR}" font-size="0.9" font-family="sans-serif" '
            f'text-anchor="middle">{_escape(number)}</text>'
        )

    name = _sub_text(node, "name")
    if name and name != "~":
        # `~` is KiCad's "this pin has no name"; drawing it would put a tilde on
        # every passive in the catalog.
        nx, ny = bx + (dx / length * 0.6 if length else 0.6), by + (
            dy / length * 0.6 if length else 0.0
        )
        drawing.cover(nx, ny, 0.6 + len(name) * 0.3)
        drawing.add(
            f'<text x="{_f(nx)}" y="{_f(ny + 0.3)}" fill="{_TEXT_COLOUR}" '
            f'font-size="1" font-family="sans-serif" '
            f'text-anchor="middle">{_escape(name)}</text>'
        )


def _sub_text(node: SExp, tag: str) -> str:
    child = node.find(tag, recursive=False)
    if child is not None and len(child) >= 2:
        return _atom(child[1])
    return ""


# ---------------------------------------------------------------------------
# Footprints
# ---------------------------------------------------------------------------

#: Drawn back to front, so copper sits above the courtyard it is inside and
#: silkscreen sits above everything.
_FOOTPRINT_LAYER_ORDER = ("F.CrtYd", "B.CrtYd", "F.Fab", "B.Fab", "F.SilkS", "B.SilkS")


def footprint_svg(footprint: Document | SExp, *, scale: float = 12.0) -> str:
    """Render a footprint: pads, silkscreen, courtyard and fab outlines."""
    root = footprint.root if isinstance(footprint, Document) else footprint
    drawing = Drawing()

    # Footprint space is already Y-down, so unlike symbols nothing is flipped.
    for pad in fp.iter_pads(root):
        _draw_pad(drawing, pad)
    for layer in _FOOTPRINT_LAYER_ORDER:
        for node in fp.graphics_on(root, layer):
            _draw_footprint_graphic(drawing, node, layer)

    return drawing.svg(scale=scale, padding=0.5, title=fp.footprint_name(root) or "")


def _draw_pad(drawing: Drawing, pad: fp.PadInfo) -> None:
    colour = LAYER_COLOURS.get(
        next((layer for layer in pad.layers if layer in LAYER_COLOURS), ""), "#c83434"
    )
    half_w, half_h = pad.width / 2, pad.height / 2
    drawing.cover(pad.x - half_w, pad.y - half_h)
    drawing.cover(pad.x + half_w, pad.y + half_h)

    if pad.shape in ("circle", "oval") and abs(pad.width - pad.height) < 1e-9:
        drawing.add(
            f'<circle cx="{_f(pad.x)}" cy="{_f(pad.y)}" r="{_f(half_w)}" fill="{colour}"/>'
        )
    else:
        # Ovals and roundrects both round; the exact corner radius is cosmetic
        # here, and a square-cornered pad would misread as a rectangular one.
        radius = min(half_w, half_h) if pad.shape == "oval" else min(half_w, half_h) * 0.25
        drawing.add(
            f'<rect x="{_f(pad.x - half_w)}" y="{_f(pad.y - half_h)}" '
            f'width="{_f(pad.width)}" height="{_f(pad.height)}" '
            f'rx="{_f(radius)}" fill="{colour}"/>'
        )

    if pad.number:
        drawing.add(
            f'<text x="{_f(pad.x)}" y="{_f(pad.y + min(half_w, half_h) * 0.5)}" '
            f'fill="#101010" font-size="{_f(max(min(half_w, half_h), 0.2))}" '
            f'font-family="sans-serif" text-anchor="middle">{_escape(pad.number)}</text>'
        )


def _draw_footprint_graphic(drawing: Drawing, node: SExp, layer: str) -> None:
    colour = LAYER_COLOURS[layer]
    width = _stroke_width(node, 0.12)
    common = f'fill="none" stroke="{colour}" stroke-width="{_f(width)}"'
    kind = (node.name or "").removeprefix("fp_").removeprefix("gr_")

    if kind == "line":
        start, end = _point(node, "start"), _point(node, "end")
        if start is None or end is None:
            return
        drawing.cover(*start, width / 2)
        drawing.cover(*end, width / 2)
        drawing.add(
            f'<line x1="{_f(start[0])}" y1="{_f(start[1])}" '
            f'x2="{_f(end[0])}" y2="{_f(end[1])}" {common}/>'
        )

    elif kind == "rect":
        start, end = _point(node, "start"), _point(node, "end")
        if start is None or end is None:
            return
        drawing.cover(min(start[0], end[0]), min(start[1], end[1]), width / 2)
        drawing.cover(max(start[0], end[0]), max(start[1], end[1]), width / 2)
        drawing.add(
            f'<rect x="{_f(min(start[0], end[0]))}" y="{_f(min(start[1], end[1]))}" '
            f'width="{_f(abs(end[0] - start[0]))}" height="{_f(abs(end[1] - start[1]))}" '
            f"{common}/>"
        )

    elif kind == "circle":
        centre, edge = _point(node, "center"), _point(node, "end")
        if centre is None or edge is None:
            return
        radius = math.hypot(edge[0] - centre[0], edge[1] - centre[1])
        drawing.cover(*centre, radius + width / 2)
        drawing.add(
            f'<circle cx="{_f(centre[0])}" cy="{_f(centre[1])}" r="{_f(radius)}" {common}/>'
        )

    elif kind == "arc":
        start, mid, end = (_point(node, tag) for tag in ("start", "mid", "end"))
        if start is None or mid is None or end is None:
            return
        for point in (start, mid, end):
            drawing.cover(*point, width / 2)
        arc = _arc_path(start, mid, end)
        if arc is None:
            drawing.add(
                f'<line x1="{_f(start[0])}" y1="{_f(start[1])}" '
                f'x2="{_f(end[0])}" y2="{_f(end[1])}" {common}/>'
            )
            return
        drawing.add(f'<path d="{arc[0]}" {common}/>')

    elif kind == "poly":
        points = _points(node)
        if len(points) < 2:
            return
        for point in points:
            drawing.cover(*point, width / 2)
        path = " ".join(f"{_f(x)},{_f(y)}" for x, y in points)
        drawing.add(f'<polygon points="{path}" {common}/>')
