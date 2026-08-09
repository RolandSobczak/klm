"""Tests for the structured requirement.

What is being protected is the hard/soft split and the three places a
requirement could quietly lie:

* a constraint nobody checked reading as met;
* a typo'd section silently producing a requirement with no filters;
* a preference the candidate said nothing about being scored as though it had.
"""

from __future__ import annotations

import pytest

from klm.model import Lifecycle
from klm.research.requirement import (
    CheckStatus,
    ChoiceConstraint,
    NumericConstraint,
    Objective,
    Requirement,
    RequirementError,
    load_requirement,
    parse_requirement,
)
from klm.suppliers.constraints import ParameterValue, SupplierParameter, resolve

BUCK = {
    "kind": "buck_converter",
    "notes": "3.3 V rail on a battery board.",
    "constraints": {
        "vin": {"min": 4.5, "max": 18, "unit": "V"},
        "vout": {"value": 3.3, "unit": "V", "tolerance": 0.02},
        "iout": ">=1A",
        "topology": "synchronous",
        "package": {"allow": ["SOT-23-6", "SOIC-8", "QFN-*"]},
        "temp_range": {"min": -40, "max": 85, "unit": "°C"},
    },
    "preferences": [
        {"kind": "minimize", "what": "unit_price"},
        {"kind": "prefer", "what": "existing_footprint_in_catalog"},
        {"kind": "prefer", "what": "supplier", "value": "tme"},
    ],
    "sourcing": {"suppliers": ["tme", "lcsc"], "min_stock": 50, "lifecycle": ["active"]},
}


def requirement() -> Requirement:
    return parse_requirement(BUCK)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_the_documented_example_parses() -> None:
    spec = requirement()
    assert spec.kind == "buck_converter"
    assert len(spec.constraints) == 6
    assert spec.sourcing.lifecycle == (Lifecycle.ACTIVE,)
    assert spec.sourcing.min_stock == 50


def test_a_bare_string_is_text_unless_it_says_otherwise() -> None:
    """`0402` is a package. Reading it as the number 402 filters another axis."""
    spec = parse_requirement({"kind": "r", "constraints": {"package": "0402", "iout": ">=1A"}})
    assert isinstance(spec.constraint("package"), ChoiceConstraint)
    assert isinstance(spec.constraint("iout"), NumericConstraint)


def test_a_bare_number_is_refused_for_having_no_unit() -> None:
    with pytest.raises(RequirementError, match="no unit"):
        parse_requirement({"kind": "x", "constraints": {"vin": 18}})


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(RequirementError) as caught:
        parse_requirement(
            {
                "contraints": {},  # codespell:ignore
                "preferences": [{"kind": "sideways", "what": "price"}],
                "sourcing": {"suppliers": ["tem"]},
            }
        )
    problems = "\n".join(caught.value.problems)
    assert len(caught.value.problems) == 4
    assert "unknown section 'contraints'" in problems
    assert "'kind' is required" in problems
    assert "sideways" not in problems and "must be one of" in problems
    assert "unknown supplier(s) tem" in problems


def test_an_unknown_section_is_never_tolerated() -> None:
    """A misspelled section drops its constraints, and an unfiltered search
    returns plenty of results — so the failure looks like success."""
    with pytest.raises(RequirementError, match="unknown section"):
        parse_requirement({"kind": "x", "constrains": {"vin": ">=5V"}})


def test_prefer_inside_a_constraint_points_at_preferences() -> None:
    with pytest.raises(RequirementError, match="'prefer' is a preference"):
        parse_requirement(
            {"kind": "x", "constraints": {"package": {"allow": ["SOT-23-6"], "prefer": "SOT-23-6"}}}
        )


def test_min_above_max_is_refused() -> None:
    with pytest.raises(RequirementError, match="min is above max"):
        parse_requirement(
            {"kind": "x", "constraints": {"vin": {"min": 18, "max": 4.5, "unit": "V"}}}
        )


def test_value_cannot_be_combined_with_a_range() -> None:
    with pytest.raises(RequirementError, match="cannot be combined"):
        parse_requirement(
            {"kind": "x", "constraints": {"v": {"value": 3.3, "min": 3, "unit": "V"}}}
        )


