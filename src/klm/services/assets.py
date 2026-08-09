"""Acquiring a part's assets, in the order that produces the best result.

The priority order (docs/08 §1) is the whole design:

1. **Already in the catalog.** A new 0402 resistor must not create a new
   footprint asset. Fifty of them share one footprint and one 3D model, and
   that is what keeps the repository small and makes a footprint fix propagate.
2. **KiCad's own libraries.** IPC-compliant, widely reviewed, and permissively
   licensed with an explicit exception for use in designs — the safest origin
   for anything headed to a public repository.
3. **Generated from a template.** Deterministic and exact for the cases it
   covers: two-terminal passives and chip land patterns.
4. ~~EasyEDA / LCSC import~~ — **not implemented**, see the note below.
5. **Hand-drawn.** klm imports the result and hashes it; that is `klm import`.

**On source 4.** [ADR-0009](../../docs/adr/0009-lcsc-manual-first.md) established
that klm ships no client against LCSC's unofficial endpoints, and the EasyEDA
component API is exactly such an endpoint. Separately,
[Q4](../../docs/14-open-questions.md#q4) — whether EasyEDA-derived symbols,
footprints and models may be redistributed in a public repository — is still
open, and vendoring assets into a GitHub project is precisely the case it
covers. Both would have to be answered before the importer is worth writing, so
`klm assets acquire` reports EasyEDA as unavailable rather than pretending.

Every acquisition ends in the QA gate, and the report is stored on the asset so
that "was this checked?" outlives the session that checked it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from klm.assets import landpattern, templates
from klm.assets.kicad_libs import KicadLibraries, default_libraries
from klm.assets.packages import Package, find_package
from klm.assets.qa import QaReport, QaStatus, check_footprint, check_model3d, check_symbol
from klm.categories import find_category
from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.sexpr import SExp, dumps_canonical, loads
from klm.model import Part
from klm.services.catalog import now, save_part
from klm.services.register import MODELS_VAR
from klm.store.assets import AssetError, AssetKind, AssetStore
from klm.store.db import transaction

__all__ = [
    "Acquired",
    "AcquisitionReport",
    "AssetOrigin",
    "acquire_assets",
    "find_asset_by_filename",
    "register_asset",
    "reuse_candidates",
    "run_qa",
]


class AssetOrigin:
    """Where an asset came from. Stored verbatim in `asset.source`."""

    CATALOG = "catalog"
    """Reused from a part klm already had — the best outcome."""
    KICAD = "imported:kicad"
    GENERATED = "generated"
    IMPORTED = "imported:kicad-file"
    HAND = "hand-drawn"
    PROJECT = "project"
    """Taken back out of a project by `klm sync push` or `klm promote`."""


#: What KiCad's own library exception amounts to, recorded on every asset taken
#: from it so the licence question is answerable later without guessing.
KICAD_LICENSE = "KiCad Library License (CC-BY-SA 4.0 with the design-use exception)"

#: Category path → the KiCad symbol everyone already recognises. Reaching for
#: `Device:R` rather than a generated rectangle is not about effort; it is that
#: a library whose resistors look unfamiliar is a library people misread.
CATEGORY_SYMBOLS = {
    "Passive/Resistor": "Device:R",
    "Passive/Resistor/Shunt": "Device:R_Shunt",
    "Passive/Resistor/Potentiometer": "Device:R_Potentiometer",
    "Passive/Resistor/NTC": "Device:Thermistor_NTC",
    "Passive/Capacitor": "Device:C",
    "Passive/Capacitor/Ceramic": "Device:C",
    "Passive/Capacitor/Electrolytic": "Device:C_Polarized",
    "Passive/Capacitor/Tantalum": "Device:C_Polarized",
    "Passive/Inductor": "Device:L",
    "Passive/Ferrite": "Device:FerriteBead",
    "Diode": "Device:D",
    "Diode/Schottky": "Device:D_Schottky",
    "Diode/Zener": "Device:D_Zener",
    "Diode/LED": "Device:LED",
    "Diode/TVS": "Device:D_TVS",
}

#: Designators klm has a two-terminal template for.
_TEMPLATE_DESIGNATORS = {"R", "C", "L", "FB"}


@dataclass(frozen=True, slots=True)
class Acquired:
    """One asset klm produced or found, and what it thinks of it."""

    kind: AssetKind
    content_hash: str
    origin: str
    detail: str
    qa: QaReport | None = None

    @property
    def reused(self) -> bool:
        return self.origin == AssetOrigin.CATALOG


@dataclass
class AcquisitionReport:
    klm_id: str
    acquired: list[Acquired] = field(default_factory=list)
    unavailable: list[tuple[str, str]] = field(default_factory=list)
    """``(kind, reason)`` — what klm could not get, and why. Never silent."""

    def of(self, kind: AssetKind) -> Acquired | None:
        return next((a for a in self.acquired if a.kind is kind), None)

    @property
    def reused(self) -> int:
        return sum(1 for a in self.acquired if a.reused)

    @property
    def blocked(self) -> list[Acquired]:
        """Assets whose QA gate failed. These stop a part being approved."""
        return [a for a in self.acquired if a.qa is not None and not a.qa.passed]

    @property
    def ok(self) -> bool:
        return not self.unavailable and not self.blocked


# ---------------------------------------------------------------------------
# The asset metadata table
# ---------------------------------------------------------------------------


def register_asset(
    conn: sqlite3.Connection,
    content_hash: str,
    kind: AssetKind,
    *,
    filename: str,
    source: str,
    license_note: str | None = None,
    qa: QaReport | None = None,
) -> None:
    """Record what an asset is and where it came from.

    Assets are immutable, so this is an upsert on everything *except* the hash:
    re-acquiring the same footprint from the same place must not create a second
    row, and re-running QA on it must update the verdict rather than append one.
    """
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO asset (content_hash, kind, filename, source, license_note,
                               qa_status, qa_report, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                filename = excluded.filename,
                source = excluded.source,
                license_note = COALESCE(excluded.license_note, asset.license_note),
                qa_status = excluded.qa_status,
                qa_report = excluded.qa_report
            """,
            (
                content_hash,
                str(kind),
                filename,
                source,
                license_note,
                str(qa.status) if qa is not None else str(QaStatus.UNCHECKED),
                qa.to_json() if qa is not None else None,
                now(),
            ),
        )


