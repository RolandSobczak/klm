"""KiCad library tables — ``sym-lib-table`` and ``fp-lib-table``.

These are the user's files, shared with every other library they have installed.
klm reads, changes only the rows it owns, and writes back: never a wholesale
rewrite (docs/04 §6). The lossless S-expression writer means an untouched row
comes back byte-identical, including whatever formatting the user or KiCad gave it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from klm.kicad.sexpr import Atom, Document, SExp, dumps, loads

__all__ = ["LibEntry", "TableKind", "load_table", "read_entries", "upsert_entry"]


class TableKind:
    SYMBOL = "sym_lib_table"
    FOOTPRINT = "fp_lib_table"


@dataclass(frozen=True)
class LibEntry:
    name: str
    uri: str
    type: str = "KiCad"
    options: str = ""
    descr: str = ""


def load_table(path: Path, kind: str) -> Document:
    """Read a library table, or synthesise an empty one if absent.

    KiCad creates these on first run; klm may legitimately be the first to need
    one, so a missing file is not an error.
    """
    if path.exists():
        # newline="" keeps CRLF verbatim so the round-trip stays byte-exact.
        # Path.read_text gained a newline argument only in 3.13, and klm
        # supports 3.11.
        with open(path, encoding="utf-8", newline="") as handle:
            return loads(handle.read())
    root = SExp([Atom(kind), SExp([Atom("version"), Atom("7")], pre="\n  ")], pre_close="\n")
    return Document([root], trailing="\n")


def read_entries(doc: Document) -> list[LibEntry]:
    entries: list[LibEntry] = []
    for lib in doc.root.find_all("lib", recursive=False):
        fields = {
            child.name: child[1].value
            for child in lib.children
            if isinstance(child, SExp) and len(child) >= 2 and isinstance(child[1], Atom)
        }
        if "name" in fields:
            entries.append(
                LibEntry(
                    name=fields["name"],
                    uri=fields.get("uri", ""),
                    type=fields.get("type", "KiCad"),
                    options=fields.get("options", ""),
                    descr=fields.get("descr", ""),
                )
            )
    return entries


def upsert_entry(doc: Document, entry: LibEntry) -> bool:
    """Add or update one row. Returns True if the document changed.

    An existing row is edited in place rather than replaced, so its formatting
    and any fields klm does not model survive.
    """
    for lib in doc.root.find_all("lib", recursive=False):
        name_node = lib.find("name", recursive=False)
        if name_node is None or len(name_node) < 2 or not isinstance(name_node[1], Atom):
            continue
        if name_node[1].value != entry.name:
            continue

        changed = False
        for field_name, value in (
            ("type", entry.type),
            ("uri", entry.uri),
            ("options", entry.options),
            ("descr", entry.descr),
        ):
            node = lib.find(field_name, recursive=False)
            if node is not None and len(node) >= 2 and isinstance(node[1], Atom):
                if node[1].value != value:
                    node[1].value = value
                    changed = True
            else:
                lib.children.append(_field(field_name, value))
                changed = True
        return changed

    doc.root.children.append(_make_lib(entry))
    return True


def _field(name: str, value: str, *, first: bool = False) -> SExp:
    """One `(name "value")` pair, spaced the way KiCad writes these files."""
    return SExp([Atom(name), Atom(value, quoted=True, pre=" ")], pre=" " if first else "")


def _make_lib(entry: LibEntry) -> SExp:
    return SExp(
        [
            Atom("lib"),
            _field("name", entry.name, first=True),
            _field("type", entry.type),
            _field("uri", entry.uri),
            _field("options", entry.options),
            _field("descr", entry.descr),
        ],
        pre="\n  ",
    )


def write_table(doc: Document, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".klm-tmp")
    try:
        tmp.write_text(dumps(doc), encoding="utf-8", newline="")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
