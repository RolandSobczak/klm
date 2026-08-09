"""Deciding whether a supplier's listing is the part you meant (docs/07 §5).

This is the fiddliest part of the adapter layer, and the failure mode is
expensive: a wrong auto-match puts the wrong component on a board. So the
design is asymmetric on purpose — a match must *earn* high confidence, and
anything short of certain is recorded at a lower one and surfaced by lint rule
P007 rather than silently trusted.

Two normalisations do most of the work:

* **Packaging suffixes.** `-TR`, `-REEL`, `/TR` distinguish order codes for
  what is electrically the same die in the same package. They belong on the
  Offer, not the Part (docs/02 §1), so they are stripped for comparison and
  remembered as the offer's packaging.
* **Manufacturer aliases.** The same company appears under three or four names
  across two suppliers. `ST`, `STMicro` and `STMicroelectronics` are one
  company; `TI` and `Texas Instruments` are one company.

What this module will not do is fuzzy-match. Edit distance over MPNs is how
`RC0402FR-074K7L` becomes `RC0402FR-074K7L` with a different tolerance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from klm.model import Confidence, Packaging
from klm.services.catalog import normalize_manufacturer

__all__ = [
    "MANUFACTURER_ALIASES",
    "PACKAGING_SUFFIXES",
    "MatchResult",
    "match_mpn",
    "normalize_mpn",
    "same_manufacturer",
    "strip_packaging",
]


#: Suffix → what it says about packaging. Ordered longest-first at use, so
#: `-T&R` is not partly consumed by a shorter pattern.
PACKAGING_SUFFIXES: dict[str, Packaging] = {
    "-TR": Packaging.REEL,
    "/TR": Packaging.REEL,
    "-T&R": Packaging.REEL,
    "-TRPBF": Packaging.REEL,
    "-REEL": Packaging.REEL,
    "-RL": Packaging.REEL,
    "-TAPE": Packaging.CUT_TAPE,
    "-CT": Packaging.CUT_TAPE,
    "-TUBE": Packaging.TUBE,
    "-TRAY": Packaging.TRAY,
    "-BULK": Packaging.BAG,
    "-ND": Packaging.UNKNOWN,
}

#: Alias → canonical manufacturer name. Keys are matched after normalisation,
#: so spelling, case and punctuation in the key are for the reader's benefit.
MANUFACTURER_ALIASES: dict[str, str] = {
    "st": "STMicroelectronics",
    "stmicro": "STMicroelectronics",
    "stmicroelectronics": "STMicroelectronics",
    "ti": "Texas Instruments",
    "texasinstruments": "Texas Instruments",
    "nxp": "NXP Semiconductors",
    "nxpsemiconductors": "NXP Semiconductors",
    "nxpsemiconductor": "NXP Semiconductors",
    "adi": "Analog Devices",
    "analogdevices": "Analog Devices",
    "analogdevicesinc": "Analog Devices",
    "linear": "Analog Devices",
    "lineartechnology": "Analog Devices",
    "maxim": "Analog Devices",
    "maximintegrated": "Analog Devices",
    "onsemi": "onsemi",
    "onsemiconductor": "onsemi",
    "onsemiconductormicro": "onsemi",
    "fairchild": "onsemi",
    "fairchildsemiconductor": "onsemi",
    "infineon": "Infineon",
    "infineontechnologies": "Infineon",
    "microchip": "Microchip",
    "microchiptechnology": "Microchip",
    "atmel": "Microchip",
    "yageo": "Yageo",
    "murata": "Murata",
    "murataelectronics": "Murata",
    "samsung": "Samsung Electro-Mechanics",
    "samsungelectromechanics": "Samsung Electro-Mechanics",
    "samsungelectronics": "Samsung Electro-Mechanics",
    "vishay": "Vishay",
    "vishayintertechnology": "Vishay",
    "kemet": "KEMET",
    "tdk": "TDK",
    "nichicon": "Nichicon",
    "panasonic": "Panasonic",
    "wurth": "Würth Elektronik",
    "wurthelektronik": "Würth Elektronik",
    "espressif": "Espressif Systems",
    "espressifsystems": "Espressif Systems",
    "diodes": "Diodes Incorporated",
    "diodesinc": "Diodes Incorporated",
    "diodesincorporated": "Diodes Incorporated",
    "rohm": "ROHM",
    "rohmsemiconductor": "ROHM",
    "nexperia": "Nexperia",
    "toshiba": "Toshiba",
    "renesas": "Renesas",
}

_WHITESPACE = re.compile(r"\s+")


def normalize_mpn(mpn: str) -> str:
    """Fold an MPN for comparison: case and whitespace only.

    Hyphens, slashes and dots are *significant* in part numbers and are left
    alone. `LM317T` and `LM317-T` are not reliably the same device, and a
    normalisation that says they are would auto-link them.
    """
    return _WHITESPACE.sub("", mpn).upper()


def strip_packaging(mpn: str) -> tuple[str, Packaging]:
    """Split an order code into its base MPN and what the suffix implies.

    Returns the MPN unchanged and ``Packaging.UNKNOWN`` when no suffix matches,
    so callers need no special case for the common one.
    """
    folded = normalize_mpn(mpn)
    for suffix in sorted(PACKAGING_SUFFIXES, key=len, reverse=True):
        if folded.endswith(suffix) and len(folded) > len(suffix):
            return folded[: -len(suffix)], PACKAGING_SUFFIXES[suffix]
    return folded, Packaging.UNKNOWN


def canonical_manufacturer(name: str) -> str:
    """The alias-resolved name, or the input unchanged when klm doesn't know it."""
    return MANUFACTURER_ALIASES.get(normalize_manufacturer(name), name.strip())


