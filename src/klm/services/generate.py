"""Build the KiCad libraries KiCad actually reads, from the catalog.

``generated/`` is a build artifact: it can be deleted and rebuilt at any time,
and it is what the global library tables point at. Generation is idempotent and
byte-stable, so the desktop app can run it after every edit without churn
(docs/04 §5).

Only ``approved`` parts are generated. A draft is not usable in a design, and
putting it in the library would make it usable by accident.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from klm import __version__, fields
from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.sexpr import Document, dumps_canonical, loads
from klm.model import Part, PartStatus
from klm.services.catalog import list_parts
from klm.store.assets import AssetError, AssetKind, AssetStore
from klm.store.paths import Paths

__all__ = ["GenerateResult", "generate"]

GENERATOR = "klm"
DEFAULT_FORMAT_VERSION = "20231120"
MODEL_ENV_VAR = "KLM_3DMODELS"


@dataclass
class GenerateResult:
    symbols: int = 0
    footprints: int = 0
    models: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(klm_id, reason)`` for parts that could not be generated."""
    changed: bool = False
    """False when every output file already held the bytes we would write."""

    @property
    def ok(self) -> bool:
        return not self.skipped


def generate(conn: sqlite3.Connection, paths: Paths) -> GenerateResult:
    """Rebuild ``generated/`` from every approved part."""
    store = AssetStore(paths.assets)
    parts = list_parts(conn, status=PartStatus.APPROVED)
    result = GenerateResult()

    # Names are assigned in klm_id order so that a collision resolves the same
    # way on every machine and every run.
    symbol_names = _assign_names(parts, lambda p: sym.sanitize_name(p.mpn))
    footprint_names, model_names = _assign_asset_names(parts, store, result)

    symbols = []
    format_version = DEFAULT_FORMAT_VERSION
    for part in parts:
        if part.symbol_hash is None:
            result.skipped.append((part.klm_id, "no symbol asset"))
            continue
        try:
            source = loads(store.read_text(part.symbol_hash, AssetKind.SYMBOL))
        except (AssetError, ValueError) as exc:
            result.skipped.append((part.klm_id, f"symbol unreadable: {exc}"))
            continue

        extracted = sym.extract_symbols(source)
        if not extracted:
            result.skipped.append((part.klm_id, "asset contains no symbol"))
            continue

        version_node = _library_version(source)
        if version_node is not None:
            format_version = version_node

        symbol = extracted[0]
        sym.rename_symbol(symbol, symbol_names[part.klm_id])
        _apply_fields(symbol, part, footprint_names.get(part.klm_id))
        symbols.append(symbol)

    library = sym.make_library(symbols, generator=GENERATOR, version=format_version)
    result.symbols = len(symbols)
    result.changed |= _write_if_changed(
        paths.generated_symbols, dumps_canonical(library)
    )

    result.footprints, footprint_changed = _write_footprints(
        parts, store, paths, footprint_names, model_names, result
    )
    result.models, models_changed = _write_models(parts, store, paths, model_names)
    result.changed = result.changed or footprint_changed or models_changed

    result.changed |= _write_if_changed(
        paths.generated / "manifest.json",
        _manifest(parts, symbol_names, footprint_names, model_names),
    )
    return result


def _library_version(doc: Document) -> str | None:
    node = doc.root.find("version", recursive=False)
    if node is not None and len(node) >= 2:
        child = node[1]
        return getattr(child, "value", None)
    return None


def _assign_names(parts: list[Part], base: object) -> dict[str, str]:
    """Give each part a unique name, resolving collisions deterministically."""
    assert callable(base)
    used: set[str] = set()
    out: dict[str, str] = {}
    for part in sorted(parts, key=lambda p: p.klm_id):
        candidate = base(part)
        if candidate in used:
            candidate = f"{candidate}_{part.klm_id[-6:]}"
        used.add(candidate)
        out[part.klm_id] = candidate
    return out


def _assign_asset_names(
    parts: list[Part], store: AssetStore, result: GenerateResult
) -> tuple[dict[str, str], dict[str, str]]:
    """Name footprint and model files by asset, so sharing is preserved.

    Many parts reference one footprint; it must be written once under one name,
    or fifty 0402 resistors produce fifty identical files.
    """
    footprint_by_hash: dict[str, str] = {}
    model_by_hash: dict[str, str] = {}
    used_footprints: set[str] = set()
    per_part_footprint: dict[str, str] = {}

    for part in sorted(parts, key=lambda p: p.klm_id):
        if part.footprint_hash is None:
            continue
        if part.footprint_hash not in footprint_by_hash:
            try:
                doc = loads(store.read_text(part.footprint_hash, AssetKind.FOOTPRINT))
                name = fp.footprint_name(doc) or sym.sanitize_name(part.package or part.mpn)
            except (AssetError, ValueError):
                name = sym.sanitize_name(part.package or part.mpn)
            name = sym.sanitize_name(name)
            if name in used_footprints:
                name = f"{name}_{part.footprint_hash[7:13]}"
            used_footprints.add(name)
            footprint_by_hash[part.footprint_hash] = name
        per_part_footprint[part.klm_id] = footprint_by_hash[part.footprint_hash]

        # A shared model is named after the first footprint that references it,
        # which keeps the filename meaningful in KiCad's 3D viewer.
        if part.model3d_hash and part.model3d_hash not in model_by_hash:
            model_by_hash[part.model3d_hash] = footprint_by_hash[part.footprint_hash]

    return per_part_footprint, model_by_hash


