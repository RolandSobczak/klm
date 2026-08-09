"""The QA gate (docs/08 §5) — the difference between an automated pipeline and
an automated mess.

Nothing becomes `approved` without passing this. Errors block; warnings do not,
because a warning is klm saying "look at this", and a gate that blocks on every
imperfection is a gate people learn to override by reflex.

Three properties shape the checks:

* **A check must be able to fail.** "Pin count matches the package" is only a
  check where klm knows the package's pin count. Where it doesn't, the check is
  *skipped and said to be skipped*, never quietly passed — a green report that
  means "I didn't look" is worse than no report.
* **Errors are things that make the part unusable**, not things that make it
  ugly. Off-grid pins, duplicate pin numbers and a missing courtyard are all in
  that category: each one breaks a downstream tool rather than offending taste.
* **The report is stored on the asset**, so the answer to "was this checked?"
  survives the session that checked it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum

from klm.assets.packages import Package
from klm.categories import find_category
from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.sexpr import Document, SExp

__all__ = [
    "QaReport",
    "QaResult",
    "QaStatus",
    "check_footprint",
    "check_model3d",
    "check_symbol",
]

#: Maximum a 3D model may weigh before klm complains. STEP files dominate a
#: catalog's size, and a 40 MB screw terminal is always a conversion accident.
MAX_MODEL_BYTES = 10 * 1024 * 1024

#: How far a courtyard may sit from the footprint's copper extent before the
#: bounding-box comparison calls them disagreeing, in mm.
BOUNDS_TOLERANCE = 1.0


class QaStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNCHECKED = "unchecked"


@dataclass(frozen=True, slots=True)
class QaResult:
    check: str
    status: QaStatus
    detail: str = ""

    @property
    def skipped(self) -> bool:
        return self.status is QaStatus.UNCHECKED

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.status:<9} {self.check}{suffix}"


@dataclass
class QaReport:
    """The outcome of one asset's checks."""

    kind: str
    results: list[QaResult] = field(default_factory=list)

    def add(self, check: str, status: QaStatus, detail: str = "") -> None:
        self.results.append(QaResult(check, status, detail))

    def _count(self, status: QaStatus) -> int:
        return sum(1 for r in self.results if r.status is status)

    @property
    def errors(self) -> list[QaResult]:
        return [r for r in self.results if r.status is QaStatus.FAIL]

    @property
    def warnings(self) -> list[QaResult]:
        return [r for r in self.results if r.status is QaStatus.WARN]

    @property
    def skipped(self) -> list[QaResult]:
        return [r for r in self.results if r.skipped]

    @property
    def status(self) -> QaStatus:
        """One word for the whole asset, which is what the `asset` row stores.

        A report of nothing but skips is `unchecked`, not `pass`. That
        distinction is the point: it stops "klm couldn't check this" from
        reading as "klm checked this and it was fine".
        """
        if self.errors:
            return QaStatus.FAIL
        if self.warnings:
            return QaStatus.WARN
        if self.results and all(r.skipped for r in self.results):
            return QaStatus.UNCHECKED
        return QaStatus.PASS

    @property
    def passed(self) -> bool:
        """Whether this asset may be approved. Warnings do not block."""
        return not self.errors

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "status": str(self.status),
                "results": [
                    {"check": r.check, "status": str(r.status), "detail": r.detail}
                    for r in self.results
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

_REQUIRED_SYMBOL_FIELDS = ("Reference", "Value")


def check_symbol(
    symbol: SExp,
    *,
    package: Package | None = None,
    category: str | None = None,
) -> QaReport:
    """Run the symbol checks (docs/08 §5)."""
    report = QaReport("symbol")
    pins = sym.iter_pins(symbol)
    properties = sym.properties(symbol)

    numbers = [pin.number for pin in pins if pin.number]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    report.add(
        "no duplicate pin numbers",
        QaStatus.FAIL if duplicates else QaStatus.PASS,
        f"repeated: {', '.join(duplicates)}" if duplicates else "",
    )

    if package is None:
        report.add("pin count matches the package", QaStatus.UNCHECKED, "package is unknown")
    elif len(pins) == package.pins:
        report.add("pin count matches the package", QaStatus.PASS, f"{len(pins)} pins")
    else:
        report.add(
            "pin count matches the package",
            QaStatus.FAIL,
            f"{len(pins)} pins, {package.name} has {package.pins}",
        )

    off_grid = [p.number or "?" for p in pins if not p.on_grid()]
    if not pins:
        report.add("pins on the 1.27 mm grid", QaStatus.FAIL, "symbol has no pins")
    else:
        report.add(
            "pins on the 1.27 mm grid",
            QaStatus.FAIL if off_grid else QaStatus.PASS,
            f"off-grid: {', '.join(off_grid)}" if off_grid else "",
        )

    # A two-terminal passive is *correctly* all-passive, so this only fires
    # where leaving every pin at the default actually means something is
    # missing — anything with more than two pins.
    untyped = [p.number or "?" for p in pins if p.type in ("passive", "unspecified")]
    if len(pins) <= 2:
        report.add("pin electrical types set", QaStatus.PASS, "two-terminal part")
    elif untyped:
        report.add(
            "pin electrical types set",
            QaStatus.WARN,
            f"{len(untyped)} pin(s) left passive/unspecified: {', '.join(untyped[:8])}",
        )
    else:
        report.add("pin electrical types set", QaStatus.PASS)

    rails = [p for p in pins if p.name.upper().rstrip("+-") in _RAIL_NAMES]
    mistyped = [p.number or "?" for p in rails if not p.type.startswith("power")]
    if not rails:
        report.add("power pins typed as power_in", QaStatus.UNCHECKED, "no recognisable rails")
    else:
        report.add(
            "power pins typed as power_in",
            QaStatus.WARN if mistyped else QaStatus.PASS,
            f"not power pins: {', '.join(mistyped)}" if mistyped else "",
        )

    missing = [name for name in _REQUIRED_SYMBOL_FIELDS if name not in properties]
    report.add(
        "required fields present",
        QaStatus.FAIL if missing else QaStatus.PASS,
        f"missing: {', '.join(missing)}" if missing else "",
    )

    expected = find_category(category)
    reference = properties.get("Reference", "").strip()
    if expected is None:
        report.add("designator matches the category", QaStatus.UNCHECKED, "no category")
    elif reference == expected.designator:
        report.add("designator matches the category", QaStatus.PASS, reference)
    else:
        report.add(
            "designator matches the category",
            QaStatus.WARN,
            f"{reference!r}, expected {expected.designator!r} for {expected.path}",
        )

    return report


_RAIL_NAMES = frozenset(
    {"VCC", "VDD", "VDDA", "VDDIO", "VBAT", "VIN", "VOUT", "VSS", "VSSA", "GND", "AGND", "DGND"}
)


# ---------------------------------------------------------------------------
# Footprints
# ---------------------------------------------------------------------------


def check_footprint(
    footprint: Document | SExp,
    *,
    symbol: SExp | None = None,
    package: Package | None = None,
) -> QaReport:
    """Run the footprint checks (docs/08 §5)."""
    report = QaReport("footprint")
    pads = [pad for pad in fp.iter_pads(footprint) if pad.plated]

    if not pads:
        report.add("footprint has pads", QaStatus.FAIL, "no plated pads found")
    else:
        report.add("footprint has pads", QaStatus.PASS, f"{len(pads)} pads")

    pad_numbers = sorted({pad.number for pad in pads if pad.number})
    if symbol is None:
        report.add("pad numbers match the symbol's pins", QaStatus.UNCHECKED, "no symbol given")
    else:
        pin_numbers = sorted({pin.number for pin in sym.iter_pins(symbol) if pin.number})
        if pin_numbers == pad_numbers:
            report.add("pad numbers match the symbol's pins", QaStatus.PASS)
        else:
            only_pins = sorted(set(pin_numbers) - set(pad_numbers))
            only_pads = sorted(set(pad_numbers) - set(pin_numbers))
            report.add(
                "pad numbers match the symbol's pins",
                QaStatus.FAIL,
                f"pins without pads: {only_pins or 'none'}; "
                f"pads without pins: {only_pads or 'none'}",
            )

    report.results.append(_courtyard_result(footprint))
    report.results.append(_silk_result(footprint, pads))
    report.results.append(_pin1_result(footprint, pads))
    report.results.append(_fab_result(footprint))
    report.results.append(_dimension_result(footprint, package))
    return report


def _extent(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _courtyard_result(footprint: Document | SExp) -> QaResult:
    """A missing courtyard is an error: DRC has nothing to space parts by."""
    items = fp.graphics_on(footprint, "F.CrtYd") + fp.graphics_on(footprint, "B.CrtYd")
    if not items:
        return QaResult("courtyard present and closed", QaStatus.FAIL, "no courtyard layer")

    endpoints: list[tuple[float, float]] = []
    for node in items:
        if node.name in ("fp_rect", "fp_circle", "fp_poly"):
            # A rectangle, circle or polygon is closed by construction.
            return QaResult("courtyard present and closed", QaStatus.PASS, node.name or "")
        points = fp.segment_points(node)
        endpoints.extend(points[:2])

    # Every vertex of a closed outline is shared by exactly two segments. An
    # odd one out is a gap, and a gap makes the courtyard useless to DRC.
    rounded = [(round(x, 3), round(y, 3)) for x, y in endpoints]
    dangling = [point for point in set(rounded) if rounded.count(point) % 2 != 0]
    if dangling:
        return QaResult(
            "courtyard present and closed",
            QaStatus.FAIL,
            f"{len(dangling)} unjoined endpoint(s), e.g. {dangling[0]}",
        )
    return QaResult("courtyard present and closed", QaStatus.PASS, f"{len(items)} segments")


def _silk_result(footprint: Document | SExp, pads: list[fp.PadInfo]) -> QaResult:
    """Silkscreen over a pad is a solder defect, but only ever a warning here.

    Warning rather than error because plenty of perfectly good vendor
    footprints do it by a few hundredths of a millimetre, and blocking on that
    would teach people to override the gate.
    """
    items = fp.graphics_on(footprint, "F.SilkS") + fp.graphics_on(footprint, "B.SilkS")
    if not items:
        return QaResult("silkscreen clear of pads", QaStatus.UNCHECKED, "no silkscreen")
    if not pads:
        return QaResult("silkscreen clear of pads", QaStatus.UNCHECKED, "no pads")

    for node in items:
        for x, y in fp.segment_points(node):
            for pad in pads:
                if pad.overlaps(x, y):
                    return QaResult(
                        "silkscreen clear of pads",
                        QaStatus.WARN,
                        f"silk vertex ({x}, {y}) sits on pad {pad.number}",
                    )
    return QaResult("silkscreen clear of pads", QaStatus.PASS)


def _pin1_result(footprint: Document | SExp, pads: list[fp.PadInfo]) -> QaResult:
    """Some marking distinguishing pin 1, for anything where it could be wrong."""
    if len(pads) <= 2:
        return QaResult("pin-1 marker present", QaStatus.PASS, "two-terminal part")
    silk = fp.graphics_on(footprint, "F.SilkS")
    fab = fp.graphics_on(footprint, "F.Fab")
    pad1 = next((pad for pad in pads if pad.number == "1"), None)
    if pad1 is None:
        return QaResult("pin-1 marker present", QaStatus.UNCHECKED, "no pad numbered 1")

    # A circle or an asymmetric fab outline near pad 1 is how every convention
    # does it; klm asks only that *something* is drawn on that side.
    for node in silk + fab:
        for x, y in fp.segment_points(node):
            if abs(x - pad1.x) < 2.0 and abs(y - pad1.y) < 2.0:
                return QaResult("pin-1 marker present", QaStatus.PASS, node.name or "")
    return QaResult("pin-1 marker present", QaStatus.WARN, "nothing drawn near pad 1")


def _fab_result(footprint: Document | SExp) -> QaResult:
    root = footprint.root if isinstance(footprint, Document) else footprint
    kinds = {
        node[1].value
        for node in root.find_all("fp_text")
        if len(node) >= 2 and hasattr(node[1], "value")
    }
    missing = sorted({"reference", "value"} - kinds)
    if missing:
        return QaResult(
            "fabrication reference and value present",
            QaStatus.WARN,
            f"missing: {', '.join(missing)}",
        )
    return QaResult("fabrication reference and value present", QaStatus.PASS)


def _dimension_result(footprint: Document | SExp, package: Package | None) -> QaResult:
    """Courtyard against the package body, where klm knows the body."""
    if package is None or package.chip is None:
        return QaResult(
            "overall size matches the package",
            QaStatus.UNCHECKED,
            "no body dimensions for this package",
        )
    points: list[tuple[float, float]] = []
    for node in fp.graphics_on(footprint, "F.CrtYd"):
        points.extend(fp.segment_points(node))
    extent = _extent(points)
    if extent is None:
        return QaResult("overall size matches the package", QaStatus.UNCHECKED, "no courtyard")

    width = extent[2] - extent[0]
    height = extent[3] - extent[1]
    if width < package.chip.length or height < package.chip.width:
        return QaResult(
            "overall size matches the package",
            QaStatus.WARN,
            f"courtyard {width:.2f}x{height:.2f} mm is smaller than the "
            f"{package.chip.length}x{package.chip.width} mm body",
        )
    if width > package.chip.length + BOUNDS_TOLERANCE * 2:
        return QaResult(
            "overall size matches the package",
            QaStatus.WARN,
            f"courtyard {width:.2f} mm is far larger than the "
            f"{package.chip.length} mm body",
        )
    return QaResult(
        "overall size matches the package", QaStatus.PASS, f"{width:.2f}x{height:.2f} mm"
    )


# ---------------------------------------------------------------------------
# 3D models
# ---------------------------------------------------------------------------

_STEP_HEADER = b"ISO-10303-21"


def check_model3d(
    data: bytes,
    *,
    footprint: Document | SExp | None = None,
    max_bytes: int = MAX_MODEL_BYTES,
) -> QaReport:
    """Run the 3D-model checks (docs/08 §5).

    Deliberately structural rather than geometric: klm parses the STEP header
    and counts entities, and leaves anything requiring a real kernel to
    FreeCAD, which owns that job in this design.
    """
    report = QaReport("model3d")

    if _STEP_HEADER in data[:512]:
        report.add("file parses as STEP", QaStatus.PASS)
    else:
        report.add(
            "file parses as STEP",
            QaStatus.FAIL,
            "no ISO-10303-21 header — this is not a STEP file",
        )
        return report

    if b"END-ISO-10303-21" not in data[-512:]:
        report.add("file is complete", QaStatus.FAIL, "no end marker; the file is truncated")
    else:
        report.add("file is complete", QaStatus.PASS)

    # A shell that never became a solid is the signature of a mesh with holes,
    # which renders in KiCad and is rejected by every mechanical tool.
    if b"MANIFOLD_SOLID_BREP" in data or b"BREP_WITH_VOIDS" in data:
        report.add("geometry is a closed solid", QaStatus.PASS)
    elif b"SHELL_BASED_SURFACE_MODEL" in data or b"OPEN_SHELL" in data:
        report.add(
            "geometry is a closed solid",
            QaStatus.WARN,
            "open shell rather than a solid; the source mesh was probably not watertight",
        )
    else:
        report.add("geometry is a closed solid", QaStatus.UNCHECKED, "no recognisable solid entity")

    size_mb = len(data) / (1024 * 1024)
    if len(data) > max_bytes:
        report.add(
            "file size within the threshold",
            QaStatus.WARN,
            f"{size_mb:.1f} MB exceeds {max_bytes / (1024 * 1024):.0f} MB",
        )
    else:
        report.add("file size within the threshold", QaStatus.PASS, f"{size_mb:.2f} MB")

    if footprint is None:
        report.add("model referenced by the footprint", QaStatus.UNCHECKED, "no footprint given")
    elif fp.model_paths(footprint):
        report.add("model referenced by the footprint", QaStatus.PASS)
    else:
        report.add(
            "model referenced by the footprint",
            QaStatus.WARN,
            "the footprint has no (model …) node, so KiCad will not show this",
        )

    return report
