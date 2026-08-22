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
* **A symbol that already carries `KLM_ID` keeps it**, and one that does not is
  matched on manufacturer + MPN. Re-importing a library updates those parts
  rather than duplicating them, and importing the same symbol from every
  project that copied it converges on one part.
* **A part is its symbol, its footprint and its model.** Given somewhere to look
  (`libraries`), import also takes the footprint the symbol's `Footprint` field
  names and the 3D model that footprint references, because a catalog holding
  symbols alone is one whose parts stop working the moment they leave this
  machine. What could not be found is *reported* — an unresolved footprint is
  the thing the user has to know about, so it is never silently skipped.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from klm import fields as field_schema
from klm import ids
from klm.assets.kicad_libs import KicadLibraries
from klm.assets.qa import check_footprint, check_model3d
from klm.config import Config, FieldConfig
from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.sexpr import Atom, SExp, dumps_canonical, loads
from klm.model import Confidence, Parameter, Part, PartStatus, SourceKind
from klm.services.assets import AssetOrigin, register_asset
from klm.services.catalog import find_by_mpn, get_part, save_part
from klm.services.register import MODELS_VAR
from klm.store.assets import AssetKind, AssetStore

__all__ = ["ImportReport", "ImportedSymbol", "import_symbol", "import_symbol_library"]

UNKNOWN_MANUFACTURER = "Unknown"

#: Tried in order when a footprint names a model klm cannot use as it stands.
_STEP_SUFFIXES = (".step", ".stp", ".STEP", ".STP")


@dataclass
class ImportedSymbol:
    name: str
    klm_id: str
    created: bool
    """False when the symbol matched a part already in the catalog."""
    footprint: str | None = None
    """The `Library:Name` whose land pattern was taken, if one was found."""
    model3d: str | None = None