def _apply_fields(symbol: object, part: Part, footprint_name: str | None) -> None:
    """Impose the canonical field schema on a symbol.

    The imported symbol's own field spelling is discarded — that is the entire
    point of the exercise (docs/05).
    """
    from klm.kicad.sexpr import SExp

    assert isinstance(symbol, SExp)
    sym.set_property(symbol, "Reference", _reference_prefix(part), hidden=False)
    sym.set_property(symbol, "Value", part.mpn, hidden=False)
    if footprint_name:
        sym.set_property(symbol, "Footprint", f"KLM:{footprint_name}")
    if part.datasheet_url:
        sym.set_property(symbol, "Datasheet", part.datasheet_url)
    sym.set_property(symbol, "MPN", part.mpn)
    sym.set_property(symbol, "Manufacturer", part.manufacturer)
    if part.description:
        sym.set_property(symbol, "Description", part.description)
    if part.package:
        sym.set_property(symbol, "Package", part.package)
    sym.set_property(symbol, fields.KLM_ID, part.klm_id)


def _reference_prefix(part: Part) -> str:
    """Best-effort designator prefix from the category.

    Deliberately crude: Phase 1's field schema owns this properly. Getting it
    wrong here costs a rename in the schematic, not a broken library.
    """
    category = (part.category or "").lower()
    for needle, prefix in (
        ("resistor", "R"),
        ("capacitor", "C"),
        ("inductor", "L"),
        ("diode", "D"),
        ("led", "D"),
        ("transistor", "Q"),
        ("connector", "J"),
        ("crystal", "Y"),
        ("switch", "SW"),
        ("fuse", "F"),
    ):
        if needle in category:
            return f"{prefix}**"
    return "U**"


def _write_footprints(
    parts: list[Part],
    store: AssetStore,
    paths: Paths,
    footprint_names: dict[str, str],
    model_names: dict[str, str],
    result: GenerateResult,
) -> tuple[int, bool]:
    target_dir = paths.generated_footprints
    target_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}
    changed = False
    for part in sorted(parts, key=lambda p: p.klm_id):
        if part.footprint_hash is None or part.klm_id not in footprint_names:
            continue
        name = footprint_names[part.klm_id]
        if name in written:
            continue
        try:
            doc = loads(store.read_text(part.footprint_hash, AssetKind.FOOTPRINT))
        except (AssetError, ValueError) as exc:
            result.skipped.append((part.klm_id, f"footprint unreadable: {exc}"))
            continue

        model_name = model_names.get(part.model3d_hash or "", name)
        fp.rewrite_model_paths(doc, env_var=MODEL_ENV_VAR, filename=f"{model_name}.step")
        written[name] = part.footprint_hash
        changed |= _write_if_changed(target_dir / f"{name}.kicad_mod", dumps_canonical(doc))

    # Remove footprints for parts that are no longer approved.
    expected = {f"{name}.kicad_mod" for name in written}
    for stale in sorted(target_dir.glob("*.kicad_mod")):
        if stale.name not in expected:
            stale.unlink()
            changed = True
    return len(written), changed


def _write_models(
    parts: list[Part], store: AssetStore, paths: Paths, model_names: dict[str, str]
) -> tuple[int, bool]:
    target_dir = paths.generated_models3d
    target_dir.mkdir(parents=True, exist_ok=True)

    changed = False
    written: set[str] = set()
    for content_hash, name in sorted(model_names.items()):
        try:
            data = store.read(content_hash, AssetKind.MODEL3D)
        except AssetError:
            continue
        target = target_dir / f"{name}.step"
        written.add(target.name)
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)
            changed = True

    for stale in sorted(target_dir.glob("*.step")):
        if stale.name not in written:
            stale.unlink()
            changed = True
    return len(written), changed


def _manifest(
    parts: list[Part],
    symbol_names: dict[str, str],
    footprint_names: dict[str, str],
    model_names: dict[str, str],
) -> str:
    """Record what produced this build, so a library can be traced back."""
    payload = {
        "klm_version": __version__,
        "generator": GENERATOR,
        "parts": [
            {
                "klm_id": part.klm_id,
                "symbol": symbol_names.get(part.klm_id),
                "footprint": footprint_names.get(part.klm_id),
                "symbol_hash": part.symbol_hash,
                "footprint_hash": part.footprint_hash,
                "model3d_hash": part.model3d_hash,
            }
            for part in sorted(parts, key=lambda p: p.klm_id)
        ],
        "models": dict(sorted(model_names.items())),
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_if_changed(target: Path, content: str) -> bool:
    """Write only when the bytes differ, keeping mtimes and diffs stable."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return False
    tmp = target.with_name(target.name + ".klm-tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return True
