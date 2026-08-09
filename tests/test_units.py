"""Value parsing and formatting.

Tested harder than anything else in the codebase, because a value that parses
to the wrong number produces a BOM that looks right and orders the wrong part
(docs/02 §7).
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from klm.units import (
    OHM,
    Quantity,
    UnitError,
    ValueParseError,
    format_quantity,
    format_tolerance,
    parse_range,
    parse_tolerance,
    parse_value,
    try_parse_value,
)


@pytest.mark.parametrize(
    ("text", "expected", "unit"),
    [
        # The four spellings from docs/05 §3 that must collapse to one number.
        ("100n", 1e-7, "F"),
        ("0.1uF", 1e-7, None),
        ("100 nF", 1e-7, None),
        ("100NF", 1e-7, None),
        ("4k7", 4700.0, OHM),
        ("4700", 4700.0, OHM),
        ("4.7k", 4700.0, OHM),
        ("4K7Ω", 4700.0, None),
        ("1R2", 1.2, OHM),
        ("1.2R", 1.2, None),
        ("1.2 ohm", 1.2, None),
        ("10u", 1e-5, "F"),
        ("10µF", 1e-5, None),
        ("10uF", 1e-5, None),
        # Prefixes and units elsewhere in the schema.
        ("16V", 16.0, None),
        ("0.1W", 0.1, None),
        ("100mW", 0.1, None),
        ("2n2", 2.2e-9, "F"),
        ("1M", 1e6, OHM),
        ("1m", 1e-3, OHM),
        ("100kHz", 1e5, None),
        ("10mH", 1e-2, None),
        ("2.2uH", 2.2e-6, None),
        ("100R", 100.0, None),
    ],
)
def test_parses_real_spellings(text: str, expected: float, unit: str | None) -> None:
    assert parse_value(text, unit=unit).value == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (1e-7, "F", "100nF"),
        (1e-5, "F", "10µF"),
        (2.2e-9, "F", "2.2nF"),
        (4700.0, OHM, "4.7k"),
        (1.2, OHM, "1R2"),
        (100.0, OHM, "100R"),
        (1e6, OHM, "1M"),
        (0.05, OHM, "0R05"),
        (0.0, OHM, "0R"),
        (16.0, "V", "16V"),
        (0.1, "W", "100mW"),
        (1e5, "Hz", "100kHz"),
        (1e-7, None, "100n"),
    ],
)
def test_formats_canonically(value: float, unit: str | None, expected: str) -> None:
    assert format_quantity(Quantity(value, unit)) == expected


def test_milli_and_mega_are_never_case_folded() -> None:
    """`1m` and `1M` differ by a factor of a billion; guessing is not an option."""
    assert parse_value("1m", unit=OHM).value == pytest.approx(1e-3)
    assert parse_value("1M", unit=OHM).value == pytest.approx(1e6)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "4x7", "1E5", "nF", "..", "4.7.2k", "1R2R3", "1 2 3 k"],
)
def test_rejects_rather_than_guesses(text: str) -> None:
    with pytest.raises(ValueParseError):
        parse_value(text)
    assert try_parse_value(text) is None


def test_stated_unit_disagreeing_with_the_expected_one_is_an_error() -> None:
    with pytest.raises(ValueParseError):
        parse_value("100nF", unit=OHM)


def test_unknown_unit_is_an_error() -> None:
    with pytest.raises(UnitError):
        parse_value("10", unit="furlong")


def test_with_unit_verifies_rather_than_overwrites() -> None:
    assert parse_value("100n").with_unit("F").unit == "F"
    with pytest.raises(UnitError):
        parse_value("100nF").with_unit(OHM)


def test_matches_tolerates_display_rounding() -> None:
    assert parse_value("4.7k", unit=OHM).matches(Quantity(4700.0, OHM))
    assert not parse_value("4.7k", unit=OHM).matches(Quantity(4800.0, OHM))
    assert not Quantity(1.0, "F").matches(Quantity(1.0, OHM))


@pytest.mark.parametrize(
    ("text", "expected"),
    [("±1%", 0.01), ("1%", 0.01), ("0.01", 0.01), ("+/-5%", 0.05), (" 20 % ", 0.2)],
)
def test_parses_tolerance(text: str, expected: float) -> None:
    assert parse_tolerance(text) == pytest.approx(expected)


def test_formats_tolerance() -> None:
    assert format_tolerance(0.01) == "1%"
    assert format_tolerance(0.005) == "0.5%"


@pytest.mark.parametrize("text", ["-40..85", "-40 to 85", "-40…85", "-40 .. 85"])
def test_parses_range(text: str) -> None:
    low, high = parse_range(text)
    assert (low.value, high.value) == (-40.0, 85.0)


def test_range_unit_carries_from_the_high_bound() -> None:
    low, high = parse_range("1k..10k", unit=OHM)
    assert (low.value, high.value, high.unit) == (1000.0, 10000.0, OHM)


def test_backwards_range_is_an_error() -> None:
    with pytest.raises(ValueParseError):
        parse_range("85..-40")


# -- properties --------------------------------------------------------


_MAGNITUDES = st.floats(min_value=1e-12, max_value=1e11, allow_nan=False, allow_infinity=False)
_UNITS = st.sampled_from([OHM, "F", "H", "V", "A", "W", "Hz", None])


@given(value=_MAGNITUDES, unit=_UNITS)
def test_format_then_parse_recovers_the_value(value: float, unit: str | None) -> None:
    """`parse(format(v)) == v` to the precision the display form carries."""
    quantity = Quantity(value, unit)
    reparsed = parse_value(format_quantity(quantity), unit=unit)
    assert math.isclose(reparsed.value, value, rel_tol=5e-3)
    assert reparsed.unit == unit


@given(value=_MAGNITUDES, unit=_UNITS)
def test_formatting_is_idempotent(value: float, unit: str | None) -> None:
    """`format(parse(format(v)))` is stable, so lint --fix converges."""
    once = format_quantity(Quantity(value, unit))
    twice = format_quantity(parse_value(once, unit=unit))
    assert once == twice


@given(value=_MAGNITUDES, unit=_UNITS)
def test_formatted_values_never_use_exponent_notation(value: float, unit: str | None) -> None:
    text = format_quantity(Quantity(value, unit))
    assert "e" not in text.lower() or text.lower().endswith("hz")


@given(
    st.sampled_from(["100n", "4k7", "1R2", "10uF", "0.1", "22p", "4.7k", "1M5"]),
    _UNITS,
)
def test_parse_then_format_round_trips(text: str, unit: str | None) -> None:
    quantity = try_parse_value(text, unit=unit)
    assume(quantity is not None)
    assert quantity is not None
    assert parse_value(format_quantity(quantity), unit=unit).matches(quantity, rel_tol=5e-3)
