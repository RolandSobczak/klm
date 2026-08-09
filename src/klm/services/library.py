"""Building a KiCad library out of catalog parts.

There are two libraries in klm's life and they must be built the same way: the
global one that ``klm generate`` writes for KiCad's global tables, and the
vendored one that ``klm vendor`` writes inside a project. They differ only in
where the files land and what a ``(model ...)`` path and a ``Footprint`` field
point at — which is exactly what :class:`Layout` captures.

Building them from one code path is not tidiness. Sync compares the hash of a
vendored asset against the hash of the same asset built from the catalog; if the
two builders drifted by so much as a field order, every part would read as
``conflict`` forever (docs/06 §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from klm import fields
from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.sexpr import Document, SExp, dumps_canonical, loads
from klm.model import Part
from klm.store.assets import AssetError, AssetKind, AssetStore

__all__ = [
    "DEFAULT_FORMAT_VERSION",
    "GENERATOR",
    "MODEL_ENV_VAR",
    "BuiltPart",
    "Layout",
    "LibraryBuild",
    "NameMap",
    "apply_fields",
    "assign_names",
    "build_library",
    "reference_prefix",
    "write_if_changed",
    "write_library",
]

GENERATOR = "klm"
DEFAULT_FORMAT_VERSION = "20231120"
MODEL_ENV_VAR = "KLM_3DMODELS"


@dataclass(frozen=True)
class Layout:
    """Where a built library's files go, and how they refer to one another."""

    symbol_file: Path
    footprint_dir: Path
    model_dir: Path
    footprint_lib: str
    """The nickname a symbol's ``Footprint`` field is prefixed with."""
    include_models: bool = True
    """When false, models are not written *and* their references are dropped.

    The directory is still named, because a build that stops including models
    has to be able to clear away the ones a previous build left behind.
    """
    model_env_var: str = MODEL_ENV_VAR
    model_subdir: str = ""


@dataclass(frozen=True)
class NameMap:
    """The file and symbol names a build will use, keyed by part.

    Assigned once and reused, because a name that changed between the build and
    the schematic rewrite would produce a project whose symbols do not resolve.
    """

    symbols: dict[str, str] = field(default_factory=dict)
    """``klm_id`` → symbol name. A part absent here is built without a symbol."""
    footprints: dict[str, str] = field(default_factory=dict)
    """``klm_id`` → footprint file stem."""
    models: dict[str, str] = field(default_factory=dict)
    """3D model content hash → file stem. Keyed by asset because models are shared."""

    def without_symbols(self, klm_ids: set[str]) -> NameMap:
        return NameMap(
            symbols={k: v for k, v in self.symbols.items() if k not in klm_ids},
            footprints=self.footprints,
            models=self.models,
        )


def assign_names(
    parts: list[Part],
    store: AssetStore,
    *,
    keep_symbols: dict[str, str] | None = None,
    keep_footprints: dict[str, str] | None = None,
) -> NameMap:
    """Name every symbol, footprint and model deterministically.

    Names are assigned in ``klm_id`` order so a collision resolves the same way
    on every machine and every run. Footprints and models are named per *asset*,
    not per part: fifty 0402 resistors share one footprint file, and naming it
    per part would write it fifty times.

    ``keep_*`` pins names a caller has already committed to — the ones a lock
    file records. Adding a part to a vendored project must not rename the parts
    already in it, or every schematic in the repository needs re-linking.
    """
    ordered = sorted(parts, key=lambda p: p.klm_id)
    pinned_symbols = keep_symbols or {}
    pinned_footprints = keep_footprints or {}

    symbols: dict[str, str] = {}
    used_symbols = set(pinned_symbols.values())
    for part in ordered:
        pinned = pinned_symbols.get(part.klm_id)
        if pinned:
            symbols[part.klm_id] = pinned
            continue
        candidate = sym.sanitize_name(part.mpn)
        if candidate in used_symbols:
            candidate = f"{candidate}_{part.klm_id[-6:]}"
        used_symbols.add(candidate)
        symbols[part.klm_id] = candidate

    footprints: dict[str, str] = {}
    models: dict[str, str] = {}
    by_hash: dict[str, str] = {}
    used_footprints = set(pinned_footprints.values())
    for part in ordered:
        if part.footprint_hash is None:
            continue
        pinned_fp = pinned_footprints.get(part.klm_id)
        if pinned_fp:
            by_hash.setdefault(part.footprint_hash, pinned_fp)
        if part.footprint_hash not in by_hash:
            name = _footprint_name(store, part)
            if name in used_footprints:
                name = f"{name}_{part.footprint_hash[7:13]}"
            used_footprints.add(name)
            by_hash[part.footprint_hash] = name
        footprints[part.klm_id] = by_hash[part.footprint_hash]

        # A shared model is named after the first footprint that references it,
        # which keeps the filename meaningful in KiCad's 3D viewer.
        if part.model3d_hash and part.model3d_hash not in models:
            models[part.model3d_hash] = by_hash[part.footprint_hash]

    return NameMap(symbols=symbols, footprints=footprints, models=models)


