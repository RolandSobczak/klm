"""The canonical symbol field schema.

One spelling per fact. The reason BOMs never line up across projects is that the
same fact is written four ways — ``MPN``, ``mpn``, ``Manufacturer_Part_Number``,
``Part Number`` — so klm fixes the spelling at generation time and the linter
enforces it thereafter (docs/05-field-schema-and-linting.md).

Phase 0 needs only the names and their order. Alias resolution, value
normalisation and the lint rules arrive in Phase 1.
"""

from __future__ import annotations

__all__ = [
    "CANONICAL_ORDER",
    "KICAD_RESERVED",
    "KLM_ID",
    "REQUIRED",
    "SUPPLIER_FIELDS",
    "is_klm_reserved",
]

#: Written into every generated symbol. The join key that makes library sync
#: exact rather than heuristic (docs/adr/0003).
KLM_ID = "KLM_ID"

#: Fields KiCad itself defines. Order matters: KiCad expects these first.
KICAD_RESERVED = ("Reference", "Value", "Footprint", "Datasheet")

#: Every part must carry these.
REQUIRED = (*KICAD_RESERVED, KLM_ID, "MPN", "Manufacturer", "Description")

#: Spelled exactly as the JLCPCB assembly toolchain expects. Deviating costs
#: interoperability for no gain.
SUPPLIER_FIELDS = ("LCSC", "TME")

#: The order fields are emitted in, so generated libraries are byte-stable.
CANONICAL_ORDER = (
    *KICAD_RESERVED,
    "MPN",
    "Manufacturer",
    "Description",
    "Package",
    *SUPPLIER_FIELDS,
    KLM_ID,
)


def is_klm_reserved(name: str) -> bool:
    """True for names in klm's own namespace, which users must not repurpose."""
    return name.upper().startswith("KLM_")


def sort_key(name: str) -> tuple[int, str]:
    """Sort canonical fields into their fixed order, unknown ones after, by name."""
    try:
        return (CANONICAL_ORDER.index(name), "")
    except ValueError:
        return (len(CANONICAL_ORDER), name)
