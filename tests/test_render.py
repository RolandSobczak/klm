"""Tests for the SVG renderer.

What is worth asserting about a drawing is not its bytes — a nicer stroke width
would break every test and nothing else — but the handful of things that are
*wrong* rather than merely different:

* geometry lands where the file says, in the coordinate system SVG uses;
* a pin runs from its connection point *towards* the body, not away from it;
* a shape klm does not understand is skipped rather than approximated;
* the output is well-formed XML and stable across runs.
"""

from __future__ import annotations

import math
from xml.etree import ElementTree

import pytest
from tests.projects import FOOTPRINT_ASSET, SYMBOL_ASSET

from klm.assets.landpattern import chip_footprint
from klm.assets.packages import find_package
from klm.assets.templates import connector_symbol, passive_symbol
from klm.kicad.render import _arc_path, footprint_svg, symbol_svg
from klm.kicad.sexpr import loads


def parse(svg: str) -> ElementTree.Element:
    return ElementTree.fromstring(svg)


def elements(svg: str, tag: str) -> list[ElementTree.Element]:
    return parse(svg).iter(f"{{http://www.w3.org/2000/svg}}{tag}")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Well-formedness
# ---------------------------------------------------------------------------


def test_symbol_renders_well_formed_svg() -> None:
    root = parse(symbol_svg(loads(SYMBOL_ASSET)))
    assert root.tag.endswith("svg")
    assert root.get("viewBox")


def test_footprint_renders_well_formed_svg() -> None:
    root = parse(footprint_svg(loads(FOOTPRINT_ASSET)))
    assert root.tag.endswith("svg")


def test_rendering_is_stable() -> None:
    """Same input, same bytes — the rest of klm holds to this and so does this."""
    symbol = passive_symbol("R_x", "R", "10k")
    assert symbol_svg(symbol) == symbol_svg(symbol)


def test_a_name_with_a_quote_does_not_break_the_document() -> None:
    symbol = loads(SYMBOL_ASSET.replace('"~"', '"A<B&C"'))
    assert "A&lt;B&amp;C" in symbol_svg(symbol)
    parse(symbol_svg(symbol))  # would raise if the escaping were wrong


def test_nothing_to_draw_says_so() -> None:
    """An empty symbol gets a box with a caption, not a zero-sized document."""
    svg = symbol_svg(loads('(symbol "Empty")'))
    assert "nothing to draw" in svg
    parse(svg)


def test_a_document_that_is_not_a_symbol_renders_empty() -> None:
    assert "nothing to draw" in symbol_svg(loads("(kicad_pcb (version 1))"))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_symbol_y_axis_is_flipped() -> None:
    """KiCad symbols are Y-up and SVG is Y-down.

    Without the flip a symbol renders mirrored: legible, plausible, and with
    pin 1 in the wrong corner.
    """
    svg = symbol_svg(passive_symbol("R_x", "R", "10k"))
    rects = [e for e in elements(svg, "rect") if e.get("stroke")]
    assert rects, "the resistor body should be drawn"
    # The body is (-1.016, -2.54) to (1.016, 2.54) in KiCad's space; flipped,
    # it occupies the same box, so what proves the flip is the pins.
    lines = list(elements(svg, "line"))
    # Pin 1 is at KiCad y=+3.81, which is *above* the body: SVG y=-3.81.
    assert any(float(line.get("y1", "0")) == pytest.approx(-3.81) for line in lines)


def test_a_pin_runs_from_its_connection_point_towards_the_body() -> None:
    """Pin 1 of a resistor: at (0, 3.81) angle 270, length 1.27.

    Drawing it the other way puts the line at y=5.08 — outside everything, and
    the symbol still looks like a resistor, which is why this is asserted.
    """
    svg = symbol_svg(passive_symbol("R_x", "R", "10k"))
    ends = {
        (float(line.get("y1", "0")), float(line.get("y2", "0")))
        for line in elements(svg, "line")
    }
    assert (-3.81, -2.54) in ends


def test_a_connector_pin_runs_inwards_too() -> None:
    """The horizontal case: at x=-5.08, angle 0, length 3.81 → body edge -1.27."""
    svg = symbol_svg(connector_symbol("J_x", 2))
    spans = {
        (float(line.get("x1", "0")), float(line.get("x2", "0")))
        for line in elements(svg, "line")
    }
    assert (-5.08, -1.27) in spans


