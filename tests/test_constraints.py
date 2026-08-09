"""Tests for constraint → parameter-ID resolution.

The thing being protected is the reason this module exists: the research agent
must never supply a supplier's parameter identifier, because a wrong one does
not error — it returns confidently wrong parts. So klm does the mapping, and
what matters is that it never *silently* does it badly:

* a constraint it cannot map is reported, not dropped;
* a value it cannot read is skipped and named, not guessed at;
* a name that nearly matches is not treated as a match.
"""

from __future__ import annotations

import pytest

from klm.suppliers.constraints import (
    Comparison,
    ParameterValue,
    SupplierParameter,
    parse_constraint,
    resolve,
)
from klm.units import ValueParseError, parse_value


def parameter(name: str, *values: str, parameter_id: str = "2") -> SupplierParameter:
    return SupplierParameter(
        parameter_id=parameter_id,
        name=name,
        values=tuple(ParameterValue(str(i), text) for i, text in enumerate(values, start=100)),
    )


# ---------------------------------------------------------------------------
# Reading a constraint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "comparison", "value"),
    [
        (">=18V", Comparison.AT_LEAST, 18.0),
        ("=> 18 V", Comparison.AT_LEAST, 18.0),
        (">18V", Comparison.AT_LEAST, 18.0),
        ("<=6.5V", Comparison.AT_MOST, 6.5),
        ("=3.3V", Comparison.EQUAL, 3.3),
        ("100nF", Comparison.EQUAL, 100e-9),
        ("4k7", Comparison.EQUAL, 4700.0),
    ],
)
def test_constraints_are_read_in_human_units(text, comparison, value) -> None:
    constraint = parse_constraint(text)
    assert constraint.comparison is comparison
    assert constraint.low.value == pytest.approx(value)


def test_a_bare_value_means_equality_not_a_minimum() -> None:
    """`100nF` asks for a 100 nF capacitor, not for everything above it."""
    constraint = parse_constraint("100nF")
    assert constraint.satisfied_by(parse_value("100nF"))
    assert not constraint.satisfied_by(parse_value("220nF"))


def test_a_range_reads_as_between() -> None:
    constraint = parse_constraint("4.5..18V")
    assert constraint.comparison is Comparison.BETWEEN
    assert constraint.satisfied_by(parse_value("12V"))
    assert not constraint.satisfied_by(parse_value("24V"))


def test_a_backwards_range_is_refused() -> None:
    with pytest.raises(ValueParseError, match="backwards"):
        parse_constraint("18..4.5V")


def test_bounds_are_inclusive() -> None:
    assert parse_constraint(">=18V").satisfied_by(parse_value("18V"))
    assert parse_constraint("<=6.5V").satisfied_by(parse_value("6.5V"))


def test_a_different_unit_never_satisfies() -> None:
    """A category mixes volts, amps and degrees; comparing across them has no answer."""
    assert not parse_constraint(">=18V").satisfied_by(parse_value("20A"))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_a_constraint_becomes_the_ids_that_satisfy_it() -> None:
    parameters = [parameter("Vin max", "6.5 V", "18 V", "24 V", "36 V")]
    result = resolve(parameters, {"Vin max": ">=18V"})

    assert result.complete
    (group,) = result.groups
    assert group.parameter_id == "2"
    # 18, 24 and 36 — not 6.5, and not the parameter's own id by mistake.
    assert group.value_ids == ("101", "102", "103")


def test_the_name_match_ignores_case_spacing_and_punctuation() -> None:
    parameters = [parameter("Input voltage (max)", "18 V", "36 V")]
    result = resolve(parameters, {"input_voltage_max": ">=18V"})
    assert result.complete and result.groups[0].value_ids == ("100", "101")


def test_a_near_miss_on_the_name_is_not_a_match() -> None:
    """Guessing 'Output voltage' for 'voltage' filters the wrong axis and says it worked."""
    parameters = [parameter("Output voltage", "3.3 V", "5 V")]
    result = resolve(parameters, {"voltage": ">=3V"})

    assert not result.complete
    name, why = result.unmapped[0]
    assert name == "voltage"
    assert "no such parameter" in why
    assert "Output voltage" in why, "say what the category does have"


def test_an_unmappable_constraint_is_reported_never_dropped() -> None:
    """Dropping it would widen the search and present the result as filtered."""
    parameters = [parameter("Vin max", "6.5 V", "12 V")]
    result = resolve(parameters, {"Vin max": ">=18V"})

    assert result.groups == []
    assert not result.complete
    assert "satisfies" in result.unmapped[0][1]


def test_unreadable_values_are_skipped_and_named() -> None:
    """Supplier lists carry free text beside numbers. Guessing at it excludes parts."""
    parameters = [parameter("Vin max", "18 V", "see datasheet", "-", "36 V")]
    result = resolve(parameters, {"Vin max": ">=18V"})

    assert result.groups[0].value_ids == ("100", "103")
    assert ("Vin max", "see datasheet") in result.unparsed
    assert ("Vin max", "-") in result.unparsed


def test_an_unreadable_constraint_is_reported_with_its_text() -> None:
    parameters = [parameter("Vin max", "18 V")]
    result = resolve(parameters, {"Vin max": ">= lots"})
    assert "cannot read" in result.unmapped[0][1]


def test_several_constraints_become_several_groups() -> None:
    parameters = [
        parameter("Vin max", "6.5 V", "36 V", parameter_id="2"),
        parameter("Iout", "0.5 A", "1 A", "3 A", parameter_id="367"),
    ]
    result = resolve(parameters, {"Vin max": ">=18V", "Iout": ">=1A"})

    assert result.complete
    assert {g.parameter_id for g in result.groups} == {"2", "367"}
    assert dict((g.parameter_id, g.value_ids) for g in result.groups)["367"] == ("101", "102")


def test_explain_says_what_was_and_was_not_applied() -> None:
    parameters = [parameter("Vin max", "36 V", "see datasheet")]
    result = resolve(parameters, {"Vin max": ">=18V", "Iout": ">=1A"})

    lines = "\n".join(result.explain())
    assert "Vin max: 1 matching value(s)" in lines
    assert "Iout: not applied" in lines
    assert "could not read" in lines


def test_no_constraints_resolves_to_no_filter_and_is_complete() -> None:
    """Browsing a whole category is a legitimate search, not a failed one."""
    result = resolve([parameter("Vin max", "36 V")], {})
    assert result.complete and result.groups == []
