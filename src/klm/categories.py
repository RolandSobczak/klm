"""The category taxonomy.

A category is a path — `Passive/Capacitor/Ceramic`, `IC/Power/Regulator/LDO` —
and it is what lets klm hold a part to a standard without being told part by
part: a resistor's `Value` is in ohms, wants a tolerance and a power rating,
and gets an `R` designator. Nothing else in klm knows those facts.

The tree below is deliberately shallow and incomplete. A taxonomy that tries to
name every component ends up with one part in each leaf; this one names the
distinctions that change how a part is checked, and lets anything else sit at a
parent node (docs/02 §2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from klm.units import OHM

__all__ = [
    "CATEGORIES",
    "Category",
    "designator_prefix",
    "find_category",
    "known_paths",
    "value_unit",
]


@dataclass(frozen=True)
class Category:
    """One node of the taxonomy, and what it implies about a part."""

    path: str
    designator: str = "U"
    """Reference designator prefix, per IEEE 315."""
    unit: str | None = None
    """Unit of the part's principal `Value`, if it has a numeric one."""
    expects: tuple[str, ...] = field(default_factory=tuple)
    """Fields a part in this category should carry, checked by lint rule V004."""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def parents(self) -> tuple[str, ...]:
        parts = self.path.split("/")
        return tuple("/".join(parts[: i + 1]) for i in range(len(parts) - 1))


_TREE: tuple[Category, ...] = (
    Category("Passive", designator="U"),
    Category("Passive/Resistor", "R", OHM, ("Tolerance", "Power")),
    Category("Passive/Resistor/Array", "RN", OHM, ("Tolerance", "Power")),
    Category("Passive/Resistor/Shunt", "R", OHM, ("Tolerance", "Power")),
    Category("Passive/Resistor/Potentiometer", "RV", OHM, ("Tolerance",)),
    Category("Passive/Resistor/NTC", "TH", OHM, ("Tolerance",)),
    Category("Passive/Capacitor", "C", "F", ("Tolerance", "Voltage")),
    Category("Passive/Capacitor/Ceramic", "C", "F", ("Tolerance", "Voltage", "Dielectric")),
    Category("Passive/Capacitor/Electrolytic", "C", "F", ("Tolerance", "Voltage")),
    Category("Passive/Capacitor/Tantalum", "C", "F", ("Tolerance", "Voltage")),
    Category("Passive/Capacitor/Film", "C", "F", ("Tolerance", "Voltage")),
    Category("Passive/Inductor", "L", "H", ("Tolerance",)),
    Category("Passive/Inductor/Ferrite", "FB", OHM),
    Category("Passive/Crystal", "Y", "Hz", ("Tolerance",)),
    Category("Passive/Crystal/Oscillator", "X", "Hz"),
    Category("Passive/Fuse", "F", "A", ("Voltage",)),
    Category("Diode", "D"),
    Category("Diode/Rectifier", "D", expects=("Voltage",)),
    Category("Diode/Schottky", "D", expects=("Voltage",)),
    Category("Diode/Zener", "D", "V"),
    Category("Diode/TVS", "D", "V"),
    Category("Diode/LED", "D"),
    Category("Transistor", "Q"),
    Category("Transistor/BJT", "Q"),
    Category("Transistor/MOSFET", "Q"),
    Category("Transistor/IGBT", "Q"),
    Category("IC", "U"),
    Category("IC/Microcontroller", "U"),
    Category("IC/Memory", "U"),
    Category("IC/Logic", "U"),
    Category("IC/Amplifier", "U"),
    Category("IC/Interface", "U"),
    Category("IC/Sensor", "U"),
    Category("IC/Power", "U"),
    Category("IC/Power/Regulator", "U"),
    Category("IC/Power/Regulator/LDO", "U"),
    Category("IC/Power/Regulator/Switching", "U"),
    Category("IC/Power/Reference", "U"),
    Category("IC/Optocoupler", "U"),
    Category("Connector", "J"),
    Category("Connector/Header", "J"),
    Category("Connector/USB", "J"),
    Category("Connector/Terminal", "J"),
    Category("Connector/Socket", "J"),
    Category("Electromechanical", "U"),
    Category("Electromechanical/Switch", "SW"),
    Category("Electromechanical/Relay", "K"),
    Category("Electromechanical/Motor", "M"),
    Category("Electromechanical/Speaker", "LS"),
    Category("Module", "U"),
    Category("Module/RF", "U"),
    Category("Module/Display", "DS"),
    Category("Mechanical", "MP"),
    Category("Mechanical/Mounting", "MP"),
    Category("Mechanical/Enclosure", "MP"),
    Category("Test", "TP"),
)

#: Indexed by lowercased path, because categories are typed by humans.
CATEGORIES: dict[str, Category] = {category.path.lower(): category for category in _TREE}


def known_paths() -> tuple[str, ...]:
    return tuple(category.path for category in _TREE)


def find_category(path: str | None) -> Category | None:
    """Resolve a category path, falling back to its nearest known ancestor.

    `IC/Power/Regulator/Buck` is not in the tree, but `IC/Power/Regulator` is,
    and everything klm needs to know about the part is the same either way.
    Growing a leaf should never be a prerequisite for adding a part.
    """
    if not path:
        return None
    segments = path.strip("/").split("/")
    while segments:
        found = CATEGORIES.get("/".join(segments).lower())
        if found is not None:
            return found
        segments.pop()
    return None


def designator_prefix(path: str | None) -> str:
    """Designator prefix for a category, defaulting to the one for an IC."""
    category = find_category(path)
    return category.designator if category is not None else "U"


def value_unit(path: str | None) -> str | None:
    """Unit a part's `Value` should be in, or ``None`` if it has no numeric value.

    An IC's `Value` is its MPN, so ``None`` here also means "do not try to
    parse this as a number" — which is what keeps lint rule V002 off ICs.
    """
    category = find_category(path)
    return category.unit if category is not None else None
