"""What klm knows about physical packages (docs/08 §1, §3).

Two different kinds of knowledge live here, and keeping them apart matters:

* **Where KiCad's own footprint lives.** This is the preferred source for
  anything standard — those libraries are IPC-compliant, widely reviewed, and
  permissively licensed with an explicit exception for use in designs. A 0402
  resistor should get `Resistor_SMD:R_0402_1005Metric`, not a generated one.
* **Body dimensions, for the cases where klm has to generate.** Chip packages
  are the only ones klm generates, because they are the only ones whose land
  pattern is two rectangles derived from dimensions that are not in dispute:
  an 0402 is 1.0 x 0.5 mm by definition of the name.

Everything else — SOT, SOIC, QFN, LQFP — is named here so klm can *find* it in
KiCad's libraries and check a pin count against it, but never generated.
Reconstructing an IPC land pattern for a fine-pitch part from a table in a
docstring is exactly the kind of confidently-wrong output this project exists
to avoid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CHIP_PACKAGES",
    "PACKAGES",
    "ChipDimensions",
    "Package",
    "find_package",
    "normalize_package",
]


@dataclass(frozen=True, slots=True)
class ChipDimensions:
    """Nominal body and termination dimensions of a two-terminal chip, in mm.

    ``toe`` and ``side`` are the IPC-7351B density-level-B fillets used to turn
    those into a land pattern. The heel fillet is zero for chip components,
    which is why it is not a field.
    """

    length: float
    width: float
    termination: float
    toe: float
    side: float


#: Imperial chip sizes and their metric bodies. The names are the sizes: `0402`
#: *means* 40 x 20 mil. Termination lengths are the manufacturer-typical values
#: these packages have converged on.
CHIP_PACKAGES: dict[str, ChipDimensions] = {
    "0201": ChipDimensions(0.60, 0.30, 0.15, 0.15, 0.01),
    "0402": ChipDimensions(1.00, 0.50, 0.25, 0.25, 0.05),
    "0603": ChipDimensions(1.60, 0.80, 0.35, 0.35, 0.05),
    "0805": ChipDimensions(2.00, 1.25, 0.40, 0.35, 0.05),
    "1206": ChipDimensions(3.20, 1.60, 0.50, 0.35, 0.05),
    "1210": ChipDimensions(3.20, 2.50, 0.50, 0.35, 0.05),
    "1812": ChipDimensions(4.50, 3.20, 0.50, 0.35, 0.05),
    "2010": ChipDimensions(5.00, 2.50, 0.60, 0.35, 0.05),
    "2512": ChipDimensions(6.30, 3.20, 0.60, 0.35, 0.05),
}

#: Metric spelling → imperial, because both appear in real libraries.
_METRIC_ALIASES = {
    "0603": "0201",
    "1005": "0402",
    "1608": "0603",
    "2012": "0805",
    "3216": "1206",
    "3225": "1210",
    "4532": "1812",
    "5025": "2010",
    "6332": "2512",
}


@dataclass(frozen=True, slots=True)
class Package:
    """A physical package klm can recognise, and where to find its footprint."""

    name: str
    pins: int
    smd: bool = True
    kicad_library: str | None = None
    """KiCad footprint library, e.g. `Package_SO`. ``None`` when klm has none."""
    kicad_footprint: str | None = None
    """Footprint name within that library, or a `{}` template for passives."""
    chip: ChipDimensions | None = None
    """Set only for two-terminal chips, which are the ones klm can generate."""

    @property
    def generatable(self) -> bool:
        return self.chip is not None

    def kicad_id(self, designator: str = "") -> str | None:
        """The `Library:Footprint` KiCad knows this as, if klm knows one.

        For a chip package the answer depends on the *designator*, because
        KiCad files one land pattern under four names: an 0402 resistor and an
        0402 capacitor have identical geometry and live in different libraries.
        Following that convention costs nothing and buys interoperability with
        every existing project.
        """
        if self.chip is not None:
            return _chip_kicad_id(self.name, designator)
        if self.kicad_library is None or self.kicad_footprint is None:
            return None
        return f"{self.kicad_library}:{self.kicad_footprint}"


#: Designator → (KiCad library, footprint name prefix) for chip packages.
_CHIP_LIBRARIES = {
    "R": ("Resistor_SMD", "R"),
    "RN": ("Resistor_SMD", "R"),
    "C": ("Capacitor_SMD", "C"),
    "L": ("Inductor_SMD", "L"),
    "FB": ("Inductor_SMD", "L"),
    "D": ("Diode_SMD", "D"),
    "LED": ("LED_SMD", "LED"),
}


def _chip_kicad_id(name: str, designator: str) -> str | None:
    entry = _CHIP_LIBRARIES.get(designator.upper())
    if entry is None:
        return None
    library, prefix = entry
    return f"{library}:{prefix}_{name}_{_CHIP_METRIC[name]}Metric"


def _chip(name: str, dimensions: ChipDimensions) -> Package:
    return Package(name=name, pins=2, chip=dimensions)


_CHIP_METRIC = {imperial: metric for metric, imperial in _METRIC_ALIASES.items()}

_STANDARD: tuple[Package, ...] = (
    *(_chip(name, dims) for name, dims in CHIP_PACKAGES.items()),
    Package("SOT-23", 3, kicad_library="Package_TO_SOT_SMD", kicad_footprint="SOT-23"),
    Package("SOT-23-5", 5, kicad_library="Package_TO_SOT_SMD", kicad_footprint="SOT-23-5"),
    Package("SOT-23-6", 6, kicad_library="Package_TO_SOT_SMD", kicad_footprint="SOT-23-6"),
    Package("SOT-89-3", 3, kicad_library="Package_TO_SOT_SMD", kicad_footprint="SOT-89-3"),
    Package("SOT-223-3", 4, kicad_library="Package_TO_SOT_SMD", kicad_footprint="SOT-223"),
    Package("SOD-123", 2, kicad_library="Diode_SMD", kicad_footprint="D_SOD-123"),
    Package("SOD-323", 2, kicad_library="Diode_SMD", kicad_footprint="D_SOD-323"),
    Package("SMA", 2, kicad_library="Diode_SMD", kicad_footprint="D_SMA"),
    Package("SMB", 2, kicad_library="Diode_SMD", kicad_footprint="D_SMB"),
    Package(
        "SOIC-8", 8, kicad_library="Package_SO", kicad_footprint="SOIC-8_3.9x4.9mm_P1.27mm"
    ),
    Package(
        "SOIC-14", 14, kicad_library="Package_SO", kicad_footprint="SOIC-14_3.9x8.7mm_P1.27mm"
    ),
    Package(
        "SOIC-16", 16, kicad_library="Package_SO", kicad_footprint="SOIC-16_3.9x9.9mm_P1.27mm"
    ),
    Package("TSSOP-8", 8, kicad_library="Package_SO", kicad_footprint="TSSOP-8_4.4x3mm_P0.65mm"),
    Package(
        "TSSOP-16", 16, kicad_library="Package_SO", kicad_footprint="TSSOP-16_4.4x5mm_P0.65mm"
    ),
    Package(
        "QFN-16", 16, kicad_library="Package_DFN_QFN", kicad_footprint="QFN-16-1EP_3x3mm_P0.5mm"
    ),
    Package(
        "QFN-24", 24, kicad_library="Package_DFN_QFN", kicad_footprint="QFN-24-1EP_4x4mm_P0.5mm"
    ),
    Package(
        "QFN-32", 32, kicad_library="Package_DFN_QFN", kicad_footprint="QFN-32-1EP_5x5mm_P0.5mm"
    ),
    Package("LQFP-32", 32, kicad_library="Package_QFP", kicad_footprint="LQFP-32_7x7mm_P0.8mm"),
    Package("LQFP-48", 48, kicad_library="Package_QFP", kicad_footprint="LQFP-48_7x7mm_P0.5mm"),
    Package("LQFP-64", 64, kicad_library="Package_QFP", kicad_footprint="LQFP-64_10x10mm_P0.5mm"),
    Package("TO-220-3", 3, smd=False, kicad_library="Package_TO_SOT_THT",
            kicad_footprint="TO-220-3_Vertical"),
    Package("TO-92-3", 3, smd=False, kicad_library="Package_TO_SOT_THT",
            kicad_footprint="TO-92_Inline"),
    Package("DIP-8", 8, smd=False, kicad_library="Package_DIP",
            kicad_footprint="DIP-8_W7.62mm"),
    Package("DIP-16", 16, smd=False, kicad_library="Package_DIP",
            kicad_footprint="DIP-16_W7.62mm"),
)

PACKAGES: dict[str, Package] = {package.name.upper(): package for package in _STANDARD}

_SEPARATORS = re.compile(r"[\s_]+")
_METRIC_SUFFIX = re.compile(r"(?:METRIC)$")


def normalize_package(name: str) -> str:
    """Fold a package spelling to the key :data:`PACKAGES` uses.

    Real libraries write `SOT23`, `SOT-23`, `sot_23` and `0402 (1005 Metric)`
    for the same thing. Metric chip spellings resolve to their imperial name so
    that `1005` and `0402` are one package rather than two.
    """
    folded = _SEPARATORS.sub("", name.strip()).upper()
    folded = folded.replace("(", "").replace(")", "")
    folded = _METRIC_SUFFIX.sub("", folded)

    if folded in _METRIC_ALIASES and folded not in CHIP_PACKAGES:
        # `1005` is unambiguous; `0603` is both an imperial size and the metric
        # name of an 0201, so the imperial reading wins — it is what a library
        # writing bare digits almost always means.
        return _METRIC_ALIASES[folded]
    if folded in CHIP_PACKAGES:
        return folded

    # `SOT23-5` and `SOT-23-5` differ only in a hyphen klm does not care about.
    stripped = folded.replace("-", "")
    for key in PACKAGES:
        if key.replace("-", "") == stripped:
            return key
    return folded


def find_package(name: str | None) -> Package | None:
    """Resolve a package name, or ``None`` when klm has never heard of it.

    Unknown is a normal answer: klm's package table names the packages it can
    *act* on, not every package that exists. A part in an unlisted package is
    fine, it just gets no template and no pin-count check.
    """
    if not name:
        return None
    return PACKAGES.get(normalize_package(name))
