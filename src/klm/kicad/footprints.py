"""Editing KiCad footprints — chiefly the 3D model reference.

An absolute path inside a ``.kicad_mod`` is what makes a library unshareable:
it resolves on the machine that wrote it and nowhere else. Every footprint klm
stores or generates has its model path rewritten to a KiCad environment
variable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

from klm.kicad.sexpr import Atom, Document, Node, SExp

__all__ = [
    "PadInfo",
    "absolute_model_paths",
    "footprint_name",
    "graphics_on",
    "is_absolute_model_path",
    "iter_pads",
    "layer_of",
    "model_paths",
    "remove_model_nodes",
    "rewrite_model_paths",
    "segment_points",
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
    doc: Document | SExp,
    *,
    env_var: str = "KLM_3DMODELS",
    subdir: str = "",
    filename: str | None = None,
) -> int:
    """Point every model reference at ``${env_var}/<subdir>/<name>``.

    The basename is preserved unless ``filename`` overrides it, and the suffix
    is left alone — a footprint referencing a ``.wrl`` keeps referencing a
    ``.wrl`` until something actually converts it.

    ``subdir`` exists for vendored projects, whose models sit at a fixed path
    below ``${KIPRJMOD}`` rather than at the root of a variable of their own.

    Returns the number of references whose value actually changed, so a caller
    can tell "already correct" from "rewritten" — `klm lint --fix` reports a fix
    only when there was one.
    """
    root = doc.root if isinstance(doc, Document) else doc
    prefix = f"${{{env_var}}}/{subdir.strip('/')}" if subdir else f"${{{env_var}}}"
    count = 0
    for node in root.find_all("model"):
        if len(node) < 2 or not isinstance(node[1], Atom):
            continue
        target = node[1]
        name = filename or PurePath(target.value.replace("\\", "/")).name
        rewritten = f"{prefix}/{name}"
        if target.value != rewritten:
            target.value = rewritten
            count += 1
    return count


def remove_model_nodes(doc: Document | SExp) -> int:
    """Drop every ``(model ...)`` reference. Returns how many were removed.

    Used when vendoring without 3D models: leaving the references behind would
    give a collaborator a project whose every footprint points at a file the
    repository does not contain.
    """
    root = doc.root if isinstance(doc, Document) else doc
    before = len(root.children)
    root.children = [
        child
        for child in root.children
        if not (isinstance(child, SExp) and child.name == "model")
    ]
    return before - len(root.children)


@dataclass(frozen=True, slots=True)
class PadInfo:
    """One pad of a footprint, as the QA gate needs to see it."""

    number: str
    pad_type: str
    """`smd` | `thru_hole` | `np_thru_hole` | `connect`."""
    shape: str
    x: float
    y: float
    width: float
    height: float
    layers: tuple[str, ...] = ()

    @property
    def plated(self) -> bool:
        """Whether this pad is electrically connected to anything.

        A mounting hole is `np_thru_hole` and carries no pad number worth
        matching against a symbol pin, which is why the pin/pad count check
        has to ask.
        """
        return self.pad_type != "np_thru_hole"

    def overlaps(self, x: float, y: float, *, margin: float = 0.0) -> bool:
        """Whether a point falls inside the pad, expanded by ``margin``."""
        return (
            abs(x - self.x) <= self.width / 2 + margin
            and abs(y - self.y) <= self.height / 2 + margin
        )


def iter_pads(doc: Document | SExp) -> list[PadInfo]:
    """Every pad, in file order."""
    root = doc.root if isinstance(doc, Document) else doc
    pads: list[PadInfo] = []
    for node in root.find_all("pad"):
        if len(node) < 4:
            continue
        number, pad_type, shape = (_atom(node[i]) for i in (1, 2, 3))
        at = node.find("at", recursive=False)
        size = node.find("size", recursive=False)
        layers = node.find("layers", recursive=False)
        pads.append(
            PadInfo(
                number=number,
                pad_type=pad_type,
                shape=shape,
                x=_number(at, 1),
                y=_number(at, 2),
                width=_number(size, 1),
                height=_number(size, 2),
                layers=tuple(_atom(item) for item in (layers.children[1:] if layers else [])),
            )
        )
    return pads


def layer_of(node: SExp) -> str:
    """The layer a graphic item sits on, or `''` if it declares none."""
    layer = node.find("layer", recursive=False)
    return _atom(layer[1]) if layer is not None and len(layer) >= 2 else ""


def graphics_on(doc: Document | SExp, layer: str) -> list[SExp]:
    """Graphic items (`fp_line`, `fp_rect`, `fp_poly`, `fp_arc`, `fp_circle`) on a layer."""
    root = doc.root if isinstance(doc, Document) else doc
    kinds = ("fp_line", "fp_rect", "fp_poly", "fp_arc", "fp_circle")
    return [
        node
        for node in root.children
        if isinstance(node, SExp) and node.name in kinds and layer_of(node) == layer
    ]


def segment_points(node: SExp) -> list[tuple[float, float]]:
    """Every explicit coordinate of a graphic item, for extent and closure checks."""
    points: list[tuple[float, float]] = []
    for tag in ("start", "end", "center", "mid"):
        for child in node.find_all(tag):
            points.append((_number(child, 1), _number(child, 2)))
    for pts in node.find_all("pts"):
        for xy in pts.find_all("xy"):
            points.append((_number(xy, 1), _number(xy, 2)))
    return points


def _atom(item: Node | None) -> str:
    return item.value if isinstance(item, Atom) else ""


def _number(node: SExp | None, index: int) -> float:
    if node is None or len(node) <= index:
        return 0.0
    item = node[index]
    if not isinstance(item, Atom):
        return 0.0
    try:
        return float(item.value)
    except ValueError:
        return 0.0
