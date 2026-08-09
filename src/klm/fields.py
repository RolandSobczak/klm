"""The canonical symbol field schema.

One spelling per fact. The reason BOMs never line up across projects is that the
same fact is written four ways — ``MPN``, ``mpn``, ``Manufacturer_Part_Number``,
``Part Number`` — so klm fixes the spelling at generation time and the linter
enforces it thereafter (docs/05-field-schema-and-linting.md).

The alias map below is the other half: real libraries are full of historical
spellings, and renaming them to canonical form is a pure rename — no value is
touched — which is what makes `klm lint --fix` safe to apply mechanically.
"""

from __future__ import annotations

import re

__all__ = [
    "CANONICAL_ORDER",
    "DEFAULT_ALIASES",
    "KICAD_EMPTY_BY_DEFAULT",
    "KICAD_INTERNAL",
    "KICAD_RESERVED",
    "KLM_ID",
    "KNOWN_FIELDS",
    "OPTIONAL",
    "REQUIRED",
    "SUPPLIER_FIELDS",
    "alias_key",
    "is_kicad_internal",
    "is_klm_reserved",
]

#: Written into every generated symbol. The join key that makes library sync
#: exact rather than heuristic (docs/adr/0003).
KLM_ID = "KLM_ID"

#: Fields KiCad itself defines. Order matters: KiCad expects these first.
KICAD_RESERVED = ("Reference", "Value", "Footprint", "Datasheet")

#: KiCad's own symbol metadata, present in every stock symbol. Not part data —
#: it drives the library browser and the footprint filter list — so klm passes
#: it through and the linter says nothing about it.
KICAD_INTERNAL = ("ki_keywords", "ki_description", "ki_fp_filters", "ki_locked")

#: Fields KiCad creates empty on every new symbol. Their emptiness carries no
#: information, so reporting it once per part would be pure noise.
KICAD_EMPTY_BY_DEFAULT = ("Footprint", "Datasheet")

#: Every part must carry these.
REQUIRED = (*KICAD_RESERVED, KLM_ID, "MPN", "Manufacturer", "Description")

#: Spelled exactly as the JLCPCB assembly toolchain expects. Deviating costs
#: interoperability for no gain.
SUPPLIER_FIELDS = ("LCSC", "TME")

#: Fields klm understands but does not demand. Their presence is checked
#: per category (docs/05 §1, "required where applicable").
OPTIONAL = ("Package", "Tolerance", "Voltage", "Power", "Dielectric", "Current", "Frequency")

#: The order fields are emitted in, so generated libraries are byte-stable.
CANONICAL_ORDER = (
    *KICAD_RESERVED,
    "MPN",
    "Manufacturer",
    "Description",
    "Package",
    "Tolerance",
    "Voltage",
    "Power",
    "Current",
    "Frequency",
    "Dielectric",
    *SUPPLIER_FIELDS,
    KLM_ID,
)

#: Every name klm claims. Anything else must be declared in config, which is
#: how a typo gets caught rather than silently becoming a new field.
KNOWN_FIELDS = (*REQUIRED, *OPTIONAL, *SUPPLIER_FIELDS)

#: Historical spellings, mapped to the canonical name. Polish entries are here
#: because libraries scavenged from local sources carry them (docs/05 §2).
DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "MPN": (
        "mpn",
        "Manufacturer_Part_Number",
        "Manufacturer Part Number",
        "Part Number",
        "PartNumber",
        "MFR_PN",
        "MFR#",
        "Mfg Part #",
        "MFP",
        "Numer katalogowy",
    ),
    "Manufacturer": ("manufacturer", "MFR", "Mfg", "Vendor", "Producent", "Maker", "Brand"),
    "Description": ("description", "Descr", "Opis", "Comment", "Note"),
    "LCSC": ("LCSC Part #", "LCSC_PN", "LCSC Part Number", "JLCPCB Part", "lcsc"),
    "TME": ("TME Symbol", "TME_PN", "TME Part", "tme"),
    "Package": ("package", "Case", "Case/Package", "Obudowa", "Footprint Type", "Housing"),
    "Tolerance": ("tolerance", "Tol", "Tolerancja"),
    "Voltage": ("voltage", "Voltage Rating", "Rated Voltage", "Napiecie", "Napięcie", "VDC"),
    "Power": ("power", "Power Rating", "Wattage", "Moc"),
    "Dielectric": ("dielectric", "Temperature Coefficient", "TempCo", "Dielectryk"),
    "Current": ("current", "Current Rating", "Rated Current", "Prad", "Prąd"),
    "Frequency": ("frequency", "Freq", "Czestotliwosc", "Częstotliwość"),
    "Datasheet": ("datasheet", "Datasheet URL", "DS", "Nota katalogowa"),
}

_SEPARATORS = re.compile(r"[\s_\-.#/]+")


def alias_key(name: str) -> str:
    """Fold a field name to its lookup key.

    Case, spaces, underscores, hyphens, dots, slashes and `#` all vary between
    libraries without meaning anything, so `mfr_pn`, `MFR PN` and `MfrPn`
    collapse to one key.
    """
    return _SEPARATORS.sub("", name).lower()


def is_kicad_internal(name: str) -> bool:
    """True for KiCad's own metadata fields, which klm neither owns nor judges."""
    return name.lower() in KICAD_INTERNAL


def is_klm_reserved(name: str) -> bool:
    """True for names in klm's own namespace, which users must not repurpose."""
    return name.upper().startswith("KLM_")


def sort_key(name: str) -> tuple[int, str]:
    """Sort canonical fields into their fixed order, unknown ones after, by name."""
    try:
        return (CANONICAL_ORDER.index(name), "")
    except ValueError:
        return (len(CANONICAL_ORDER), name)
