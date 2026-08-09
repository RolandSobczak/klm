"""Building and editing KiCad symbols.

Operates on the generic S-expression tree from :mod:`klm.kicad.sexpr`, touching
only the nodes it understands so that anything a future KiCad adds passes
through untouched.
"""

from __future__ import annotations

import re

from klm.kicad.sexpr import Atom, Document, SExp

__all__ = [
    "extract_symbols",
    "make_library",
    "rename_symbol",
    "sanitize_name",
    "set_property",
    "symbol_name",
]

#: KiCad reserves these in library identifiers; `/` also separates a hierarchy.
_UNSAFE_IN_NAME = re.compile(r"[/:\\\s]+")


def sanitize_name(name: str) -> str:
    """Make ``name`` safe to use as a KiCad symbol or footprint name."""
    cleaned = _UNSAFE_IN_NAME.sub("_", name.strip())
    return cleaned or "unnamed"


def symbol_name(symbol: SExp) -> str | None:
    """The name of a ``(symbol "NAME" ...)`` node."""
    if symbol.name != "symbol" or len(symbol) < 2:
        return None
    second = symbol[1]
    return second.value if isinstance(second, Atom) else None


def extract_symbols(doc: Document | SExp) -> list[SExp]:
    """Return the top-level symbols of a library, or the node itself if bare.

    An asset may have been stored either as a whole ``kicad_symbol_lib`` file or
    as a single ``(symbol ...)`` fragment; both are accepted.
    """
    root = doc.root if isinstance(doc, Document) else doc
    if root.name == "kicad_symbol_lib":
        return [c for c in root.children if isinstance(c, SExp) and c.name == "symbol"]
    if root.name == "symbol":
        return [root]
    return []


def rename_symbol(symbol: SExp, new_name: str) -> None:
    """Rename a symbol and its unit sub-symbols.

    KiCad names units after their parent (``AMS1117-3.3_0_1``). Renaming the
    parent without them produces a library that loads but renders nothing, so
    the prefix is rewritten on every child too.
    """
    old_name = symbol_name(symbol)
    if old_name is None:
        raise ValueError("not a symbol node")

    target = symbol[1]
    assert isinstance(target, Atom)
    target.value = new_name

    for child in symbol.children:
        if not isinstance(child, SExp) or child.name != "symbol":
            continue
        unit_name = symbol_name(child)
        if unit_name is None:
            continue
        unit_atom = child[1]
        assert isinstance(unit_atom, Atom)
        if unit_name.startswith(old_name):
            unit_atom.value = new_name + unit_name[len(old_name) :]
        else:
            # A unit that does not follow the convention still has to end up
            # under the new parent, or KiCad will not associate the two.
            unit_atom.value = f"{new_name}_{unit_name}"


def find_property(symbol: SExp, name: str) -> SExp | None:
    for node in symbol.children:
        if (
            isinstance(node, SExp)
            and node.name == "property"
            and len(node) >= 2
            and isinstance(node[1], Atom)
            and node[1].value == name
        ):
            return node
    return None


def set_property(symbol: SExp, name: str, value: str, *, hidden: bool = True) -> None:
    """Set a symbol property, creating it if absent.

    An existing property keeps its position, placement and formatting — only the
    value changes. That matters because a hand-placed ``Reference`` should not
    jump because klm rewrote the field.
    """
    existing = find_property(symbol, name)
    if existing is not None:
        if len(existing) >= 3 and isinstance(existing[2], Atom):
            existing[2].value = value
        else:  # pragma: no cover - malformed property, repaired rather than lost
            existing.children.insert(2, Atom(value, quoted=True))
        return

    node = SExp(
        [
            Atom("property"),
            Atom(name, quoted=True),
            Atom(value, quoted=True),
            SExp([Atom("at"), Atom("0"), Atom("0"), Atom("0")]),
            SExp(
                [
                    Atom("effects"),
                    SExp([Atom("font"), SExp([Atom("size"), Atom("1.27"), Atom("1.27")])]),
                    *([SExp([Atom("hide"), Atom("yes")])] if hidden else []),
                ]
            ),
        ]
    )
    # Properties belong before the graphical units, which KiCad emits last.
    insert_at = len(symbol.children)
    for index, child in enumerate(symbol.children):
        if isinstance(child, SExp) and child.name == "symbol":
            insert_at = index
            break
    symbol.children.insert(insert_at, node)


def make_library(symbols: list[SExp], *, generator: str, version: str) -> SExp:
    """Wrap symbols in a ``kicad_symbol_lib`` root.

    ``version`` is KiCad's file-format stamp, taken from the source assets so
    klm does not assert a format it has not seen.
    """
    return SExp(
        [
            Atom("kicad_symbol_lib"),
            SExp([Atom("version"), Atom(version)]),
            SExp([Atom("generator"), Atom(generator, quoted=True)]),
            *symbols,
        ]
    )