def find_asset_by_filename(
    conn: sqlite3.Connection, kind: AssetKind, filename: str
) -> str | None:
    """The hash of an asset klm already holds under this name, if any.

    Name rather than geometry, deliberately: two footprints called
    `R_0402_1005Metric` are the same land pattern by convention, and comparing
    geometry to establish that would be slower and no more certain. Genuine
    near-duplicate detection is :func:`reuse_candidates`, which is a separate,
    explicit operation.
    """
    row = conn.execute(
        "SELECT content_hash FROM asset WHERE kind = ? AND filename = ? "
        "ORDER BY created_at LIMIT 1",
        (str(kind), filename),
    ).fetchone()
    return str(row["content_hash"]) if row else None


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


def acquire_assets(
    conn: sqlite3.Connection,
    store: AssetStore,
    part: Part,
    *,
    libraries: KicadLibraries | None = None,
    overwrite: bool = False,
    save: bool = True,
) -> AcquisitionReport:
    """Find or build this part's symbol, footprint and 3D model.

    Existing assets are left alone unless ``overwrite`` is set: re-running
    acquisition on a part someone has hand-corrected must not throw the
    correction away.
    """
    libs = libraries if libraries is not None else default_libraries()
    package = find_package(part.package)
    category = find_category(part.category)
    designator = category.designator if category else "U"
    report = AcquisitionReport(klm_id=part.klm_id)

    symbol_node: SExp | None = None
    if part.symbol_hash and not overwrite:
        symbol_node = _read_symbol(store, part.symbol_hash)
    else:
        acquired = _acquire_symbol(conn, store, part, package, designator, libs)
        if acquired is None:
            report.unavailable.append(
                (
                    "symbol",
                    f"no template for designator {designator!r} and KiCad's libraries have "
                    "nothing for this category — draw one and `klm import --from-kicad` it",
                )
            )
        else:
            part.symbol_hash = acquired.content_hash
            symbol_node = _read_symbol(store, acquired.content_hash)
            report.acquired.append(acquired)

    if part.footprint_hash and not overwrite:
        pass
    else:
        acquired = _acquire_footprint(conn, store, package, designator, libs, symbol_node)
        if acquired is None:
            report.unavailable.append(
                (
                    "footprint",
                    f"package {part.package or 'unset'!r} is not in KiCad's libraries and is "
                    "not a chip klm generates",
                )
            )
        else:
            part.footprint_hash = acquired.content_hash
            report.acquired.append(acquired)

    if part.model3d_hash and not overwrite:
        pass
    else:
        acquired = _acquire_model(conn, store, package, designator, libs)
        if acquired is None:
            report.unavailable.append(
                ("model3d", "no 3D model found; convert a mesh with `klm assets convert-3d`")
            )
        else:
            part.model3d_hash = acquired.content_hash
            report.acquired.append(acquired)

    if save:
        save_part(conn, part)
    return report