def test_a_repeated_constraint_is_reported_not_overwritten() -> None:
    with pytest.raises(RequirementError, match="repeats"):
        parse_requirement({"kind": "x", "constraints": {"Vin max": ">=5V", "vin_max": ">=9V"}})


def test_a_preference_value_only_means_something_for_prefer() -> None:
    with pytest.raises(RequirementError, match="only means something"):
        parse_requirement(
            {"kind": "x", "preferences": [{"kind": "minimize", "what": "price", "value": "tme"}]}
        )


def test_lifecycle_defaults_to_no_filter_not_to_active() -> None:
    """Silently excluding NRND would hide the substitute search's best answers."""
    assert parse_requirement({"kind": "x"}).sourcing.lifecycle == ()


# ---------------------------------------------------------------------------
# Checking a candidate
# ---------------------------------------------------------------------------


def test_a_candidate_that_meets_everything_passes() -> None:
    report = requirement().check(
        {
            "vin": "4.5..18V",
            "vout": "3.3V",
            "iout": "2A",
            "topology": "synchronous",
            "package": "QFN-16",
            "temp_range": "-40..125°C",
        }
    )
    assert report.satisfied
    assert report.explain()[0] == "vin: required 4.5V..18V, actual 4.5V..18V — PASS"


def test_a_range_has_to_cover_the_requirement_not_merely_overlap_it() -> None:
    """A part rated 1.8-6.5 V does not run a 4.5-18 V rail, and the two overlap."""
    spec = requirement()
    assert spec.check({"vin": "1.8..6.5V"}).checks[0].status is CheckStatus.FAIL
    assert spec.check({"vin": "3..24V"}).checks[0].status is CheckStatus.PASS


def test_a_range_satisfies_a_one_sided_requirement_by_reaching_it() -> None:
    spec = requirement()
    assert spec.check({"iout": "0..3A"}).checks[2].status is CheckStatus.PASS
    assert spec.check({"iout": "0..0.5A"}).checks[2].status is CheckStatus.FAIL


def test_a_range_satisfies_an_equality_by_containing_it() -> None:
    """An adjustable regulator states 0.8-20 V and can be set to the 3.3 asked for."""
    assert requirement().check({"vout": "0.8..20V"}).checks[1].status is CheckStatus.PASS


def test_a_parameter_the_candidate_did_not_state_is_unknown_never_pass() -> None:
    report = requirement().check({"vout": "3.3V"})
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["vin"] is CheckStatus.UNKNOWN
    assert statuses["vout"] is CheckStatus.PASS
    assert not report.satisfied, "unknown is not met"
    assert report.possible, "but nothing has ruled it out either"


def test_a_value_klm_cannot_read_is_unknown_not_a_failure() -> None:
    """Failing it would reject the right part over a supplier's free text."""
    report = requirement().check({"iout": "see datasheet"})
    (check,) = [c for c in report.checks if c.name == "iout"]
    assert check.status is CheckStatus.UNKNOWN
    assert check.actual == "see datasheet"
    assert check.note


def test_a_failure_is_a_failure() -> None:
    report = requirement().check({"iout": "750mA"})
    (check,) = [c for c in report.checks if c.name == "iout"]
    assert check.status is CheckStatus.FAIL
    assert check.required == ">=1A"
    assert not report.possible


def test_tolerance_is_what_equality_means() -> None:
    spec = requirement()
    assert spec.check({"vout": "3.35V"}).checks[1].status is CheckStatus.PASS  # within ±2%
    assert spec.check({"vout": "3.5V"}).checks[1].status is CheckStatus.FAIL


def test_package_families_are_globs() -> None:
    spec = requirement()
    assert spec.check({"package": "qfn-24"}).checks[4].status is CheckStatus.PASS
    assert spec.check({"package": "TSSOP-8"}).checks[4].status is CheckStatus.FAIL


def test_parameter_names_are_matched_the_way_klm_matches_them_everywhere() -> None:
    report = requirement().check({"Temp range": "25°C"})
    (check,) = [c for c in report.checks if c.name == "temp_range"]
    assert check.status is CheckStatus.PASS


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

