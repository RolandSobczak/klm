"""Turning a stored asset into a picture.

Thin on purpose: the drawing lives in :mod:`klm.kicad.render`, and this exists
so the CLI and the API reach it through one function rather than each assembling
"read the asset, parse it, pick a renderer" slightly differently.

The one judgement here is what to do when there is nothing to draw. A part with
no footprint is a normal state — it is what `klm assets acquire` is for — so it
returns a `PreviewError` naming the gap rather than an empty picture, and the
caller says so in its own idiom.
"""

from __future__ import annotations

from klm.kicad.render import footprint_svg, symbol_svg
from klm.kicad.sexpr import SExprError, loads
from klm.model import Part
from klm.store.assets import AssetError, AssetKind, AssetStore

__all__ = ["PreviewError", "render_asset", "render_part"]


class PreviewError(Exception):
    """Raised when there is nothing to draw, with the reason a human needs."""


def render_asset(store: AssetStore, content_hash: str, kind: AssetKind) -> str:
    """Render one stored asset to SVG."""
    if kind is AssetKind.MODEL3D:
        # Deliberately not attempted. A STEP file is a boundary representation;
        # rendering one means tessellation, and a wrong picture of a 3D model is
        # exactly the failure this project refuses elsewhere (docs/12 §4).
        raise PreviewError("3D models are not rendered — open the STEP in KiCad's 3D viewer")
    try:
        text = store.read_text(content_hash, kind)
    except AssetError as exc:
        raise PreviewError(str(exc)) from exc
    try:
        document = loads(text)
    except SExprError as exc:
        raise PreviewError(f"{kind.value} does not parse: {exc}") from exc

    return symbol_svg(document) if kind is AssetKind.SYMBOL else footprint_svg(document)


def render_part(store: AssetStore, part: Part, kind: AssetKind) -> str:
    """Render a part's symbol or footprint."""
    content_hash = {
        AssetKind.SYMBOL: part.symbol_hash,
        AssetKind.FOOTPRINT: part.footprint_hash,
        AssetKind.MODEL3D: part.model3d_hash,
    }[kind]
    if content_hash is None:
        raise PreviewError(f"{part.mpn} has no {kind.value} — `klm assets acquire` gets one")
    return render_asset(store, content_hash, kind)