def same_manufacturer(left: str, right: str) -> bool:
    """True when two spellings name the same company."""
    if not left.strip() or not right.strip():
        return False
    if normalize_manufacturer(left) == normalize_manufacturer(right):
        return True
    return normalize_manufacturer(canonical_manufacturer(left)) == normalize_manufacturer(
        canonical_manufacturer(right)
    )


@dataclass(frozen=True, slots=True)
class MatchResult:
    confidence: Confidence
    reason: str
    packaging: Packaging = Packaging.UNKNOWN

    @property
    def auto_link(self) -> bool:
        """Whether klm links this without asking.

        ``low`` never auto-links. That is the whole point of the confidence: it
        is the boundary between "klm decided" and "a human decides".
        """
        return self.confidence is not Confidence.LOW


def match_mpn(
    part_mpn: str,
    part_manufacturer: str,
    offer_mpn: str,
    offer_manufacturer: str = "",
) -> MatchResult | None:
    """Score a candidate offer against a catalog part.

    ``None`` means the MPNs are not the same part at all. Everything else is a
    match with a stated confidence and a human-readable reason, which is what
    ends up on the offer row so a review can second-guess it later.
    """
    if not part_mpn.strip() or not offer_mpn.strip():
        return None

    exact = normalize_mpn(part_mpn) == normalize_mpn(offer_mpn)
    part_base, _ = strip_packaging(part_mpn)
    offer_base, packaging = strip_packaging(offer_mpn)

    if exact:
        reason = "exact MPN match"
    elif part_base == offer_base:
        reason = f"MPN matches after stripping packaging suffix ({packaging})"
    else:
        return None

    confidence = Confidence.HIGH if exact else Confidence.MEDIUM

    if not part_manufacturer.strip() or not offer_manufacturer.strip():
        # An MPN alone is usually but not always unique across manufacturers.
        # Not enough to auto-link at full confidence; enough to propose.
        confidence = _weaken(confidence)
        reason += "; manufacturer not stated"
    elif same_manufacturer(part_manufacturer, offer_manufacturer):
        if normalize_manufacturer(part_manufacturer) != normalize_manufacturer(
            offer_manufacturer
        ):
            confidence = _weaken(confidence)
            reason += f"; manufacturer via alias ({offer_manufacturer})"
    else:
        # Same MPN, different company. Occasionally a second source, usually a
        # mistake. Either way it is not klm's call.
        return MatchResult(
            Confidence.LOW,
            f"{reason}; but manufacturer differs "
            f"({part_manufacturer!r} vs {offer_manufacturer!r})",
            packaging,
        )

    return MatchResult(confidence, reason, packaging)


def _weaken(confidence: Confidence) -> Confidence:
    return Confidence.MEDIUM if confidence is Confidence.HIGH else Confidence.LOW
