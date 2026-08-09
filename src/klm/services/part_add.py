"""`klm part add` — one command from "I need this part" to a reviewable draft.

The command is a composition, not new logic: it resolves what the suppliers
know, creates the part, runs the asset pipeline, and checks the result. Each of
those already exists and is tested on its own; what this module adds is the
order and the honesty about what did not work.

Two rules it inherits from everything else in klm:

* **The result is a `draft`.** A part that arrived automatically has not been
  looked at by a human, and only `approved` parts are usable in a design. The
  pipeline produces something worth reviewing; it does not decide the review.
* **A gap is reported, not filled.** A part with no manufacturer gets no
  manufacturer, and the report says so. Inventing "Unknown" and moving on is how
  a catalog fills with parts nobody can order.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass, field

from klm import ids
from klm.assets.kicad_libs import KicadLibraries
from klm.categories import find_category
from klm.kicad import symbols as sym
from klm.kicad.sexpr import dumps_canonical, loads
from klm.model import Confidence, Offer, Parameter, Part, PartStatus, SourceKind
from klm.services.assets import AcquisitionReport, acquire_assets
from klm.services.catalog import find_by_mpn, save_part
from klm.services.offers import save_offer
from klm.store.assets import AssetError, AssetKind, AssetStore
from klm.suppliers.base import SupplierAdapter, SupplierError
from klm.suppliers.lcsc import is_lcsc_pn, product_url
from klm.suppliers.matching import match_mpn
from klm.units import ValueParseError, format_quantity, parse_value

__all__ = ["AddReport", "add_part"]


@dataclass
class AddReport:
    part: Part
    created: bool
    assets: AcquisitionReport | None = None
    offers: list[Offer] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Things the user should know: what was filled in, and what was not."""

    @property
    def ok(self) -> bool:
        return self.assets is not None and self.assets.ok


def add_part(
    conn: sqlite3.Connection,
    store: AssetStore,
    *,
    mpn: str,
    manufacturer: str = "",
    category: str | None = None,
    package: str | None = None,
    value: str = "",
    description: str = "",
    datasheet: str | None = None,
    fields: dict[str, str] | None = None,
    lcsc: str | None = None,
    adapters: dict[str, SupplierAdapter] | None = None,
    libraries: KicadLibraries | None = None,
    status: PartStatus = PartStatus.DRAFT,
) -> AddReport:
    """Create a part, source what can be sourced, and acquire its assets."""
    mpn = mpn.strip()
    if not mpn:
        raise ValueError("a part needs an MPN")

    resolved = _from_suppliers(adapters or {}, mpn, manufacturer)
    report_notes: list[str] = []

    if not manufacturer and resolved is not None and resolved.manufacturer:
        manufacturer = resolved.manufacturer
        report_notes.append(f"manufacturer {manufacturer!r} from {resolved.supplier}")
    if not description and resolved is not None and resolved.description:
        description = resolved.description
        report_notes.append(f"description from {resolved.supplier}")
    if not datasheet and resolved is not None and resolved.datasheet_url:
        datasheet = resolved.datasheet_url

    if not manufacturer:
        # Deliberately left blank rather than filled with a placeholder: lint
        # rule S001 will say so, which is a question a human can answer.
        report_notes.append("manufacturer is unknown — S001 will report it")

    existing = find_by_mpn(conn, manufacturer, mpn) if manufacturer else None
    part = Part(
        klm_id=existing.klm_id if existing else ids.new_id(),
        mpn=mpn,
        manufacturer=manufacturer,
        description=description,
        category=category or (existing.category if existing else None),
        package=package or (existing.package if existing else None),
        status=existing.status if existing else status,
        datasheet_url=datasheet or (existing.datasheet_url if existing else None),
        symbol_hash=existing.symbol_hash if existing else None,
        footprint_hash=existing.footprint_hash if existing else None,
        model3d_hash=existing.model3d_hash if existing else None,
        parameters=list(existing.parameters) if existing else [],
        created_at=existing.created_at if existing else None,
    )
    part = save_part(conn, part)

    report = AddReport(part=part, created=existing is None, notes=report_notes)

    report.assets = acquire_assets(conn, store, part, libraries=libraries)
    updated = _write_symbol_fields(store, part, value, category, fields or {})
    if updated is not None:
        part.symbol_hash = updated
        part.parameters = _merge_parameters(part, fields or {})
        part = save_part(conn, part)
        report.part = part

    if resolved is not None:
        resolved.klm_id = part.klm_id
        report.offers.append(save_offer(conn, resolved))
    if lcsc:
        report.offers.append(_lcsc_offer(conn, part, lcsc))

    return report


