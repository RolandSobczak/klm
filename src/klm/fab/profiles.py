"""House-specific formatting, kept out of the pipeline.

Steps 1-4 of the fab pipeline are generic: preflight, plot, place, group. Steps
5-6 are where a particular house wants particular column names, particular units
and a particular idea of which way "up" is. Putting that behind a profile is
what stops JLCPCB's quirks from becoming klm's behaviour, and it makes adding
another house a data change (docs/09 §6).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = ["JLCPCB", "FabProfile", "Placement", "profile_for", "render_csv"]


@dataclass(frozen=True)
class Placement:
    """One component as it will be placed, after corrections."""

    reference: str
    value: str
    footprint: str
    x: float
    y: float
    rotation: float
    side: str
    """`top` or `bottom`, in klm's spelling — the profile renames it."""
    klm_id: str = ""
    """Which catalog part this is. Carried into the manifest, so `klm fab
    feedback` can map a reference back to a part without re-reading the
    schematic — which by then may have moved on."""
    lcsc: str = ""
    correction_source: str = "none"
    confirmed: bool = False


@dataclass(frozen=True)
class FabProfile:
    """Everything about an output that is the fabricator's preference."""

    name: str
    gerber_protel_extensions: bool = True
    drill_merge_pth_npth: bool = False
    cpl_columns: Sequence[str] = ("Designator", "Mid X", "Mid Y", "Layer", "Rotation")
    cpl_units: str = "mm"
    cpl_layer_names: dict[str, str] = field(
        default_factory=lambda: {"top": "Top", "bottom": "Bottom"}
    )
    bom_columns: Sequence[str] = ("Comment", "Designator", "Footprint", "LCSC Part #")
    apply_rotation_corrections: bool = True
    mirror_bottom_rotation: bool = False
    """Whether the house reads bottom-side angles mirrored from KiCad's.

    KiCad mirrors components on the bottom layer and JLCPCB does not, which is a
    whole-layer transform rather than a per-package correction — so it belongs
    here and not in the correction table (docs/adr/0011).
    """
    decimals: int = 4

    # -- rendering ------------------------------------------------------

    def cpl_row(self, placement: Placement) -> dict[str, str]:
        rotation = placement.rotation
        if self.mirror_bottom_rotation and placement.side == "bottom":
            rotation = (180.0 - rotation) % 360.0
        return {
            "Designator": placement.reference,
            "Mid X": self._number(placement.x),
            "Mid Y": self._number(placement.y),
            "Layer": self.cpl_layer_names.get(placement.side, placement.side),
            "Rotation": self._number(rotation % 360.0),
            "Val": placement.value,
            "Package": placement.footprint,
        }

    def bom_row(self, line: object) -> dict[str, str]:
        from klm.services.bom import BomLine

        assert isinstance(line, BomLine)
        return {
            "Comment": line.value,
            "Designator": line.designators,
            "Footprint": line.footprint_name,
            "LCSC Part #": line.lcsc,
            "Quantity": str(line.quantity),
            "MPN": line.mpn,
            "Manufacturer": line.manufacturer,
        }

    def _number(self, value: float) -> str:
        """Fixed precision, so the same board exports byte-identically twice."""
        text = f"{value:.{self.decimals}f}"
        return "0" if text.strip("-0.") == "" else text


JLCPCB = FabProfile(
    name="jlcpcb",
    gerber_protel_extensions=True,
    drill_merge_pth_npth=False,
    cpl_columns=("Designator", "Mid X", "Mid Y", "Layer", "Rotation"),
    bom_columns=("Comment", "Designator", "Footprint", "LCSC Part #"),
    apply_rotation_corrections=True,
    mirror_bottom_rotation=True,
)

#: A house that wants KiCad's own conventions untouched — the escape hatch for
#: anyone whose fabricator accepts a plain position file.
GENERIC = FabProfile(
    name="generic",
    gerber_protel_extensions=False,
    cpl_columns=("Designator", "Mid X", "Mid Y", "Layer", "Rotation", "Val", "Package"),
    bom_columns=("Comment", "Designator", "Footprint", "Quantity", "MPN", "Manufacturer"),
    apply_rotation_corrections=False,
    mirror_bottom_rotation=False,
)

_PROFILES = {profile.name: profile for profile in (JLCPCB, GENERIC)}


def profile_for(name: str) -> FabProfile:
    try:
        return _PROFILES[name.lower()]
    except KeyError:
        known = ", ".join(sorted(_PROFILES))
        raise KeyError(f"no fab profile named {name!r} (known: {known})") from None


def render_csv(columns: Sequence[str], rows: Sequence[dict[str, str]]) -> str:
    """CSV with CRLF line endings, which is what every fab's parser expects.

    Written through `csv` rather than by joining strings, because a value
    containing a comma — a description, a tolerance like "1%, 100ppm" — would
    otherwise shift every column after it and the file would still look fine.
    """
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\r\n"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue()
