"""Finding a substitute, and being honest about what "compatible" means.

The task is the one from docs/11 §8: a part goes to zero and something else
has to go on the board this week. The split is the point —

* **klm decides pin compatibility mechanically**, from the catalog's own
  footprint and symbol data. Same land pattern, same pin numbers, same
  electrical types, same pin names. That is a fact, and it is checkable
  without a model, without a network and without an opinion.
* **A human (or the agent) argues electrical equivalence.** Whether a 3.3 V
  regulator with the same pinout is *the same part for this circuit* is not a
  question geometry answers.

Three answers, and the third is the one that matters:

* `compatible` — nothing klm can compare differs.
* `differs` — checkable, and here is exactly what differs. Never a bare "no":
  a difference a human can overrule is worth more than a verdict they cannot
  see inside.
* `unchecked` — an asset is missing, so the comparison did not run. Same rule
  as the asset QA gate (docs/08 §5): a check that could not run is `unchecked`,
  never `pass`. A green "compatible" that means "I could not look" is how the
  wrong part reaches a board.

Names are compared, not just numbers and types. Two parts can share a footprint
and a pin-type map while pin 3 is `EN` on one and `GND` on the other — both
inputs, both on the same pad, and one of them destroys the board.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from klm.kicad import footprints as fp
from klm.kicad.sexpr import SExp, loads
from klm.kicad.symbols import extract_symbols, iter_pins
from klm.model import Part, PartStatus
from klm.services.catalog import list_parts
from klm.services.offers import list_offers
from klm.services.stock import where
from klm.store.assets import AssetError, AssetKind, AssetStore

__all__ = [
    "Candidate",
    "Compatibility",
    "compare",
    "find_substitutes",
    "pin_map",
]

STATUS_COMPATIBLE = "compatible"
STATUS_DIFFERS = "differs"
STATUS_UNCHECKED = "unchecked"

#: Land patterns are drawn to the same nominal dimensions by different hands,
#: and the last decimal disagrees often enough that exact comparison finds
#: nothing. 50 µm is far below any real difference in pad geometry.
PAD_TOLERANCE = 0.05


@dataclass(frozen=True)
class Compatibility:
    """Whether two parts can swap, mechanically speaking."""

    status: str
    footprint: str
    """`same` | `differs` | `unchecked`."""
    pins: str
    differences: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Why something could not be checked."""

    @property
    def compatible(self) -> bool:
        return self.status == STATUS_COMPATIBLE

    def explain(self) -> list[str]:
        return [*self.differences, *self.notes]


@dataclass(frozen=True)
class Candidate:
    """A part that might do instead, with the mechanical verdict attached."""

    part: Part
    compatibility: Compatibility
    stock: int = 0
    unit_price: float | None = None
    currency: str | None = None
    suppliers: tuple[str, ...] = ()

    @property
    def buyable(self) -> bool:
        return bool(self.suppliers)


def pin_map(document: SExp) -> dict[str, tuple[str, str]]:
    """`pin number -> (folded name, electrical type)` for a symbol.

    Keyed by number because that is what the board connects. A symbol whose
    pins carry no numbers is a symbol this comparison cannot speak about, and
    the caller treats an empty map as unchecked rather than as "no pins".
    """
    mapping: dict[str, tuple[str, str]] = {}
    for pin in iter_pins(document):
        if not pin.number.strip():
            continue
        mapping[pin.number.strip()] = (_fold(pin.name), pin.type.strip().lower())
    return mapping


def compare(store: AssetStore, left: Part, right: Part) -> Compatibility:
    """Compare two parts on the only axes klm can check."""
    differences: list[str] = []
    notes: list[str] = []

    footprint = _compare_footprints(store, left, right, differences, notes)
    pins = _compare_pins(store, left, right, differences, notes)

    if footprint == STATUS_UNCHECKED or pins == STATUS_UNCHECKED:
        status = STATUS_UNCHECKED
    elif differences:
        status = STATUS_DIFFERS
    else:
        status = STATUS_COMPATIBLE
    return Compatibility(status, footprint, pins, differences, notes)


def _compare_footprints(
    store: AssetStore, left: Part, right: Part, differences: list[str], notes: list[str]
) -> str:
    if not left.footprint_hash or not right.footprint_hash:
        notes.append("one of the parts has no footprint, so the land pattern was not compared")
        return STATUS_UNCHECKED
    if left.footprint_hash == right.footprint_hash:
        return "same"

    first = _pads(store, left.footprint_hash)
    second = _pads(store, right.footprint_hash)
    if first is None or second is None:
        notes.append("a footprint is not in the asset store, so the land pattern was not compared")
        return STATUS_UNCHECKED
    if first == second:
        return "same"

    if len(first) != len(second):
        differences.append(f"pad count: {len(first)} vs {len(second)}")
    else:
        differences.append("same pad count, different pad geometry")
    return STATUS_DIFFERS


