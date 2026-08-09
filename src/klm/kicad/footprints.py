"""Editing KiCad footprints — chiefly the 3D model reference.

An absolute path inside a ``.kicad_mod`` is what makes a library unshareable:
it resolves on the machine that wrote it and nowhere else. Every footprint klm
stores or generates has its model path rewritten to a KiCad environment
variable.
"""

from __future__ import annotations

from pathlib import PurePath

from klm.kicad.sexpr import Atom, Document, SExp

__all__ = ["footprint_name", "model_paths", "rewrite_model_paths"]


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


def rewrite_model_paths(
    doc: Document | SExp, *, env_var: str = "KLM_3DMODELS", filename: str | None = None
) -> int:
    """Point every model reference at ``${env_var}/<name>``.

    The basename is preserved unless ``filename`` overrides it, and the suffix
    is left alone — a footprint referencing a ``.wrl`` keeps referencing a
    ``.wrl`` until something actually converts it.

    Returns the number of references rewritten, so a caller can report that a
    footprint had no model rather than silently succeeding.
    """
    root = doc.root if isinstance(doc, Document) else doc
    count = 0
    for node in root.find_all("model"):
        if len(node) < 2 or not isinstance(node[1], Atom):
            continue
        target = node[1]
        name = filename or PurePath(target.value.replace("\\", "/")).name
        target.value = f"${{{env_var}}}/{name}"
        count += 1
    return count
