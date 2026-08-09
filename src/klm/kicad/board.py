"""Reading and rewriting footprint references in a ``.kicad_pcb``.

The board is scanned for the same reason the schematic is, plus one of its own:
a board legitimately carries footprints that no symbol ever placed — mounting
holes, fiducials, logos, test points. Vendoring only what the schematic mentions
would produce a project that opens with a board full of unresolved footprints.
"""

from __future__ import annotations

from dataclasses import dataclass

from klm.kicad.schematic import split_lib_id
from klm.kicad.sexpr import Atom, Document, SExp

__all__ = ["FootprintInstance", "iter_footprints", "rewrite_footprint_ids"]


@dataclass(frozen=True)
class FootprintInstance:
    lib_id: str
    reference: str

    @property
    def library(self) -> str:
        return split_lib_id(self.lib_id)[0]

    @property
    def name(self) -> str:
        return split_lib_id(self.lib_id)[1]


def _footprint_nodes(doc: Document | SExp) -> list[SExp]:
    """Top-level footprints. ``module`` is the pre-6.0 spelling of the same node."""
    root = doc.root if isinstance(doc, Document) else doc
    return [
        child
        for child in root.children
        if isinstance(child, SExp) and child.name in ("footprint", "module") and len(child) >= 2
    ]


def _reference(node: SExp) -> str:
    """The reference designator, from either the property or the legacy text node."""
    for child in node.children:
        if not isinstance(child, SExp):
            continue
        if child.name == "property" and len(child) >= 3:
            key, value = child[1], child[2]
            if isinstance(key, Atom) and key.value == "Reference" and isinstance(value, Atom):
                return value.value
        if child.name == "fp_text" and len(child) >= 3:
            kind, value = child[1], child[2]
            if isinstance(kind, Atom) and kind.value == "reference" and isinstance(value, Atom):
                return value.value
    return ""


def iter_footprints(doc: Document | SExp) -> list[FootprintInstance]:
    instances: list[FootprintInstance] = []
    for node in _footprint_nodes(doc):
        name = node[1]
        if not isinstance(name, Atom):
            continue
        instances.append(FootprintInstance(lib_id=name.value, reference=_reference(node)))
    return instances


def rewrite_footprint_ids(doc: Document | SExp, mapping: dict[str, str]) -> int:
    """Point each footprint at its vendored equivalent. Returns the count changed."""
    changed = 0
    for node in _footprint_nodes(doc):
        target = node[1]
        if isinstance(target, Atom) and target.value in mapping:
            replacement = mapping[target.value]
            if replacement != target.value:
                target.value = replacement
                changed += 1
    return changed