@dataclass
class ImportReport:
    source: Path
    imported: list[ImportedSymbol] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(symbol name, reason)`` — reported, never guessed at."""
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    """``(symbol name, what was missing)`` for a part imported without an asset.

    A part with no footprint is still worth having; a part with no footprint
    nobody was told about is how a synced catalog turns out to be unusable on
    the machine that pulled it.
    """

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
    libraries: KicadLibraries | None = None,
    model_dirs: Sequence[Path] = (),
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
            imported = import_symbol(
                conn,
                store,
                symbol,
                name,
                schema=schema,
                status=status,
                category=category,
                libraries=libraries,
                model_dirs=model_dirs,
                report=report,
            )
        except (ValueError, sqlite3.DatabaseError) as exc:
            # One unimportable symbol must not cost the other two hundred.
            report.skipped.append((name, str(exc)))
            continue
        report.imported.append(imported)

    return report


def _is_derived(symbol: SExp) -> bool:
    return symbol.find("extends", recursive=False) is not None


def import_symbol(
    conn: sqlite3.Connection,
    store: AssetStore,
    symbol: SExp,
    name: str,
    *,
    schema: FieldConfig | None = None,
    status: PartStatus = PartStatus.DRAFT,
    category: str | None = None,
    libraries: KicadLibraries | None = None,
    model_dirs: Sequence[Path] = (),
    report: ImportReport | None = None,
) -> ImportedSymbol:
    """Import one symbol node as a part.

    Public because ``klm promote`` needs exactly this and nothing else: a
    collaborator's symbol arriving from a project must land through the same
    identity rules as one arriving from a library file.
    """
    schema = schema if schema is not None else Config().fields
    raw = sym.properties(symbol)
    resolved = _resolve_fields(raw, schema)

    mpn = resolved.get("MPN", "").strip() or resolved.get("Value", "").strip() or name
    manufacturer = resolved.get("Manufacturer", "").strip() or UNKNOWN_MANUFACTURER

    klm_id = resolved.get(field_schema.KLM_ID, "").strip()
    if klm_id and not ids.is_valid(klm_id):
        raise ValueError(f"KLM_ID {klm_id!r} is not a valid identifier")

    # Identity, in descending order of certainty: the id the symbol carries,
    # then manufacturer + MPN. The second matters because the same symbol is
    # routinely copied into every project that uses it, and importing those
    # libraries must converge on one part rather than collide.
    existing = get_part(conn, klm_id) if klm_id else find_by_mpn(conn, manufacturer, mpn)
    if existing is not None:
        klm_id = existing.klm_id
    elif not klm_id:
        klm_id = ids.new_id()

    symbol_hash = store.add_bytes(dumps_canonical(symbol).encode("utf-8"), AssetKind.SYMBOL)

    lib_id = resolved.get("Footprint", "").strip()
    acquired = (
        _acquire(conn, store, lib_id, libraries, model_dirs, symbol)
        if libraries is not None and not (existing and existing.footprint_hash)
        else _Acquired()
    )
    if libraries is not None and report is not None:
        for missing in acquired.missing:
            report.unresolved.append((name, missing))

    part = Part(
        klm_id=klm_id,
        # The symbol name is the fallback MPN: for a library of hand-drawn
        # parts it is usually the closest thing to one that exists.
        mpn=mpn,
        manufacturer=manufacturer,
        description=resolved.get("Description", "").strip(),
        category=category or (existing.category if existing else None),
        package=resolved.get("Package", "").strip() or None,
        status=existing.status if existing else status,
        datasheet_url=_datasheet(resolved),
        symbol_hash=symbol_hash,
        footprint_hash=acquired.footprint or (existing.footprint_hash if existing else None),
        model3d_hash=acquired.model3d or (existing.model3d_hash if existing else None),
        parameters=_parameters(raw, schema, name),
        created_at=existing.created_at if existing else None,
    )
    save_part(conn, part)
    return ImportedSymbol(
        name=name,
        klm_id=klm_id,
        created=existing is None,
        footprint=lib_id if acquired.footprint else None,
        model3d=acquired.model_name,
    )


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


# ---------------------------------------------------------------------------
# Footprints and models the symbol points at
# ---------------------------------------------------------------------------


@dataclass
class _Acquired:
    footprint: str | None = None
    model3d: str | None = None
    model_name: str | None = None
    missing: list[str] = field(default_factory=list)


def _acquire(
    conn: sqlite3.Connection,
    store: AssetStore,
    lib_id: str,
    libraries: KicadLibraries,
    model_dirs: Sequence[Path],
    symbol: SExp,
) -> _Acquired:
    """Take the land pattern the symbol names, and the model it references.

    Nothing is invented: an unresolvable `Library:Name`, a missing `.kicad_mod`
    or a model file that is not on this machine are all recorded in ``missing``
    and the part is imported without that asset.
    """
    result = _Acquired()
    if not lib_id or ":" not in lib_id:
        result.missing.append(
            f"footprint field {lib_id!r} names no library" if lib_id else "no footprint field"
        )
        return result

    node = libraries.find_footprint(lib_id)
    if node is None:
        result.missing.append(f"footprint {lib_id} was not found in the libraries searched")
        return result

    # Read the model reference before rewriting it: the original path is the
    # only thing that says which file on disk this footprint means.
    source = _model_source(node, model_dirs)
    name = lib_id.split(":", 1)[1]

    if source is None:
        fp.remove_model_nodes(node)
        result.missing.append(f"{lib_id} references no 3D model klm could find")
    else:
        data = source.read_bytes()
        result.model3d = store.add_bytes(data, AssetKind.MODEL3D)
        result.model_name = source.stem
        fp.rewrite_model_paths(node, env_var=MODELS_VAR, filename=f"{name}.step")
        register_asset(
            conn,
            result.model3d,
            AssetKind.MODEL3D,
            filename=name,
            source=AssetOrigin.IMPORTED,
            qa=check_model3d(data),
        )

    result.footprint = store.add_bytes(
        dumps_canonical(node).encode("utf-8"), AssetKind.FOOTPRINT
    )
    register_asset(
        conn,
        result.footprint,
        AssetKind.FOOTPRINT,
        filename=name,
        source=AssetOrigin.IMPORTED,
        qa=check_footprint(node, symbol=symbol),
    )
    return result


def _model_source(node: SExp, model_dirs: Sequence[Path]) -> Path | None:
    """Resolve a footprint's `(model ...)` path against the directories given.

    The recorded path is usually absolute and usually wrong — it was written on
    whichever machine drew the footprint. The basename is the part of it that
    travels, so that is what is looked up.

    A reference to a `.wrl` is followed to the STEP beside it: KiCad ships both
    and names them identically, and a mesh is not something klm can put in a
    STEP-shaped slot.
    """
    for model in node.find_all("model"):
        if len(model) < 2 or not isinstance(model[1], Atom):
            continue
        raw = model[1].value
        direct = Path(raw)
        if not raw.startswith("${") and direct.is_file():
            return direct
        name = PurePath(raw.replace("\\", "/")).name
        stem = PurePath(name).stem
        wanted = [f"{stem}{ext}" for ext in _STEP_SUFFIXES]
        if PurePath(name).suffix.lower() in (".step", ".stp"):
            wanted.insert(0, name)
        for directory in model_dirs:
            for candidate in wanted:
                path = directory / candidate
                if path.is_file():
                    return path
    return None