def _acquire_symbol(
    conn: sqlite3.Connection,
    store: AssetStore,
    part: Part,
    package: Package | None,
    designator: str,
    libs: KicadLibraries,
) -> Acquired | None:
    name = sym.sanitize_name(part.mpn or part.klm_id)
    lib_id = CATEGORY_SYMBOLS.get(part.category or "")

    # There is no catalog-reuse step for symbols, and that is not an omission.
    # A symbol carries the part's own name and `Value`, so two parts can never
    # share one: `RC0402FR-074K7L` and `RC0402FR-0710KL` differ in the symbol
    # itself. Footprints and 3D models are where sharing pays, and they have it.

    # 2 — KiCad's libraries.
    if lib_id:
        node = libs.find_symbol(lib_id)
        if node is not None:
            sym.rename_symbol(node, name)
            content_hash = _store(store, AssetKind.SYMBOL, node)
            qa = check_symbol(node, package=package, category=part.category)
            register_asset(
                conn,
                content_hash,
                AssetKind.SYMBOL,
                filename=lib_id,
                source=AssetOrigin.KICAD,
                license_note=KICAD_LICENSE,
                qa=qa,
            )
            return Acquired(AssetKind.SYMBOL, content_hash, AssetOrigin.KICAD, lib_id, qa)

    # 3 — a template, for the two-terminal parts whose symbol is fully implied.
    if designator.upper() in _TEMPLATE_DESIGNATORS:
        node = templates.passive_symbol(name, designator.upper())
        content_hash = _store(store, AssetKind.SYMBOL, node)
        qa = check_symbol(node, package=package, category=part.category)
        register_asset(
            conn,
            content_hash,
            AssetKind.SYMBOL,
            filename=f"template:{designator.upper()}",
            source=AssetOrigin.GENERATED,
            qa=qa,
        )
        return Acquired(
            AssetKind.SYMBOL, content_hash, AssetOrigin.GENERATED, f"{designator} template", qa
        )

    return None


def _acquire_footprint(
    conn: sqlite3.Connection,
    store: AssetStore,
    package: Package | None,
    designator: str,
    libs: KicadLibraries,
    symbol: SExp | None,
) -> Acquired | None:
    if package is None:
        return None
    lib_id = package.kicad_id(designator)
    name = lib_id.split(":", 1)[1] if lib_id else None

    # 1 — the catalog. This is the branch that matters most for hygiene.
    if name:
        existing = find_asset_by_filename(conn, AssetKind.FOOTPRINT, name)
        if existing and store.exists(existing, AssetKind.FOOTPRINT):
            return Acquired(
                AssetKind.FOOTPRINT, existing, AssetOrigin.CATALOG, f"reused {name}"
            )

    # 2 — KiCad's libraries.
    if lib_id:
        node = libs.find_footprint(lib_id)
        if node is not None:
            fp.rewrite_model_paths(node, env_var=MODELS_VAR)
            content_hash = _store(store, AssetKind.FOOTPRINT, node)
            qa = check_footprint(node, symbol=symbol, package=package)
            register_asset(
                conn,
                content_hash,
                AssetKind.FOOTPRINT,
                filename=name or lib_id,
                source=AssetOrigin.KICAD,
                license_note=KICAD_LICENSE,
                qa=qa,
            )
            return Acquired(AssetKind.FOOTPRINT, content_hash, AssetOrigin.KICAD, lib_id, qa)

    # 3 — generate, for chips only.
    if package.generatable:
        node = landpattern.chip_footprint(package, designator)
        generated_name = landpattern.chip_footprint_name(package, designator)
        content_hash = _store(store, AssetKind.FOOTPRINT, node)
        qa = check_footprint(node, symbol=symbol, package=package)
        register_asset(
            conn,
            content_hash,
            AssetKind.FOOTPRINT,
            filename=generated_name,
            source=AssetOrigin.GENERATED,
            qa=qa,
        )
        return Acquired(
            AssetKind.FOOTPRINT,
            content_hash,
            AssetOrigin.GENERATED,
            f"{generated_name} (IPC-7351B nominal)",
            qa,
        )

    return None


def _acquire_model(
    conn: sqlite3.Connection,
    store: AssetStore,
    package: Package | None,
    designator: str,
    libs: KicadLibraries,
) -> Acquired | None:
    """KiCad ships STEP models beside its footprints; take those where they exist."""
    if package is None:
        return None
    lib_id = package.kicad_id(designator)
    if lib_id is None:
        return None
    library, _, name = lib_id.partition(":")

    existing = find_asset_by_filename(conn, AssetKind.MODEL3D, name)
    if existing and store.exists(existing, AssetKind.MODEL3D):
        return Acquired(AssetKind.MODEL3D, existing, AssetOrigin.CATALOG, f"reused {name}")

    path = _find_kicad_model(libs, library, name)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None

    content_hash = store.add_bytes(data, AssetKind.MODEL3D)
    qa = check_model3d(data)
    register_asset(
        conn,
        content_hash,
        AssetKind.MODEL3D,
        filename=name,
        source=AssetOrigin.KICAD,
        license_note=KICAD_LICENSE,
        qa=qa,
    )
    return Acquired(AssetKind.MODEL3D, content_hash, AssetOrigin.KICAD, str(path), qa)


