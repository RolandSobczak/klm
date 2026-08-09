"""Editing KiCad footprints — chiefly the 3D model reference.

An absolute path inside a ``.kicad_mod`` is what makes a library unshareable:
it resolves on the machine that wrote it and nowhere else. Every footprint klm
stores or generates has its model path rewritten to a KiCad environment
variable.
"""

from __future__ import annotations

import re
from pathlib import PurePath

from klm.kicad.sexpr import Atom, Document, SExp

__all__ = [
    "absolute_model_paths",
    "footprint_name",
    "is_absolute_model_path",
    "model_paths",
    "rewrite_model_paths",
]

#: `/home/rs/…`, `C:\…` and `\\server\…`. A `${VAR}/…` reference is portable
#: and a relative one resolves against the project, so neither is a problem.
_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\\\\)")


def footprint_name(doc: Document | SExp) -> str | None:
    """The name of a ``(footprint "NAME" ...)`` node."""
    root = doc.root if isinstance(doc, Document) else doc
    if root.name not in ("footprint", "module") or len(root) < 2:
        return None
    second = root[1]
    return second.value if isinstance(second, Atom) else None


def model_paths(doc: Document | SExp) -> list[str]:
    """Every 3D model path referenced by the footprint."""
    root = doc.root if isinstance(doc, Document) else doc
    out: list[str] = []
    for node in root.find_all("model"):
        if len(node) >= 2 and isinstance(node[1], Atom):
            out.append(node[1].value)
    return out


def is_absolute_model_path(path: str) -> bool:
    """True for a path that only resolves on the machine that wrote it."""
    return bool(_ABSOLUTE.match(path.strip()))


def absolute_model_paths(doc: Document | SExp) -> list[str]:
    """Model references that would not resolve on anyone else's machine."""
    return [path for path in model_paths(doc) if is_absolute_model_path(path)]


def rewrite_model_paths(
    doc: Document | SExp, *, env_var: str = "KLM_3DMODELS", filename: str | None = None
) -> int:
    """Point every model reference at ``${env_var}/<name>``.

    The basename is preserved unless ``filename`` overrides it, and the suffix
    is left alone — a footprint referencing a ``.wrl`` keeps referencing a
    ``.wrl`` until something actually converts it.

    Returns the number of references whose value actually changed, so a caller
    can tell "already correct" from "rewritten" — `klm lint --fix` reports a fix
    only when there was one.
    """
    root = doc.root if isinstance(doc, Document) else doc
    count = 0
    for node in root.find_all("model"):
        if len(node) < 2 or not isinstance(node[1], Atom):
            continue
        target = node[1]
        name = filename or PurePath(target.value.replace("\\", "/")).name
        rewritten = f"${{{env_var}}}/{name}"
        if target.value != rewritten:
            target.value = rewritten
            count += 1
    return count