def _footprint_name(store: AssetStore, part: Part) -> str:
    fallback = sym.sanitize_name(part.package or part.mpn)
    if part.footprint_hash is None:  # pragma: no cover - caller checks
        return fallback
    try:
        doc = loads(store.read_text(part.footprint_hash, AssetKind.FOOTPRINT))
    except (AssetError, ValueError):
        return fallback
    return sym.sanitize_name(fp.footprint_name(doc) or fallback)


@dataclass
class BuiltPart:
    """One part's contribution to a library, ready to be written or hashed."""

    klm_id: str
    symbol_name: str | None = None
    symbol: SExp | None = None
    footprint_name: str | None = None
    footprint: SExp | None = None
    model_name: str | None = None
    model_hash: str | None = None

    def symbol_bytes(self) -> bytes | None:
        return None if self.symbol is None else dumps_canonical(self.symbol).encode("utf-8")

    def footprint_bytes(self) -> bytes | None:
        return None if self.footprint is None else dumps_canonical(self.footprint).encode("utf-8")


@dataclass
class LibraryBuild:
    layout: Layout
    parts: list[BuiltPart] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(klm_id, reason)`` — reported, never silently dropped."""
    format_version: str = DEFAULT_FORMAT_VERSION

    @property
    def ok(self) -> bool:
        return not self.skipped

    def symbols(self) -> list[SExp]:
        """Every symbol, ordered by name so the emitted library is byte-stable."""
        named = [(p.symbol_name or "", p.symbol) for p in self.parts if p.symbol is not None]
        return [symbol for _, symbol in sorted(named, key=lambda item: item[0]) if symbol]

    def footprints(self) -> dict[str, SExp]:
        """Footprint file stem → node, deduplicated across the parts that share it."""
        out: dict[str, SExp] = {}
        for part in self.parts:
            if part.footprint_name and part.footprint is not None:
                out.setdefault(part.footprint_name, part.footprint)
        return out

    def models(self) -> dict[str, str]:
        """Model file stem → content hash."""
        return {
            part.model_name: part.model_hash
            for part in self.parts
            if part.model_name and part.model_hash
        }

    def by_id(self, klm_id: str) -> BuiltPart | None:
        return next((p for p in self.parts if p.klm_id == klm_id), None)


def build_library(
    parts: list[Part], store: AssetStore, layout: Layout, names: NameMap
) -> LibraryBuild:
    """Turn catalog parts into library artifacts, without touching the disk.

    Separating build from write is what lets ``--dry-run`` and ``sync status``
    ask "what would this file contain?" through exactly the code that would
    write it, rather than through a second implementation that approximates it.
    """
    build = LibraryBuild(layout=layout)

    for part in sorted(parts, key=lambda p: p.klm_id):
        built = BuiltPart(klm_id=part.klm_id)
        footprint_name = names.footprints.get(part.klm_id)

        symbol_name = names.symbols.get(part.klm_id)
        if symbol_name is not None:
            symbol = _build_symbol(store, part, symbol_name, footprint_name, layout, build)
            if symbol is not None:
                built.symbol_name = symbol_name
                built.symbol = symbol

        if footprint_name is not None:
            footprint = _build_footprint(store, part, names, layout, build)
            if footprint is not None:
                built.footprint_name = footprint_name
                built.footprint = footprint

        if layout.include_models and part.model3d_hash:
            built.model_hash = part.model3d_hash
            built.model_name = names.models.get(part.model3d_hash, footprint_name)

        if built.symbol is not None or built.footprint is not None:
            build.parts.append(built)

    return build


def _build_symbol(
    store: AssetStore,
    part: Part,
    symbol_name: str,
    footprint_name: str | None,
    layout: Layout,
    build: LibraryBuild,
) -> SExp | None:
    if part.symbol_hash is None:
        build.skipped.append((part.klm_id, "no symbol asset"))
        return None
    try:
        source = loads(store.read_text(part.symbol_hash, AssetKind.SYMBOL))
    except (AssetError, ValueError) as exc:
        build.skipped.append((part.klm_id, f"symbol unreadable: {exc}"))
        return None

    extracted = sym.extract_symbols(source)
    if not extracted:
        build.skipped.append((part.klm_id, "asset contains no symbol"))
        return None

    version = _library_version(source)
    if version is not None:
        build.format_version = version

    symbol = extracted[0]
    sym.rename_symbol(symbol, symbol_name)
    apply_fields(symbol, part, footprint_name, library=layout.footprint_lib)
    return symbol


def _build_footprint(
    store: AssetStore, part: Part, names: NameMap, layout: Layout, build: LibraryBuild
) -> SExp | None:
    if part.footprint_hash is None:  # pragma: no cover - caller checks
        return None
    try:
        doc = loads(store.read_text(part.footprint_hash, AssetKind.FOOTPRINT))
    except (AssetError, ValueError) as exc:
        build.skipped.append((part.klm_id, f"footprint unreadable: {exc}"))
        return None

    root = doc.root
    if not layout.include_models:
        fp.remove_model_nodes(root)
    else:
        model_name = names.models.get(part.model3d_hash or "", names.footprints[part.klm_id])
        fp.rewrite_model_paths(
            root,
            env_var=layout.model_env_var,
            subdir=layout.model_subdir,
            filename=f"{model_name}.step",
        )
    return root


def _library_version(doc: Document) -> str | None:
    node = doc.root.find("version", recursive=False)
    if node is not None and len(node) >= 2:
        return getattr(node[1], "value", None)
    return None


def apply_fields(
    symbol: SExp, part: Part, footprint_name: str | None, *, library: str
) -> None:
    """Impose the canonical field schema on a symbol.

    The source symbol's own field spelling is discarded — that is the entire
    point of the exercise (docs/05).
    """
    sym.set_property(symbol, "Reference", reference_prefix(part), hidden=False)
    sym.set_property(symbol, "Value", part.mpn, hidden=False)
    if footprint_name:
        sym.set_property(symbol, "Footprint", f"{library}:{footprint_name}")
    if part.datasheet_url:
        sym.set_property(symbol, "Datasheet", part.datasheet_url)
    sym.set_property(symbol, "MPN", part.mpn)
    sym.set_property(symbol, "Manufacturer", part.manufacturer)
    if part.description:
        sym.set_property(symbol, "Description", part.description)
    if part.package:
        sym.set_property(symbol, "Package", part.package)
    sym.set_property(symbol, fields.KLM_ID, part.klm_id)
    _sort_properties(symbol)


def _sort_properties(symbol: SExp) -> None:
    """Emit fields in canonical schema order rather than insertion order.

    Without this, two parts carrying the same fields produce different byte
    orders depending on what order the source symbol happened to list them in,
    and a re-import shuffles a library for no reason (docs/06 §5). Names klm
    does not model sort after the canonical ones, by name, so they are stable
    too rather than merely last.

    Positions are reused rather than the list rebuilt, which keeps the fields
    wherever they were relative to the graphical units — KiCad expects those
    last.
    """
    slots = [
        index
        for index, child in enumerate(symbol.children)
        if isinstance(child, SExp) and child.name == "property"
    ]
    nodes = [symbol.children[index] for index in slots]
    ordered = sorted(nodes, key=lambda n: fields.sort_key(sym.property_name(cast(SExp, n))))
    for index, node in zip(slots, ordered, strict=True):
        symbol.children[index] = node


def reference_prefix(part: Part) -> str:
    """Best-effort designator prefix from the category.

    Deliberately crude: the field schema owns this properly. Getting it wrong
    here costs a rename in the schematic, not a broken library.
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


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_library(build: LibraryBuild, store: AssetStore, *, prune: bool = True) -> bool:
    """Write a build to its layout. Returns True if any file changed.

    ``prune`` removes footprint and model files the build no longer produces,
    which is what keeps ``generated/`` a faithful mirror of the catalog rather
    than an accumulation of everything ever approved.
    """
    layout = build.layout
    changed = write_if_changed(
        layout.symbol_file,
        dumps_canonical(
            sym.make_library(build.symbols(), generator=GENERATOR, version=build.format_version)
        ),
    )

    layout.footprint_dir.mkdir(parents=True, exist_ok=True)
    footprints = build.footprints()
    for name, node in sorted(footprints.items()):
        changed |= write_if_changed(
            layout.footprint_dir / f"{name}.kicad_mod", dumps_canonical(node)
        )
    if prune:
        expected = {f"{name}.kicad_mod" for name in footprints}
        for stale in sorted(layout.footprint_dir.glob("*.kicad_mod")):
            if stale.name not in expected:
                stale.unlink()
                changed = True

    if layout.include_models:
        changed |= _write_models(build, store, layout.model_dir, prune=prune)
    elif prune:
        # A project re-vendored without --with-3d must lose its models, or the
        # repository keeps shipping files nothing references (docs/06 §7).
        changed |= _clear_models(layout.model_dir)

    return changed


def _write_models(build: LibraryBuild, store: AssetStore, target: Path, *, prune: bool) -> bool:
    target.mkdir(parents=True, exist_ok=True)
    changed = False
    written: set[str] = set()
    for name, content_hash in sorted(build.models().items()):
        try:
            data = store.read(content_hash, AssetKind.MODEL3D)
        except AssetError:
            continue
        path = target / f"{name}.step"
        written.add(path.name)
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
            changed = True

    if prune:
        for stale in sorted(target.glob("*.step")):
            if stale.name not in written:
                stale.unlink()
                changed = True
    return changed


def _clear_models(directory: Path) -> bool:
    """Delete the models a previous build with 3D left behind."""
    if not directory.is_dir():
        return False
    changed = False
    for stale in sorted(directory.glob("*.step")):
        stale.unlink()
        changed = True
    if not any(directory.iterdir()):
        directory.rmdir()
    return changed


def write_if_changed(target: Path, content: str) -> bool:
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