def _find_kicad_model(libs: KicadLibraries, library: str, name: str) -> Path | None:
    """`Package_SO.3dshapes/SOIC-8_….step`, beside the footprint directories."""
    for directory in libs.footprint_dirs:
        for root in (directory.parent / "3dmodels", directory.parent):
            candidate = root / f"{library}.3dshapes" / f"{name}.step"
            if candidate.is_file():
                return candidate
    return None


def _store(store: AssetStore, kind: AssetKind, node: SExp) -> str:
    """Canonical bytes in, content hash out.

    Canonical rather than lossless because klm authored these: a generated
    asset has no user formatting to preserve, and a canonical writer is what
    makes the output byte-stable across rebuilds.
    """
    return store.add_bytes(dumps_canonical(node).encode("utf-8"), kind)


def _read_symbol(store: AssetStore, content_hash: str) -> SExp | None:
    try:
        symbols = sym.extract_symbols(loads(store.read_text(content_hash, AssetKind.SYMBOL)))
    except (AssetError, ValueError):
        return None
    return symbols[0] if symbols else None


# ---------------------------------------------------------------------------
# Re-running QA on what is already stored
# ---------------------------------------------------------------------------


def run_qa(
    conn: sqlite3.Connection, store: AssetStore, part: Part
) -> dict[AssetKind, QaReport]:
    """Re-check a part's stored assets and update their recorded verdicts."""
    package = find_package(part.package)
    reports: dict[AssetKind, QaReport] = {}

    symbol = _read_symbol(store, part.symbol_hash) if part.symbol_hash else None
    if symbol is not None and part.symbol_hash:
        report = check_symbol(symbol, package=package, category=part.category)
        _update_qa(conn, part.symbol_hash, report)
        reports[AssetKind.SYMBOL] = report

    footprint = _read_footprint(store, part.footprint_hash) if part.footprint_hash else None
    if footprint is not None and part.footprint_hash:
        report = check_footprint(footprint, symbol=symbol, package=package)
        _update_qa(conn, part.footprint_hash, report)
        reports[AssetKind.FOOTPRINT] = report

    if part.model3d_hash:
        try:
            data = store.read(part.model3d_hash, AssetKind.MODEL3D)
        except AssetError:
            data = None
        if data is not None:
            report = check_model3d(data, footprint=footprint)
            _update_qa(conn, part.model3d_hash, report)
            reports[AssetKind.MODEL3D] = report

    return reports


def _read_footprint(store: AssetStore, content_hash: str) -> SExp | None:
    try:
        return loads(store.read_text(content_hash, AssetKind.FOOTPRINT)).root
    except (AssetError, ValueError):
        return None


def _update_qa(conn: sqlite3.Connection, content_hash: str, report: QaReport) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE asset SET qa_status = ?, qa_report = ? WHERE content_hash = ?",
            (str(report.status), report.to_json(), content_hash),
        )


# ---------------------------------------------------------------------------
# Reuse hygiene
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReuseCandidate:
    left: str
    right: str
    detail: str


def reuse_candidates(
    conn: sqlite3.Connection, store: AssetStore, *, tolerance: float = 0.05
) -> list[ReuseCandidate]:
    """Footprints that are the same land pattern under two hashes.

    Compared by **pad geometry, not name** — that is the whole point. Catalog
    hygiene decays silently otherwise, and a duplicate footprint is the kind of
    thing you only notice when two identical parts order differently.
    """
    signatures: dict[tuple[tuple[int, ...], ...], list[str]] = {}
    for row in conn.execute(
        "SELECT content_hash, filename FROM asset WHERE kind = 'footprint' ORDER BY content_hash"
    ):
        content_hash = str(row["content_hash"])
        document = _read_footprint(store, content_hash)
        if document is None:
            continue
        signature = _pad_signature(document, tolerance)
        if signature:
            signatures.setdefault(signature, []).append(content_hash)

    candidates: list[ReuseCandidate] = []
    for group in signatures.values():
        for other in group[1:]:
            candidates.append(
                ReuseCandidate(group[0], other, "identical pad geometry under two hashes")
            )
    return candidates


def _pad_signature(document: SExp, tolerance: float) -> tuple[tuple[int, ...], ...]:
    """Quantised pad geometry, order-independent.

    Quantised because two footprints drawn to the same land pattern differ in
    the last decimal often enough that exact comparison finds nothing.
    """
    steps = max(tolerance, 1e-6)
    pads = [
        (
            round(pad.x / steps),
            round(pad.y / steps),
            round(pad.width / steps),
            round(pad.height / steps),
        )
        for pad in fp.iter_pads(document)
        if pad.plated
    ]
    return tuple(sorted(pads))
