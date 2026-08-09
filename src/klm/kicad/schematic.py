"""Reading and rewriting the symbol references in a ``.kicad_sch``.

Vendoring changes exactly three things in a schematic: the ``lib_id`` of each
placed symbol, the name of each cached definition in ``lib_symbols``, and the
value of the ``Footprint`` field. Everything else — sheet geometry, wires,
UUIDs, text, whatever KiCad 10 adds next — passes through the lossless writer
byte-identically (docs/adr/0002).

The cached ``lib_symbols`` block is easy to forget and expensive to get wrong.
KiCad keeps a copy of every library symbol inside the schematic so the file
opens without its libraries; rewriting the placed symbols and not the cache
leaves a schematic whose symbols resolve to nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from klm.kicad.sexpr import Atom, Document, SExp

__all__ = [
    "SymbolInstance",
    "cached_symbols",
    "iter_symbol_instances",
    "join_lib_id",
    "rewrite_footprint_fields",
    "rewrite_lib_ids",
    "split_lib_id",
]


def split_lib_id(lib_id: str) -> tuple[str, str]:
    """``"KLM:R_0402"`` into its library nickname and item name.

    An identifier with no colon has no library, which is legal in older files;
    the nickname comes back empty rather than being invented.
    """
    nickname, separator, name = lib_id.partition(":")
    return (nickname, name) if separator else ("", lib_id)


def join_lib_id(nickname: str, name: str) -> str:
    return f"{nickname}:{name}" if nickname else name


@dataclass(frozen=True)
class SymbolInstance:
    """One symbol placed on a sheet, with the facts vendoring needs."""

    lib_id: str
    reference: str
    klm_id: str | None
    footprint: str | None
    sheet: str = ""
    """Which file it was found in, so a report can point at the right sheet."""

    @property
    def library(self) -> str:
        return split_lib_id(self.lib_id)[0]

    @property
    def name(self) -> str:
        return split_lib_id(self.lib_id)[1]


def _property_value(node: SExp, name: str) -> str | None:
    for child in node.children:
        if not isinstance(child, SExp) or child.name != "property" or len(child) < 3:
            continue
        key, value = child[1], child[2]
        if isinstance(key, Atom) and key.value == name and isinstance(value, Atom):
            return value.value
    return None


def _placed_symbols(doc: Document | SExp) -> list[SExp]:
    """The symbols actually on the sheet, not the cached definitions.

    A placed symbol is a direct child of the root carrying a ``lib_id``; a
    cached one lives inside ``lib_symbols`` and carries its name inline. The
    distinction matters because only the first has a reference designator.
    """
    root = doc.root if isinstance(doc, Document) else doc
    return [
        child
        for child in root.children
        if isinstance(child, SExp)
        and child.name == "symbol"
        and child.find("lib_id", recursive=False) is not None
    ]


def cached_symbols(doc: Document | SExp) -> list[SExp]:
    """The ``(symbol "LIB:NAME" ...)`` definitions cached in ``lib_symbols``."""
    root = doc.root if isinstance(doc, Document) else doc
    block = root.find("lib_symbols", recursive=False)
    if block is None:
        return []
    return [c for c in block.children if isinstance(c, SExp) and c.name == "symbol"]


def iter_symbol_instances(doc: Document | SExp, *, sheet: str = "") -> list[SymbolInstance]:
    """Every placed symbol, in file order.

    ``KLM_ID`` is read from the instance and, failing that, from the cached
    definition it points at. KiCad copies library fields onto an instance when
    it is placed, but a symbol placed before klm ever touched the project has
    only whatever the cache holds.
    """
    cache = {}
    for node in cached_symbols(doc):
        if len(node) >= 2 and isinstance(node[1], Atom):
            cache[node[1].value] = node

    instances: list[SymbolInstance] = []
    for node in _placed_symbols(doc):
        lib_id_node = node.find("lib_id", recursive=False)
        if lib_id_node is None or len(lib_id_node) < 2 or not isinstance(lib_id_node[1], Atom):
            continue
        lib_id = lib_id_node[1].value
        cached = cache.get(lib_id)
        klm_id = _property_value(node, "KLM_ID")
        if not klm_id and cached is not None:
            klm_id = _property_value(cached, "KLM_ID")
        instances.append(
            SymbolInstance(
                lib_id=lib_id,
                reference=_property_value(node, "Reference") or "",
                klm_id=klm_id or None,
                footprint=_property_value(node, "Footprint") or None,
                sheet=sheet,
            )
        )
    return instances


def rewrite_lib_ids(doc: Document | SExp, mapping: dict[str, str]) -> int:
    """Rewrite placed symbols and the cached definitions together.

    ``mapping`` is keyed by whole ``lib_id``, not by nickname, so two parts from
    the same library can move to different names — which is exactly what happens
    when vendoring resolves a name collision.
    """
    changed = 0
    for node in _placed_symbols(doc):
        lib_id_node = node.find("lib_id", recursive=False)
        if lib_id_node is None or len(lib_id_node) < 2:
            continue
        target = lib_id_node[1]
        if isinstance(target, Atom) and target.value in mapping:
            replacement = mapping[target.value]
            if replacement != target.value:
                target.value = replacement
                changed += 1

    for node in cached_symbols(doc):
        target = node[1]
        if isinstance(target, Atom) and target.value in mapping:
            replacement = mapping[target.value]
            if replacement != target.value:
                _rename_cached(node, target.value, replacement)
                changed += 1
    return changed


def _rename_cached(symbol: SExp, old: str, new: str) -> None:
    """Rename a cached definition and the unit sub-symbols named after it.

    The parent carries the full ``LIB:NAME``; its units carry only the *bare*
    name (``BQ27441DRZR-G1A_1_1``, not ``BatteryCharger:BQ27441DRZR-G1A_1_1``).
    Prefixing a unit with the parent's whole new name produces
    ``NewLib:Part_Part_1_1``, which KiCad cannot associate with anything — the
    symbol then loads and renders nothing.
    """
    target = symbol[1]
    assert isinstance(target, Atom)
    target.value = new

    old_bare, new_bare = split_lib_id(old)[1], split_lib_id(new)[1]
    for child in symbol.children:
        if not isinstance(child, SExp) or child.name != "symbol" or len(child) < 2:
            continue
        unit = child[1]
        if not isinstance(unit, Atom):
            continue
        # Accept either spelling, since which one KiCad wrote depends on its
        # version, and guess at neither when it matches neither.
        for prefix, replacement in ((old, new), (old_bare, new_bare)):
            if unit.value.startswith(prefix):
                unit.value = replacement + unit.value[len(prefix) :]
                break


def rewrite_footprint_fields(doc: Document | SExp, mapping: dict[str, str]) -> int:
    """Rewrite the ``Footprint`` field on placed and cached symbols alike."""
    changed = 0
    for node in [*_placed_symbols(doc), *cached_symbols(doc)]:
        for child in node.children:
            if not isinstance(child, SExp) or child.name != "property" or len(child) < 3:
                continue
            key, value = child[1], child[2]
            if not (isinstance(key, Atom) and key.value == "Footprint"):
                continue
            if isinstance(value, Atom) and value.value in mapping:
                replacement = mapping[value.value]
                if replacement != value.value:
                    value.value = replacement
                    changed += 1
    return changed