def _compare_pins(
    store: AssetStore, left: Part, right: Part, differences: list[str], notes: list[str]
) -> str:
    first = _symbol_pins(store, left)
    second = _symbol_pins(store, right)
    if first is None or second is None:
        notes.append("one of the parts has no readable symbol, so the pinout was not compared")
        return STATUS_UNCHECKED
    if not first or not second:
        notes.append("a symbol has no numbered pins, so the pinout was not compared")
        return STATUS_UNCHECKED

    missing = sorted(set(first) - set(second), key=_pin_order)
    extra = sorted(set(second) - set(first), key=_pin_order)
    if missing:
        differences.append(f"pin(s) {', '.join(missing)} have no counterpart")
    if extra:
        differences.append(f"extra pin(s) {', '.join(extra)}")

    for number in sorted(set(first) & set(second), key=_pin_order):
        name, kind = first[number]
        other_name, other_kind = second[number]
        if name != other_name:
            # The dangerous case: same pad, same electrical type, different job.
            differences.append(f"pin {number}: {name!r} vs {other_name!r}")
        elif kind != other_kind:
            differences.append(f"pin {number} ({name}): {kind} vs {other_kind}")

    return STATUS_DIFFERS if differences else "same"


def find_substitutes(
    conn: sqlite3.Connection,
    store: AssetStore,
    part: Part,
    *,
    include_differing: bool = False,
    limit: int = 20,
) -> list[Candidate]:
    """Catalog parts that could stand in for this one.

    The search is deliberately narrow — same package, or the same category —
    because a candidate klm cannot compare mechanically is not a substitute,
    it is a suggestion. Widening it is the agent's job, and its proposals come
    back through the review queue like everything else.

    Deprecated parts are excluded: they are what you are substituting *away*
    from. Ordering is compatible first, then by what is actually on the shelf.
    """
    candidates: list[Candidate] = []
    for other in list_parts(conn):
        if other.klm_id == part.klm_id or other.status is PartStatus.DEPRECATED:
            continue
        if not _plausible(part, other):
            continue
        compatibility = compare(store, part, other)
        if compatibility.status == STATUS_DIFFERS and not include_differing:
            continue
        candidates.append(_describe(conn, other, compatibility))

    candidates.sort(key=lambda c: (not c.compatibility.compatible, -c.stock, c.part.mpn))
    return candidates[:limit]


def _describe(conn: sqlite3.Connection, part: Part, compatibility: Compatibility) -> Candidate:
    offers = list_offers(conn, klm_id=part.klm_id)
    prices = [(o.unit_price(1), o.currency, o.supplier) for o in offers if o.unit_price(1)]
    cheapest = min(prices, key=lambda item: item[0] or 0.0) if prices else None
    return Candidate(
        part=part,
        compatibility=compatibility,
        stock=sum(item.quantity for item in where(conn, part.klm_id)),
        unit_price=cheapest[0] if cheapest else None,
        currency=cheapest[1] if cheapest else None,
        suppliers=tuple(sorted({offer.supplier for offer in offers})),
    )


def _plausible(part: Part, other: Part) -> bool:
    """Cheap filter before the expensive comparison."""
    if part.package and other.package:
        return _fold(part.package) == _fold(other.package)
    if part.category and other.category:
        return part.category == other.category
    return False


def _pads(store: AssetStore, content_hash: str) -> tuple[tuple[int, ...], ...] | None:
    try:
        document = loads(store.read_text(content_hash, AssetKind.FOOTPRINT)).root
    except (AssetError, OSError, ValueError):
        return None
    steps = max(PAD_TOLERANCE, 1e-6)
    return tuple(
        sorted(
            (
                round(pad.x / steps),
                round(pad.y / steps),
                round(pad.width / steps),
                round(pad.height / steps),
            )
            for pad in fp.iter_pads(document)
            if pad.plated
        )
    )


def _symbol_pins(store: AssetStore, part: Part) -> dict[str, tuple[str, str]] | None:
    """The part's pinout, or ``None`` when there is nothing to read.

    A library file holds several symbols; the pins of the first are the ones
    that describe the part, the same reading the QA gate takes.
    """
    if not part.symbol_hash:
        return None
    try:
        symbols = extract_symbols(loads(store.read_text(part.symbol_hash, AssetKind.SYMBOL)))
    except (AssetError, OSError, ValueError):
        return None
    return pin_map(symbols[0]) if symbols else None


def _pin_order(number: str) -> tuple[int, str]:
    return (int(number), "") if number.isdigit() else (10**9, number)


def _fold(text: str) -> str:
    return " ".join(text.split()).strip().casefold()
