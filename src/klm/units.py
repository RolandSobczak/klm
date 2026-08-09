"""Parsing and formatting of component values.

`100n`, `0.1uF`, `100 nF` and `100NF` are the same capacitor. Until they are
stored as one number, no two BOMs can be merged and no two parts can be
recognised as duplicates — so this module is where BOM-merging correctness
actually lives (docs/02 §7, docs/05 §3).

Two rules shape everything here:

* **Store SI, derive display.** A value is a float in base units plus a unit
  symbol. Every spelling collapses to the same float; the display form is
  computed, never stored.
* **Never guess.** Anything ambiguous raises rather than picking a reading.
  A wrong value that looks plausible is worse than a reported failure.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = [
    "OHM",
    "Quantity",
    "UnitError",
    "ValueParseError",
    "format_quantity",
    "format_tolerance",
    "normalize_unit",
    "parse_range",
    "parse_tolerance",
    "parse_value",
    "try_parse_value",
]

#: U+03A9 GREEK CAPITAL LETTER OMEGA. KiCad, TME and LCSC all use this one;
#: U+2126 OHM SIGN looks identical and is normalised away on input.
OHM = "Ω"

MICRO = "µ"


class ValueParseError(ValueError):
    """The text is not a value klm is willing to interpret."""


class UnitError(ValueError):
    """The unit is not one klm models."""


#: Scale factors by symbol. Deliberately small: an SI prefix table that
#: includes `f` would make `4f7` parse, and `F` is a unit klm cares about.
_PREFIX_SCALE = {
    "G": 1e9,
    "M": 1e6,
    "k": 1e3,
    "m": 1e-3,
    MICRO: 1e-6,
    "n": 1e-9,
    "p": 1e-12,
}

#: Spellings that map onto a prefix. `m`/`M` are absent because milli and mega
#: differ only by case: accepting either case would silently turn 1mΩ into 1MΩ.
_PREFIX_ALIASES = {
    "g": "G",
    "K": "k",
    "u": MICRO,
    "U": MICRO,
    "μ": MICRO,  # U+03BC GREEK SMALL LETTER MU
    "N": "n",
    "P": "p",
}

#: Descending, for choosing an engineering prefix on output.
_PREFIX_STEPS: tuple[tuple[float, str], ...] = (
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
    (1.0, ""),
    (1e-3, "m"),
    (1e-6, MICRO),
    (1e-9, "n"),
    (1e-12, "p"),
)

#: Unit spellings klm accepts, mapped to the canonical symbol. Matched
#: longest-first and case-insensitively, so `nf`, `NF` and `nF` agree.
_UNIT_ALIASES = {
    "ohms": OHM,
    "ohm": OHM,
    OHM: OHM,
    "Ω": OHM,  # U+2126 OHM SIGN
    "f": "F",
    "h": "H",
    "v": "V",
    "a": "A",
    "w": "W",
    "hz": "Hz",
    "s": "s",
}

_UNIT_SUFFIXES = tuple(sorted(_UNIT_ALIASES, key=len, reverse=True))

_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)$")
#: `4k7`, `1R2`, `2n2` — the prefix stands in for the decimal point.
_R_NOTATION = re.compile(r"^([+-]?\d+)([A-Za-zµμΩΩ])(\d+)$")


@dataclass(frozen=True)
class Quantity:
    """A value in SI base units, with the unit it is measured in."""

    value: float
    unit: str | None = None
    """`None` when the text carried no unit — `100n` is a number until a
    category says what kind of component it belongs to."""

    def __str__(self) -> str:
        return format_quantity(self)

    def with_unit(self, unit: str) -> Quantity:
        """Attach a unit to a bare number, or verify the one already present."""
        canonical = normalize_unit(unit)
        if self.unit is not None and self.unit != canonical:
            raise UnitError(f"value is in {self.unit}, not {canonical}")
        return Quantity(self.value, canonical)

    def matches(self, other: Quantity, *, rel_tol: float = 1e-9) -> bool:
        """Equality for values that travelled through decimal text.

        `4.7k` and `4700` are the same resistor; exact float equality would
        disagree the moment one of them was rounded for display.
        """
        if self.unit is not None and other.unit is not None and self.unit != other.unit:
            return False
        return math.isclose(self.value, other.value, rel_tol=rel_tol, abs_tol=0.0)


def normalize_unit(unit: str) -> str:
    """Canonical symbol for a unit spelling."""
    canonical = _UNIT_ALIASES.get(unit) or _UNIT_ALIASES.get(unit.lower())
    if canonical is None:
        raise UnitError(f"unknown unit {unit!r}")
    return canonical


def parse_value(text: str, *, unit: str | None = None) -> Quantity:
    """Read a component value.

    ``unit`` is the unit the caller expects — supplied from the part's
    category, so that a bare `4k7` on a resistor becomes 4700 Ω. It is a
    default, not an override: text that states a different unit is an error,
    because that disagreement is exactly the kind of thing worth reporting.
    """
    expected = normalize_unit(unit) if unit is not None else None
    raw = text.strip()
    if not raw:
        raise ValueParseError("empty value")

    # Whitespace separates the number from its prefix and unit — `100 nF`,
    # `4.7 k` — and does nothing else. Collapsing it everywhere would turn
    # `1 2 3 k` into 123k, which is guessing.
    tokens = raw.split()
    if len(tokens) > 2 or (len(tokens) == 2 and not _NUMBER.match(tokens[0])):
        raise ValueParseError(f"cannot parse {raw!r} as a value")
    body = "".join(tokens)
    body, found_unit = _split_unit(body)
    magnitude = _parse_magnitude(body, raw)

    if found_unit is not None and expected is not None and found_unit != expected:
        raise ValueParseError(f"{raw!r} is in {found_unit}, not the expected {expected}")
    return Quantity(magnitude, found_unit or expected)


def try_parse_value(text: str, *, unit: str | None = None) -> Quantity | None:
    """:func:`parse_value`, returning ``None`` instead of raising."""
    try:
        return parse_value(text, unit=unit)
    except ValueError:
        return None


def _split_unit(body: str) -> tuple[str, str | None]:
    """Strip a trailing unit, leaving the number and any SI prefix.

    `R` is deliberately not a unit suffix here. Trailing `R` on a resistor
    means ohms (`100R`), but `R` between digits means a decimal point
    (`1R2`), and both are handled where the number itself is read.
    """
    lowered = body.lower()
    for suffix in _UNIT_SUFFIXES:
        if len(body) <= len(suffix):
            continue
        if lowered.endswith(suffix.lower()):
            return body[: -len(suffix)], _UNIT_ALIASES[suffix]
    return body, None


def _parse_magnitude(body: str, raw: str) -> float:
    if not body:
        raise ValueParseError(f"{raw!r} has no numeric part")

    r_match = _R_NOTATION.match(body)
    if r_match is not None:
        whole, marker, fraction = r_match.groups()
        scale = 1.0 if marker in ("R", "r") else _lookup_prefix(marker, raw)
        # `4k7` is 4.7k: the marker sits where the decimal point would.
        sign = -1.0 if whole.startswith("-") else 1.0
        magnitude = abs(float(whole)) + float(f"0.{fraction}")
        return sign * magnitude * scale

    if body.endswith(("R", "r")) and _NUMBER.match(body[:-1]):
        return float(body[:-1])

    if _NUMBER.match(body):
        return float(body)

    number, marker = body[:-1], body[-1]
    if _NUMBER.match(number):
        return float(number) * _lookup_prefix(marker, raw)

    raise ValueParseError(f"cannot parse {raw!r} as a value")


def _lookup_prefix(marker: str, raw: str) -> float:
    symbol = marker if marker in _PREFIX_SCALE else _PREFIX_ALIASES.get(marker)
    if symbol is None:
        raise ValueParseError(f"{raw!r} has an unknown SI prefix {marker!r}")
    return _PREFIX_SCALE[symbol]


def format_quantity(quantity: Quantity, *, significant: int = 3) -> str:
    """Render a value the way it should appear in a `Value` field.

    Engineering notation, at most three significant figures, `µ` rather than
    `u`, and R-notation for resistors — the conventions are configurable in
    principle but defaulted in practice, because agreeing matters more than
    which convention wins.
    """
    mantissa, prefix = _engineering(quantity.value, significant)
    if quantity.unit == OHM:
        return _format_ohms(mantissa, prefix, quantity.value, significant)
    text = _trim(mantissa, significant)
    return f"{text}{prefix}{quantity.unit or ''}"


def _format_ohms(mantissa: float, prefix: str, value: float, significant: int) -> str:
    """Resistors get `R` where other units get their symbol.

    `1R2`, `100R`, `4.7k`. The `R` marks the decimal point below 10 Ω, which
    is the form that survives being written on a silkscreen.

    Below 0.01 Ω the R form starts eating significant figures (`0R000005`), so
    small values keep their SI prefix: a 5 mΩ shunt reads `5m`.
    """
    if value != 0.0 and 0.01 <= abs(value) < 10.0:
        text = _trim(value, significant)
        return text.replace(".", "R") if "." in text else f"{text}R"
    if value == 0.0:
        return "0R"
    text = _trim(mantissa, significant)
    return f"{text}{prefix or 'R'}"


def _engineering(value: float, significant: int) -> tuple[float, str]:
    """Split a value into a mantissa in [1, 1000) and an SI prefix."""
    if value == 0.0 or not math.isfinite(value):
        return value, ""
    magnitude = abs(value)
    for scale, prefix in _PREFIX_STEPS:
        if magnitude >= scale:
            mantissa = value / scale
            # Rounding for display can push 999.7 up to 1000; that belongs in
            # the next prefix, not printed as a four-digit mantissa.
            if prefix != "G" and abs(float(f"{mantissa:.{significant}g}")) >= 1000.0:
                return mantissa / 1000.0, _next_prefix(prefix)
            return mantissa, prefix
    # Smaller than a picofarad: keep the smallest prefix rather than invent one.
    return value / 1e-12, "p"


def _next_prefix(prefix: str) -> str:
    symbols = [symbol for _, symbol in _PREFIX_STEPS]
    return symbols[symbols.index(prefix) - 1]


def _trim(value: float, significant: int) -> str:
    """Fixed significant figures with no trailing zeros or exponent."""
    text = f"{value:.{significant}g}"
    if "e" in text or "E" in text:
        text = f"{value:f}".rstrip("0").rstrip(".")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


_TOLERANCE = re.compile(r"^[+-]?±?\s*([0-9.]+)\s*(%?)$")


def parse_tolerance(text: str) -> float:
    """Read a tolerance as a fraction: `±1%`, `1%` and `0.01` all give 0.01."""
    match = _TOLERANCE.match(text.strip().replace("+/-", "±"))
    if match is None:
        raise ValueParseError(f"cannot parse {text!r} as a tolerance")
    number, percent = match.groups()
    try:
        magnitude = float(number)
    except ValueError as exc:
        raise ValueParseError(f"cannot parse {text!r} as a tolerance") from exc
    return magnitude / 100.0 if percent else magnitude


def format_tolerance(fraction: float) -> str:
    return f"{_trim(fraction * 100.0, 3)}%"


_RANGE = re.compile(r"^(.+?)\s*(?:\.\.|\.\.\.|…|~|to)\s*(.+)$")


def parse_range(text: str, *, unit: str | None = None) -> tuple[Quantity, Quantity]:
    """Read `-40..85`, `-40 to 85`, `-40…85` as a low/high pair.

    The low bound may carry the unit for both, which is how datasheets write
    temperature ranges.
    """
    match = _RANGE.match(text.strip())
    if match is None:
        raise ValueParseError(f"cannot parse {text!r} as a range")
    low_text, high_text = match.groups()
    high = parse_value(high_text, unit=unit)
    low = parse_value(low_text, unit=high.unit)
    if low.value > high.value:
        raise ValueParseError(f"range {text!r} runs backwards")
    return low, high
