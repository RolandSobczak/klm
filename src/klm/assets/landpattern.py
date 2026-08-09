"""Generating a chip land pattern, when KiCad's own is not available.

**This is a fallback, not the preferred source.** KiCad's footprint libraries
are IPC-compliant and widely reviewed, and `klm assets acquire` reaches for them
first (docs/08 §1, priority 2). This module exists for the case where they are
not installed — a CI runner, a fresh machine, a container — so that klm can
still produce a usable part rather than stopping.

Only **two-terminal chip packages** are generated, and that limit is deliberate.
Their land pattern is two rectangles derived from dimensions that are not in
dispute: an 0402 is 1.0 x 0.5 mm by definition. A fine-pitch QFN's is not, and
reconstructing one from memory would produce a footprint that looks right,
passes a visual check, and does not solder.

The construction is the IPC-7351B one, at density level B (nominal)::

    Zmax = L + 2·JT          outer span
    Gmin = L - 2·T - 2·JH    inner gap        (JH = 0 for chips)
    Xmax = W + 2·JS          pad width across the body

    pad length  = (Zmax - Gmin) / 2
    pad centre  = (Zmax + Gmin) / 4

Tolerance-driven rounding is omitted, so these come out within about 0.05 mm of
KiCad's equivalents — close enough to solder, and the QA gate marks a generated
footprint so it is never mistaken for a reviewed one.
"""

from __future__ import annotations

from klm.assets.packages import ChipDimensions, Package
from klm.kicad.sexpr import Atom, Node, SExp

__all__ = ["GENERATED_BY", "chip_footprint", "chip_footprint_name"]

GENERATED_BY = "klm-landpattern"

#: Clearance from the outermost copper or body edge to the courtyard, in mm.
#: IPC-7351B's density level B for chip components.
COURTYARD_CLEARANCE = 0.25
SILK_CLEARANCE = 0.2
SILK_WIDTH = 0.12
COURTYARD_WIDTH = 0.05
FAB_WIDTH = 0.1


def _n(value: float) -> Atom:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return Atom(text if text not in ("", "-0") else "0")


def _xy(tag: str, x: float, y: float) -> SExp:
    return SExp([Atom(tag), _n(x), _n(y)])


def _line(x1: float, y1: float, x2: float, y2: float, layer: str, width: float) -> SExp:
    return SExp(
        [
            Atom("fp_line"),
            _xy("start", x1, y1),
            _xy("end", x2, y2),
            SExp([Atom("stroke"), SExp([Atom("width"), _n(width)]),
                  SExp([Atom("type"), Atom("solid")])]),
            SExp([Atom("layer"), Atom(layer, quoted=True)]),
        ]
    )


def _rect(half_x: float, half_y: float, layer: str, width: float) -> list[SExp]:
    corners = [
        (-half_x, -half_y, half_x, -half_y),
        (half_x, -half_y, half_x, half_y),
        (half_x, half_y, -half_x, half_y),
        (-half_x, half_y, -half_x, -half_y),
    ]
    return [_line(*corner, layer, width) for corner in corners]


def _text(kind: str, value: str, y: float, layer: str) -> SExp:
    return SExp(
        [
            Atom("fp_text"),
            Atom(kind),
            Atom(value, quoted=True),
            SExp([Atom("at"), _n(0), _n(y), _n(0)]),
            SExp([Atom("layer"), Atom(layer, quoted=True)]),
            SExp(
                [
                    Atom("effects"),
                    SExp(
                        [
                            Atom("font"),
                            SExp([Atom("size"), _n(0.8), _n(0.8)]),
                            SExp([Atom("thickness"), _n(0.12)]),
                        ]
                    ),
                ]
            ),
        ]
    )


def _pad(number: str, x: float, width: float, height: float) -> SExp:
    return SExp(
        [
            Atom("pad"),
            Atom(number, quoted=True),
            Atom("smd"),
            Atom("roundrect"),
            SExp([Atom("at"), _n(x), _n(0)]),
            SExp([Atom("size"), _n(width), _n(height)]),
            SExp([Atom("layers"), Atom("F.Cu", quoted=True), Atom("F.Paste", quoted=True),
                  Atom("F.Mask", quoted=True)]),
            SExp([Atom("roundrect_rratio"), _n(0.25)]),
        ]
    )


def chip_footprint_name(package: Package, designator: str = "R") -> str:
    """`R_0402_1005Metric`, matching KiCad's convention.

    Matching matters: a project that later gains KiCad's libraries should find
    the same name, so swapping a generated footprint for the reviewed one is a
    footprint substitution rather than a rename across every schematic.
    """
    kicad_id = package.kicad_id(designator)
    if kicad_id is not None:
        return kicad_id.split(":", 1)[1]
    return f"{designator}_{package.name}"


def chip_footprint(package: Package, designator: str = "R") -> SExp:
    """Build a `.kicad_mod` tree for a two-terminal chip package."""
    chip: ChipDimensions | None = package.chip
    if chip is None:
        raise ValueError(
            f"{package.name} is not a chip package; klm generates land patterns only for "
            "two-terminal chips — use KiCad's library for anything else"
        )

    z_max = chip.length + 2 * chip.toe
    g_min = chip.length - 2 * chip.termination
    pad_length = (z_max - g_min) / 2
    pad_centre = (z_max + g_min) / 4
    pad_width = chip.width + 2 * chip.side

    copper_x = pad_centre + pad_length / 2
    copper_y = pad_width / 2
    court_x = copper_x + COURTYARD_CLEARANCE
    court_y = max(copper_y, chip.width / 2) + COURTYARD_CLEARANCE

    name = chip_footprint_name(package, designator)
    nodes: list[Node] = [
        Atom("footprint"),
        Atom(name, quoted=True),
        SExp([Atom("layer"), Atom("F.Cu", quoted=True)]),
        SExp([Atom("descr"), Atom(
            f"{designator} chip {package.name} ({chip.length}x{chip.width}mm body), "
            "generated by klm at IPC-7351B density level B",
            quoted=True,
        )]),
        SExp([Atom("tags"), Atom(f"{designator} {package.name}", quoted=True)]),
        SExp([Atom("attr"), Atom("smd")]),
        _text("reference", "REF**", -court_y - 0.8, "F.SilkS"),
        _text("value", name, court_y + 0.8, "F.Fab"),
        _text("user", "${REFERENCE}", 0, "F.Fab"),
    ]

    # Silkscreen lines sit beside the pads rather than over them: silk on a pad
    # is a solder defect waiting to happen, and the QA gate checks for it.
    silk_y = copper_y + SILK_CLEARANCE
    if chip.length / 2 > copper_x:  # body longer than the copper, rare but real
        silk_x = chip.length / 2
        nodes.append(_line(-silk_x, -silk_y, silk_x, -silk_y, "F.SilkS", SILK_WIDTH))
        nodes.append(_line(-silk_x, silk_y, silk_x, silk_y, "F.SilkS", SILK_WIDTH))

    nodes.extend(_rect(chip.length / 2, chip.width / 2, "F.Fab", FAB_WIDTH))
    nodes.extend(_rect(court_x, court_y, "F.CrtYd", COURTYARD_WIDTH))
    nodes.append(_pad("1", -pad_centre, pad_length, pad_width))
    nodes.append(_pad("2", pad_centre, pad_length, pad_width))

    return SExp(nodes)