def test_footprint_pads_land_where_the_file_says() -> None:
    """An 0402: two 0.5 x 0.6 pads at x = ±0.5. Y is *not* flipped here."""
    svg = footprint_svg(chip_footprint(find_package("0402")))
    pads = [e for e in elements(svg, "rect") if e.get("fill", "").startswith("#c")]
    assert len(pads) == 2
    lefts = sorted(float(pad.get("x", "0")) for pad in pads)
    assert lefts == pytest.approx([-0.75, 0.25])


def test_a_round_pad_is_drawn_round() -> None:
    fp = loads(
        '(footprint "X" (pad "1" thru_hole circle (at 1 2) (size 1.6 1.6) (layers "F.Cu")))'
    )
    circles = list(elements(footprint_svg(fp), "circle"))
    assert len(circles) == 1
    assert float(circles[0].get("r", "0")) == pytest.approx(0.8)


def test_the_viewbox_covers_the_courtyard() -> None:
    """A courtyard outside the pads must not be cropped out of the picture."""
    svg = footprint_svg(chip_footprint(find_package("0402")))
    min_x, min_y, width, height = (float(v) for v in parse(svg).get("viewBox", "").split())
    assert min_x <= -1.0 and min_y <= -0.55
    assert min_x + width >= 1.0 and min_y + height >= 0.55


def test_an_unknown_shape_is_skipped_not_guessed() -> None:
    """A future KiCad primitive draws as nothing, which reads as 'missing'."""
    fp = loads('(footprint "X" (fp_hyperbola (start 0 0) (end 1 1) (layer "F.SilkS")))')
    assert "nothing to draw" in footprint_svg(fp)


def test_only_the_layers_a_preview_needs_are_drawn() -> None:
    """Silkscreen is drawn; a user annotation layer is not.

    A fixed layer set, not a bug: a preview showing every layer a footprint can
    carry is a preview nobody can read.
    """
    fp = loads(
        '(footprint "X"'
        ' (fp_line (start 0 0) (end 1 1) (layer "F.SilkS") (stroke (width 0.1)))'
        ' (fp_line (start 0 0) (end 2 2) (layer "User.Comments") (stroke (width 0.1))))'
    )
    assert len(list(elements(footprint_svg(fp), "line"))) == 1


# ---------------------------------------------------------------------------
# Arcs
# ---------------------------------------------------------------------------


def test_an_arc_curves_through_its_midpoint() -> None:
    """KiCad stores start/mid/end; SVG wants two flags. Both come from the mid.

    The semicircle from (0,0) to (2,0) through (1,1) is centred at (1,0) with
    radius 1, and goes the *short* way in the direction the mid point picks.
    """
    result = _arc_path((0.0, 0.0), (1.0, 1.0), (2.0, 0.0))
    assert result is not None
    path, centre = result
    assert centre == pytest.approx((1.0, 0.0))
    assert "A 1 1 0 0 0 2 0" in path


def test_the_mirrored_arc_sweeps_the_other_way() -> None:
    a = _arc_path((0.0, 0.0), (1.0, 1.0), (2.0, 0.0))
    b = _arc_path((0.0, 0.0), (1.0, -1.0), (2.0, 0.0))
    assert a is not None and b is not None
    assert a[0] != b[0], "an arc bulging the other way must not render identically"


def test_a_major_arc_sets_the_large_flag() -> None:
    """Three quarters of a circle: taking the short way would draw the wrong shape."""
    radius = 1.0
    start = (radius, 0.0)
    mid = (math.cos(math.pi * 0.75) * radius, math.sin(math.pi * 0.75) * radius)
    end = (0.0, -radius)
    result = _arc_path(start, mid, end)
    assert result is not None
    assert " 1 1 " in result[0], "large-arc flag should be set"


def test_collinear_points_have_no_arc() -> None:
    """A degenerate arc has no centre; the caller draws a line instead."""
    assert _arc_path((0.0, 0.0), (1.0, 0.0), (2.0, 0.0)) is None


def test_a_degenerate_arc_still_draws_something() -> None:
    fp = loads(
        '(footprint "X" (fp_arc (start 0 0) (mid 1 0) (end 2 0) (layer "F.SilkS") '
        '(stroke (width 0.1))))'
    )
    assert len(list(elements(footprint_svg(fp), "line"))) == 1