CANDIDATES = [
    {"unit_price": 0.42, "existing_footprint_in_catalog": False, "supplier": "lcsc"},
    {"unit_price": 1.10, "existing_footprint_in_catalog": True, "supplier": "tme"},
    {"unit_price": 2.00, "existing_footprint_in_catalog": True, "supplier": "tme"},
]


def test_preferences_order_candidates_and_say_why() -> None:
    ranked = requirement().rank(CANDIDATES)
    assert [r.index for r in ranked] == [1, 2, 0]
    assert dict(ranked[0].contributions)["minimize unit_price"] == pytest.approx(0.5696, abs=1e-3)


def test_a_preference_the_candidate_is_silent_about_is_not_scored() -> None:
    """Scoring it zero would punish a part for a field klm never fetched."""
    ranked = requirement().rank([{"unit_price": 1.0}, {"unit_price": 1.0, "supplier": "tme"}])
    quiet = next(r for r in ranked if r.index == 0)
    assert "prefer existing_footprint_in_catalog" in quiet.unscored
    assert quiet.score == 1.0, "unknowns leave the average alone"


def test_ranking_one_candidate_is_not_a_question() -> None:
    (only,) = requirement().rank([{"unit_price": 7.0, "supplier": "tme"}])
    assert only.score == 1.0


def test_weight_moves_the_order() -> None:
    spec = parse_requirement(
        {
            "kind": "x",
            "preferences": [
                {"kind": "minimize", "what": "unit_price", "weight": 10},
                {"kind": "prefer", "what": "supplier", "value": "tme"},
            ],
        }
    )
    ranked = spec.rank(
        [{"unit_price": 0.4, "supplier": "lcsc"}, {"unit_price": 2.0, "supplier": "tme"}]
    )
    assert ranked[0].index == 0


def test_preferences_never_disqualify() -> None:
    """Everything handed in comes back — ranking orders, it does not filter."""
    assert len(requirement().rank(CANDIDATES)) == len(CANDIDATES)


def test_no_preferences_leaves_the_order_alone() -> None:
    spec = parse_requirement({"kind": "x"})
    assert [r.index for r in spec.rank(CANDIDATES)] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Handing it onwards
# ---------------------------------------------------------------------------


def test_numeric_constraints_reach_a_supplier_search_without_a_round_trip() -> None:
    """The requirement's own Constraint is what the parametric resolver takes."""
    spec = requirement()
    parameters = [
        SupplierParameter(
            "2", "Iout", (ParameterValue("100", "0.5 A"), ParameterValue("101", "3 A"))
        )
    ]
    result = resolve(parameters, dict(spec.supplier_constraints()))
    (group,) = result.groups
    assert group.value_ids == ("101",)


def test_choice_constraints_are_checked_rather_than_searched_on() -> None:
    """A supplier's text values are not reliably matchable; a wrong exclusion is
    worse than a larger result set."""
    assert "package" not in requirement().supplier_constraints()
    assert "topology" not in requirement().supplier_constraints()


def test_the_prompt_keeps_hard_and_soft_apart() -> None:
    prompt = requirement().to_prompt()
    assert "a candidate that fails one does not qualify" in prompt
    assert "they never disqualify" in prompt
    assert "vin: 4.5V..18V" in prompt
    assert "vout: 3.3V ±2%" in prompt, "an equality reads the way it was asked for"


def test_a_requirement_round_trips_through_toml(tmp_path) -> None:
    spec = requirement()
    path = tmp_path / "requirement.toml"
    path.write_text(spec.to_toml(), encoding="utf-8")
    assert load_requirement(path) == spec


def test_writing_is_byte_stable() -> None:
    spec = requirement()
    assert spec.to_toml() == parse_requirement(spec.to_dict()).to_toml()


def test_a_broken_file_names_itself(tmp_path) -> None:
    path = tmp_path / "requirement.toml"
    path.write_text("kind = ", encoding="utf-8")
    with pytest.raises(RequirementError, match=r"requirement\.toml"):
        load_requirement(path)


def test_preferences_survive_the_round_trip_in_order() -> None:
    spec = requirement()
    again = parse_requirement(spec.to_dict())
    assert [p.what for p in again.preferences] == [p.what for p in spec.preferences]
    assert again.preferences[2].kind is Objective.PREFER