def _from_suppliers(
    adapters: dict[str, SupplierAdapter], mpn: str, manufacturer: str
) -> Offer | None:
    """The best offer any configured supplier has for this MPN.

    Only a confident match is used to fill in blanks. Filling a part's
    manufacturer from a listing klm is unsure about would put a wrong fact into
    the catalog with no record that it was a guess.
    """
    best: tuple[int, Offer] | None = None
    order = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}

    for name, adapter in sorted(adapters.items()):
        try:
            candidates = adapter.resolve_mpn(mpn, manufacturer or None)
        except SupplierError:
            # A supplier being down must not stop a part being created; the
            # part is the deliverable and the offer is a nice-to-have.
            continue
        for candidate in candidates:
            result = match_mpn(
                mpn,
                manufacturer,
                candidate.mpn or candidate.supplier_pn,
                candidate.manufacturer or "",
            )
            if result is None or not result.auto_link:
                continue
            candidate.match_confidence = result.confidence
            candidate.supplier = candidate.supplier or name
            rank = order[result.confidence]
            if best is None or rank > best[0]:
                best = (rank, candidate)
    return best[1] if best else None


def _normalize_value(value: str, category: str | None) -> str:
    """A display value in canonical form, where the category implies a unit.

    Normalising here rather than leaving it to `klm lint --fix` means
    `klm part add --value 0.1uF` stores `100nF` and lints clean the moment it
    exists, instead of being reported by V001 against a part klm just wrote.
    """
    node_category = find_category(category)
    if node_category is None or not node_category.unit:
        return value
    # Unparseable is the user's to resolve; lint rule V002 reports it.
    # Guessing at what they meant is the one thing klm will not do here.
    with contextlib.suppress(ValueParseError):
        return format_quantity(parse_value(value, unit=node_category.unit))
    return value


def _write_symbol_fields(
    store: AssetStore,
    part: Part,
    value: str,
    category: str | None,
    fields: dict[str, str],
) -> str | None:
    """Set `Value` and any `--field` on the part's symbol.

    Returns the new symbol hash, or ``None`` when nothing changed. Field names
    go on verbatim: `klm lint` rule S002 renames an alias to its canonical
    spelling, and doing it in two places would mean two answers when they
    disagree.
    """
    if part.symbol_hash is None:
        return None

    updates: dict[str, str] = {}
    text = value.strip() or (part.mpn if not _is_passive(category) else "")
    if text:
        updates["Value"] = _normalize_value(text, category)
    updates.update({name: text for name, text in fields.items() if text.strip()})
    if not updates:
        return None

    try:
        document = loads(store.read_text(part.symbol_hash, AssetKind.SYMBOL))
    except (AssetError, ValueError):
        return None
    symbols = sym.extract_symbols(document)
    if not symbols:
        return None

    symbol = symbols[0]
    existing = sym.properties(symbol)
    if all(existing.get(name) == text for name, text in updates.items()):
        return None
    for name, text in updates.items():
        sym.set_property(symbol, name, text, hidden=name != "Value")
    return store.add_bytes(dumps_canonical(symbol).encode("utf-8"), AssetKind.SYMBOL)


def _merge_parameters(part: Part, fields: dict[str, str]) -> list[Parameter]:
    """Record `--field` values as user-sourced parameters as well as symbol fields.

    Both, not either: the symbol field is what KiCad and the BOM read, and the
    parameter is what klm compares against a datasheet later. They are the same
    fact from two sources, which is exactly what the provenance model is for
    (docs/02 §4).
    """
    keep = [p for p in part.parameters if p.name not in fields or p.source is not SourceKind.USER]
    return [
        *keep,
        *(
            Parameter(
                name=name,
                source=SourceKind.USER,
                value_text=text.strip(),
                source_ref="klm part add",
                confidence=Confidence.HIGH,
            )
            for name, text in fields.items()
            if text.strip()
        ),
    ]


def _is_passive(category: str | None) -> bool:
    resolved = find_category(category)
    return resolved is not None and resolved.unit is not None


def _lcsc_offer(conn: sqlite3.Connection, part: Part, supplier_pn: str) -> Offer:
    """Record an LCSC number given on the command line.

    High confidence with no price: the user typed this number for this part, so
    the *link* is as certain as it gets, while the price is simply not known
    yet. Those are different facts and the offer records them separately
    (docs/adr/0009).
    """
    pn = supplier_pn.strip().upper()
    if not is_lcsc_pn(pn):
        raise ValueError(f"{supplier_pn!r} is not an LCSC part number (expected C12345)")
    offer = Offer(
        supplier="lcsc",
        supplier_pn=pn,
        klm_id=part.klm_id,
        mpn=part.mpn,
        manufacturer=part.manufacturer or None,
        description=part.description,
        currency="USD",
        price_breaks=[],
        url=product_url(pn),
        datasheet_url=product_url(pn),
        match_confidence=Confidence.HIGH,
    )
    return save_offer(conn, offer)
