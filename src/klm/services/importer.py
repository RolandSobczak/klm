"""Importing an existing `.kicad_sym` library into the catalog.

This is the on-ramp: nobody starts klm with an empty library, they start it
with years of accumulated symbols whose fields are spelled six ways. Import
takes them as they are — no renaming, no normalising, no rejection — and files
them as `draft` parts. Cleaning up is `klm lint --fix`'s job, and separating
the two means an import that fails halfway has changed nothing about how the
data reads (docs/05 §7).

Two decisions worth stating:

* **Every symbol is preserved verbatim as an asset.** The stored bytes are the
  original symbol, not a reconstruction, so nothing is lost to klm's
  understanding of the format being incomplete.
* **A symbol that already carries `KLM_ID` keeps it.** Re-importing a library
  klm generated updates those parts rather than duplicating them, which is what
  makes import safe to run twice.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from klm import fields as field_schema
from klm import ids
from klm.config import Config, FieldConfig
from klm.kicad import symbols as sym
from klm.kicad.sexpr import SExp, dumps_canonical, loads
from klm.model import Confidence, Parameter, Part, PartStatus, SourceKind
from klm.services.catalog import get_part, save_part
from klm.store.assets import AssetKind, AssetStore

__all__ = ["ImportReport", "ImportedSymbol", "import_symbol_library"]

UNKNOWN_MANUFACTURER = "Unknown"


@dataclass
class ImportedSymbol:
    name: str
    klm_id: str
    created: bool
    """False when an existing `KLM_ID` matched a part already in the catalog."""


@dataclass
class ImportReport:
    source: Path
    imported: list[ImportedSymbol] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(symbol name, reason)`` — reported, never guessed at."""

    @property
    def created(self) -> int:
        return sum(1 for item in self.imported if item.created)

    @property
    def updated(self) -> int:
        return sum(1 for item in self.imported if not item.created)

    @property
    def ok(self) -> bool:
        return not self.skipped


def import_symbol_library(
    conn: sqlite3.Connection,
    store: AssetStore,
    path: Path,
    *,
    config: Config | None = None,
    status: PartStatus = PartStatus.DRAFT,
    category: str | None = None,
) -> ImportReport:
    """Import every symbol in a `.kicad_sym` file as a part."""
    schema = (config or Config()).fields
    report = ImportReport(source=path)

    with open(path, encoding="utf-8", newline="") as handle:
        document = loads(handle.read())

    for symbol in sym.extract_symbols(document):
        name = sym.symbol_name(symbol)
        if name is None:  # pragma: no cover - extract_symbols only yields named nodes
            continue
        if _is_derived(symbol):
            # KiCad's `extends` symbols inherit their parent's graphics. Until
            # klm models that relationship, importing one produces a part whose
            # symbol renders nothing — worse than not importing it.
            report.skipped.append((name, "derived symbol (extends) is not yet supported"))
            continue
        try:
            imported = _import_one(conn, store, symbol, name, schema, status, category)
        except ValueError as exc:
            report.skipped.append((name, str(exc)))
            continue
        report.imported.append(imported)

    return report


def _is_derived(symbol: SExp) -> bool:
    return symbol.find("extends", recursive=False) is not None


def _import_one(
    conn: sqlite3.Connection,
    store: AssetStore,
    symbol: SExp,
    name: str,
    schema: FieldConfig,
    status: PartStatus,
    category: str | None,
) -> ImportedSymbol:
    raw = sym.properties(symbol)
    resolved = _resolve_fields(raw, schema)

    klm_id = resolved.get(field_schema.KLM_ID, "").strip()
    if klm_id and not ids.is_valid(klm_id):
        raise ValueError(f"KLM_ID {klm_id!r} is not a valid identifier")
    existing = get_part(conn, klm_id) if klm_id else None
    if not klm_id:
        klm_id = ids.new_id()

    symbol_hash = store.add_bytes(dumps_canonical(symbol).encode("utf-8"), AssetKind.SYMBOL)

    part = Part(
        klm_id=klm_id,
        # The symbol name is the fallback MPN: for a library of hand-drawn
        # parts it is usually the closest thing to one that exists.
        mpn=resolved.get("MPN", "").strip() or resolved.get("Value", "").strip() or name,
        manufacturer=resolved.get("Manufacturer", "").strip() or UNKNOWN_MANUFACTURER,
        description=resolved.get("Description", "").strip(),
        category=category or (existing.category if existing else None),
        package=resolved.get("Package", "").strip() or None,
        status=existing.status if existing else status,
        datasheet_url=_datasheet(resolved),
        symbol_hash=symbol_hash,
        footprint_hash=existing.footprint_hash if existing else None,
        model3d_hash=existing.model3d_hash if existing else None,
        parameters=_parameters(raw, schema, name),
        created_at=existing.created_at if existing else None,
    )
    save_part(conn, part)
    return ImportedSymbol(name=name, klm_id=klm_id, created=existing is None)


def _resolve_fields(raw: dict[str, str], schema: FieldConfig) -> dict[str, str]:
    """Map the symbol's field names onto canonical ones for reading.

    The symbol itself is untouched — this only decides which value klm reads
    for `MPN`. A field whose canonical name is already present does not
    displace it: an explicit `MPN` beats an aliased `Part Number`.
    """
    resolved: dict[str, str] = {}
    for name, value in raw.items():
        canonical = schema.canonical(name) or name
        if canonical in resolved and name != canonical:
            continue
        resolved[canonical] = value
    return resolved


def _datasheet(resolved: dict[str, str]) -> str | None:
    url = resolved.get("Datasheet", "").strip()
    # KiCad writes `~` for "no datasheet"; storing it would produce a link that
    # looks present and resolves to nothing.
    return None if url in ("", "~") else url


def _parameters(raw: dict[str, str], schema: FieldConfig, symbol_name: str) -> list[Parameter]:
    """Keep every field klm has no column for, so nothing is lost on import.

    They are recorded as user-sourced parameters: the user wrote them, and the
    provenance model exists precisely so that a later datasheet reading can
    disagree without either being discarded (docs/02 §4).
    """
    modelled = {
        "Reference",
        "Value",
        "Footprint",
        "Datasheet",
        "MPN",
        "Manufacturer",
        "Description",
        "Package",
        field_schema.KLM_ID,
    }
    parameters: list[Parameter] = []
    for name, value in raw.items():
        if field_schema.is_kicad_internal(name):
            # Library-browser metadata, not a property of the component. It
            # stays in the symbol asset, where KiCad reads it from.
            continue
        canonical = schema.canonical(name) or name
        if canonical in modelled or not value.strip():
            continue
        parameters.append(
            Parameter(
                name=canonical,
                source=SourceKind.USER,
                value_text=value.strip(),
                source_ref=f"imported from symbol {symbol_name}",
                confidence=Confidence.MEDIUM,
            )
        )
    return parameters
