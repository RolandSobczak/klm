"""The structured requirement — what "I need a part like this" means to klm.

Freeform text is a bad interface for part search: it makes the model guess at
which numbers are hard limits and which are wishes, and a guess in that split
is invisible in the output (docs/11 §2). So a requirement is structured, and
the split is the structure:

* **`constraints` are hard filters.** A candidate either meets one or does not.
* **`preferences` are the ranking function.** They order what survived; they
  never remove anything.

That separation buys the single most useful answer an agent can give when a
search comes back empty — *"nothing meets your hard constraints; the closest
miss is X, which fails only on Iout"* — because a near miss was never silently
dropped for being merely non-preferred.

Three rules the rest of klm will recognise:

* **A check that cannot run is `unknown`, never `pass`.** A candidate that
  simply does not state its input-voltage range has not met the constraint; it
  has not been checked. This is the same rule as the asset QA gate, for the
  same reason: a green result meaning "I didn't look" is worse than no result.
* **A value klm cannot read is `unknown`, not `fail`.** Failing it would reject
  the right part because a supplier wrote "see datasheet" in a column.
* **Nothing is guessed from shape.** A bare string is a *text* constraint
  unless it carries a comparison operator or a range — otherwise `package =
  "0402"` would read as the number 402 and filter on an axis nobody asked for.

Numeric constraints are :class:`klm.suppliers.constraints.Constraint`, which is
the type TME's parametric search already consumes. A requirement can therefore
be handed to a supplier search without a translation step in between — and the
translation that does happen (constraint → the supplier's value identifiers)
stays in one place.

Nothing here touches a network or a model.
"""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from klm.config import default_suppliers
from klm.model import Lifecycle
from klm.suppliers.constraints import (
    Comparison,
    Constraint,
    normalise_name,
    parse_constraint,
)
from klm.units import (
    Quantity,
    UnitError,
    ValueParseError,
    format_tolerance,
    parse_range,
    parse_value,
)

__all__ = [
    "Check",
    "CheckStatus",
    "ChoiceConstraint",
    "ConstraintReport",
    "NumericConstraint",
    "Objective",
    "Preference",
    "Ranked",
    "Requirement",
    "RequirementError",
    "Sourcing",
    "load_requirement",
    "parse_requirement",
]


class RequirementError(ValueError):
    """A requirement that cannot be used, with everything wrong with it.

    Every problem is collected rather than raising on the first, because a
    requirement is usually written by hand or drafted by the agent in one go.
    Fixing five mistakes one error message at a time is five round trips.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        joined = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(f"requirement has {len(self.problems)} problem(s):\n{joined}")


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Check:
    """One constraint, checked against one candidate."""

    name: str
    required: str
    actual: str | None
    status: CheckStatus
    note: str | None = None
    """Why a check is `unknown` — absent, or unreadable and how it was spelled."""

    def __str__(self) -> str:
        actual = self.actual if self.actual is not None else "not stated"
        line = f"{self.name}: required {self.required}, actual {actual} — {self.status.upper()}"
        return f"{line} ({self.note})" if self.note else line


@dataclass(frozen=True)
class NumericConstraint:
    """A requirement on a measurable quantity."""

    name: str
    constraint: Constraint
    tolerance: float | None = None
    """Fractional, and only meaningful for equality — `3.3 V ±2%` is
    `tolerance=0.02`. Absent means exact, which for a float that travelled
    through decimal text still means "close enough to be the same number"."""

    @property
    def required(self) -> str:
        if self.constraint.comparison is not Comparison.EQUAL:
            return str(self.constraint)
        # `==3.3 V` is how the supplier layer spells it; a requirement is read
        # by a person and by a model, and "3.3 V ±2%" is the same fact stated
        # the way it was asked for.
        text = str(self.constraint.low)
        return f"{text} ±{format_tolerance(self.tolerance)}" if self.tolerance else text

    def check(self, text: str | None) -> Check:
        """Check one stated parameter.

        A candidate may state a **range** where the requirement states one:
        "I need to run it from 4.5 to 18 V" against "the part accepts 4.5 to
        18 V". Those are not the same question as a single value, so the rule
        is *coverage* — the part's range has to contain what the requirement
        asks for. A part rated 1.8-6.5 V does not satisfy a 4.5-18 V rail even
        though the two overlap, and overlap is what a naive comparison would
        find.
        """
        if text is None or not text.strip():
            return Check(self.name, self.required, None, CheckStatus.UNKNOWN, "not stated")

        unit = self.constraint.low.unit
        try:
            low, high = parse_range(text, unit=unit)
        except (ValueParseError, UnitError):
            pass
        else:
            status = CheckStatus.PASS if self._covered_by(low, high) else CheckStatus.FAIL
            return Check(self.name, self.required, f"{low}..{high}", status)

        try:
            value = parse_value(text, unit=unit)
        except (ValueParseError, UnitError) as exc:
            return Check(self.name, self.required, text, CheckStatus.UNKNOWN, str(exc))
        satisfied = self.constraint.satisfied_by(value, rel_tol=self._rel_tol)
        status = CheckStatus.PASS if satisfied else CheckStatus.FAIL
        return Check(self.name, self.required, str(value), status)

    @property
    def _rel_tol(self) -> float:
        return self.tolerance if self.tolerance is not None else 1e-9

    def _covered_by(self, low: Quantity, high: Quantity) -> bool:
        inner = self.constraint
        if inner.comparison is Comparison.AT_LEAST:
            return self._at_least(high, inner.low)
        if inner.comparison is Comparison.AT_MOST:
            return self._at_most(low, inner.low)
        if inner.comparison is Comparison.EQUAL:
            return self._at_most(low, inner.low) and self._at_least(high, inner.low)
        assert inner.high is not None
        return self._at_most(low, inner.low) and self._at_least(high, inner.high)

    def _at_least(self, value: Quantity, bound: Quantity) -> bool:
        return value.value >= bound.value or value.matches(bound, rel_tol=self._rel_tol)

    def _at_most(self, value: Quantity, bound: Quantity) -> bool:
        return value.value <= bound.value or value.matches(bound, rel_tol=self._rel_tol)


@dataclass(frozen=True)
class ChoiceConstraint:
    """A requirement on something with names rather than magnitudes.

    Patterns are shell globs, folded for case, because packages come in
    families: `QFN-*` is a real requirement and enumerating it is not.
    """

    name: str
    allow: tuple[str, ...]

    @property
    def required(self) -> str:
        return " | ".join(self.allow)

    def check(self, text: str | None) -> Check:
        if text is None or not text.strip():
            return Check(self.name, self.required, None, CheckStatus.UNKNOWN, "not stated")
        candidate = text.strip().casefold()
        matched = any(fnmatch.fnmatchcase(candidate, pattern.casefold()) for pattern in self.allow)
        status = CheckStatus.PASS if matched else CheckStatus.FAIL
        return Check(self.name, self.required, text.strip(), status)


AnyConstraint = NumericConstraint | ChoiceConstraint


@dataclass(frozen=True)
class ConstraintReport:
    """Every constraint, checked against one candidate.

    `satisfied` requires every check to have *passed*, so an unknown never
    reads as a pass. `possible` is the weaker, honest question — "nothing here
    rules it out yet" — which is what a candidate needs to survive to the point
    where a datasheet is worth reading.
    """

    checks: tuple[Check, ...]

    @property
    def satisfied(self) -> bool:
        return all(check.status is CheckStatus.PASS for check in self.checks)

    @property
    def possible(self) -> bool:
        return not self.failures

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.FAIL)

    @property
    def unknown(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.UNKNOWN)

    def explain(self) -> list[str]:
        return [str(check) for check in self.checks]


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


class Objective(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    PREFER = "prefer"


@dataclass(frozen=True)
class Preference:
    """One term of the ranking function."""

    kind: Objective
    what: str
    value: str | None = None
    """What counts as preferred, for :attr:`Objective.PREFER`. Absent means the
    named thing is itself the preference — `existing_footprint_in_catalog`."""
    weight: float = 1.0

    def __str__(self) -> str:
        if self.kind is Objective.PREFER:
            subject = f"{self.what}={self.value}" if self.value is not None else self.what
        else:
            subject = self.what
        weight = "" if self.weight == 1.0 else f" (weight {self.weight:.4g})"
        return f"{self.kind} {subject}{weight}"


@dataclass(frozen=True)
class Ranked:
    """A candidate's place in the ordering, and why it is there."""

    index: int
    """Position in the sequence handed to :meth:`Requirement.rank`."""
    score: float
    contributions: tuple[tuple[str, float], ...]
    """`(preference, 0..1)` for each preference that could be evaluated."""
    unscored: tuple[str, ...]
    """Preferences this candidate said nothing about. They neither helped nor
    hurt it — a missing number is not a bad number — and are named so a reader
    can see the ranking was made on less than the whole requirement."""

    def explain(self) -> list[str]:
        lines = [f"{name}: {value:.2f}" for name, value in self.contributions]
        lines += [f"{name}: not scored — candidate did not state it" for name in self.unscored]
        return lines


# ---------------------------------------------------------------------------
# Sourcing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sourcing:
    """Where a candidate has to be buyable from to count."""

    suppliers: tuple[str, ...] = ()
    """Empty means every supplier klm is configured for."""
    min_stock: int | None = None
    lifecycle: tuple[Lifecycle, ...] = ()
    """Empty means no lifecycle filter — not "active only". A default that
    quietly excluded NRND parts would hide the substitute search's best
    answers."""


# ---------------------------------------------------------------------------
# The requirement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Requirement:
    """A part-shaped hole, stated precisely enough to search for."""

    kind: str
    constraints: tuple[AnyConstraint, ...] = ()
    preferences: tuple[Preference, ...] = ()
    sourcing: Sourcing = field(default_factory=Sourcing)
    notes: str | None = None
    """Freeform context for the model — the circuit, the board, the reason.
    Deliberately not a constraint: nothing mechanical is derived from it."""

    def constraint(self, name: str) -> AnyConstraint | None:
        key = normalise_name(name)
        return next((c for c in self.constraints if normalise_name(c.name) == key), None)

    def check(self, values: Mapping[str, str | None]) -> ConstraintReport:
        """Check a candidate's stated parameters against every constraint.

        Keys are matched the way parameter names are matched everywhere in klm
        — case, spaces and punctuation folded — and nothing cleverer. A fuzzy
        match that read "Output voltage" as "voltage" would check the wrong
        axis and report a pass.
        """
        stated = {normalise_name(name): text for name, text in values.items()}
        return ConstraintReport(
            tuple(constraint.check(stated.get(normalise_name(constraint.name)))
                  for constraint in self.constraints)
        )

    def supplier_constraints(self) -> dict[str, Constraint]:
        """The numeric constraints, in the form a parametric search consumes.

        Choice constraints are left out: they are names, and a supplier's
        parameter values for them are free text that klm has no reliable way to
        match. They are checked against candidates instead of narrowing the
        search, which costs a larger result set and no wrong exclusions.
        """
        return {c.name: c.constraint for c in self.constraints if isinstance(c, NumericConstraint)}

    def rank(self, candidates: Sequence[Mapping[str, Any]]) -> list[Ranked]:
        """Order candidates by the preferences, best first.

        Scores are **relative to the set**: `minimize unit_price` is scored by
        where a candidate sits between the cheapest and dearest *here*, because
        there is no absolute scale on which 0.42 USD is a good price. A single
        candidate therefore scores 1.0 on everything, which is correct and
        useless — ranking one thing is not a question.

        A preference a candidate says nothing about is dropped from that
        candidate's average rather than scored zero. Scoring it zero would
        punish a part for an unfetched field, which is a fact about klm's data
        rather than about the part.
        """
        ranges = {
            preference.what: _range_of(preference, candidates)
            for preference in self.preferences
            if preference.kind is not Objective.PREFER
        }

        ranked: list[Ranked] = []
        for index, candidate in enumerate(candidates):
            contributions: list[tuple[str, float]] = []
            unscored: list[str] = []
            weighted, total_weight = 0.0, 0.0
            for preference in self.preferences:
                score = _score(preference, candidate, ranges.get(preference.what))
                if score is None:
                    unscored.append(str(preference))
                    continue
                contributions.append((str(preference), score))
                weighted += score * preference.weight
                total_weight += preference.weight
            ranked.append(
                Ranked(
                    index=index,
                    score=weighted / total_weight if total_weight else 0.0,
                    contributions=tuple(contributions),
                    unscored=tuple(unscored),
                )
            )

        # Stable, so candidates that score identically keep the order they
        # arrived in rather than being reshuffled by a sort implementation.
        ranked.sort(key=lambda r: -r.score)
        return ranked

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The requirement as plain data, in the shape :func:`parse_requirement` reads."""
        data: dict[str, Any] = {"kind": self.kind}
        if self.notes:
            data["notes"] = self.notes
        if self.constraints:
            data["constraints"] = {c.name: _constraint_to_dict(c) for c in self.constraints}
        if self.preferences:
            data["preferences"] = [_preference_to_dict(p) for p in self.preferences]
        sourcing = _sourcing_to_dict(self.sourcing)
        if sourcing:
            data["sourcing"] = sourcing
        return data

    def to_toml(self) -> str:
        """A requirement file, byte-stable and in the order a human reads it."""
        data = self.to_dict()
        lines = [f"kind = {_toml(data['kind'])}"]
        if "notes" in data:
            lines.append(f"notes = {_toml(data['notes'])}")
        if "constraints" in data:
            lines += ["", "[constraints]"]
            lines += [f"{name} = {_toml(spec)}" for name, spec in data["constraints"].items()]
        for preference in data.get("preferences", []):
            lines += ["", "[[preferences]]"]
            lines += [f"{key} = {_toml(value)}" for key, value in preference.items()]
        if "sourcing" in data:
            lines += ["", "[sourcing]"]
            lines += [f"{key} = {_toml(value)}" for key, value in data["sourcing"].items()]
        return "\n".join(lines) + "\n"

    def to_prompt(self) -> str:
        """The requirement as the model is given it.

        Hard and soft are separated on the page as firmly as they are in the
        type, and the constraints are restated in the units a human wrote them
        in — the model states constraints back in those units too, and klm
        turns them into a supplier's identifiers (docs/11 §3).
        """
        lines = [f"Requirement: {self.kind}", ""]
        if self.notes:
            lines += ["Context:", self.notes, ""]
        lines.append("Hard constraints — a candidate that fails one does not qualify:")
        lines += (
            [f"  - {c.name}: {c.required}" for c in self.constraints]
            if self.constraints
            else ["  (none stated)"]
        )
        lines += ["", "Preferences — these rank what qualifies; they never disqualify:"]
        lines += (
            [f"  - {preference}" for preference in self.preferences]
            if self.preferences
            else ["  (none stated)"]
        )
        lines += ["", "Sourcing:"]
        suppliers = ", ".join(self.sourcing.suppliers) if self.sourcing.suppliers else "any"
        lines.append(f"  - suppliers: {suppliers}")
        if self.sourcing.min_stock is not None:
            lines.append(f"  - minimum stock: {self.sourcing.min_stock}")
        if self.sourcing.lifecycle:
            lines.append(f"  - lifecycle: {', '.join(self.sourcing.lifecycle)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ranking helpers
# ---------------------------------------------------------------------------


def _numeric(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return parse_value(raw).value
        except (ValueParseError, UnitError):
            return None
    return None


def _range_of(
    preference: Preference, candidates: Sequence[Mapping[str, Any]]
) -> tuple[float, float] | None:
    values = [v for v in (_numeric(c.get(preference.what)) for c in candidates) if v is not None]
    return (min(values), max(values)) if values else None


def _score(
    preference: Preference, candidate: Mapping[str, Any], span: tuple[float, float] | None
) -> float | None:
    raw = candidate.get(preference.what)
    if raw is None:
        return None

    if preference.kind is Objective.PREFER:
        if preference.value is None:
            return 1.0 if _truthy(raw) else 0.0
        return 1.0 if str(raw).strip().casefold() == preference.value.strip().casefold() else 0.0

    value = _numeric(raw)
    if value is None or span is None:
        return None
    low, high = span
    if high == low:
        # Every candidate agrees, so the preference separates nothing. Scoring
        # them all 1.0 keeps it from dragging every average toward zero.
        return 1.0
    fraction = (value - low) / (high - low)
    return 1.0 - fraction if preference.kind is Objective.MINIMIZE else fraction


def _truthy(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().casefold() in ("true", "yes", "1")
    return bool(raw)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_SECTIONS = ("kind", "notes", "constraints", "preferences", "sourcing")
_NUMERIC_KEYS = ("min", "max", "value", "unit", "tolerance")
#: A bare string is a text constraint unless it says otherwise. `"0402"` is a
#: package, not the number 402 — and telling them apart by whether the text
#: happens to parse as a number is exactly the guess this project refuses.
_NUMERIC_MARKERS = (">=", "<=", "=>", "=<", ">", "<", "=", "..", "…")


def load_requirement(path: Path) -> Requirement:
    """Read a requirement file (TOML)."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise RequirementError([f"{path}: {exc}"]) from exc
    return parse_requirement(data)


def parse_requirement(data: Mapping[str, Any]) -> Requirement:
    """Validate plain data into a :class:`Requirement`.

    Strict about unknown keys, on purpose. A misspelled `contraints` that was
    tolerated would produce a requirement with no hard constraints at all, and
    a search with no filters returns plenty of results — so the failure looks
    like success right up until a part is ordered.
    """
    problems: list[str] = []

    for key in data:
        if key not in _SECTIONS:
            problems.append(f"unknown section {key!r} (expected: {', '.join(_SECTIONS)})")

    kind = data.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        problems.append("'kind' is required — what sort of part this is, e.g. 'buck_converter'")
        kind = ""

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        problems.append("'notes' must be text")
        notes = None

    constraints = _parse_constraints(data.get("constraints"), problems)
    preferences = _parse_preferences(data.get("preferences"), problems)
    sourcing = _parse_sourcing(data.get("sourcing"), problems)

    if problems:
        raise RequirementError(problems)
    return Requirement(
        kind=kind.strip(),
        constraints=constraints,
        preferences=preferences,
        sourcing=sourcing,
        notes=notes,
    )


def _parse_constraints(raw: Any, problems: list[str]) -> tuple[AnyConstraint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        problems.append("'constraints' must be a table of name = requirement")
        return ()

    parsed: list[AnyConstraint] = []
    seen: dict[str, str] = {}
    for name, spec in raw.items():
        key = normalise_name(name)
        if key in seen:
            problems.append(f"constraint {name!r} repeats {seen[key]!r}")
            continue
        seen[key] = name
        constraint = _parse_one_constraint(name, spec, problems)
        if constraint is not None:
            parsed.append(constraint)
    return tuple(parsed)


def _parse_one_constraint(name: str, spec: Any, problems: list[str]) -> AnyConstraint | None:
    if isinstance(spec, str):
        if not any(marker in spec for marker in _NUMERIC_MARKERS):
            return ChoiceConstraint(name, (spec.strip(),))
        try:
            return NumericConstraint(name, parse_constraint(spec))
        except (ValueParseError, UnitError) as exc:
            problems.append(f"constraint {name!r}: {exc}")
            return None

    if isinstance(spec, int | float) and not isinstance(spec, bool):
        problems.append(
            f"constraint {name!r}: a bare number has no unit — "
            f"write {{ value = {spec}, unit = \"V\" }} or a text constraint"
        )
        return None

    if not isinstance(spec, Mapping):
        problems.append(f"constraint {name!r}: expected text or a table, got {type(spec).__name__}")
        return None

    if "prefer" in spec:
        problems.append(
            f"constraint {name!r}: 'prefer' is a preference, not a constraint — "
            "move it to [[preferences]] so a merely non-preferred part is ranked, not dropped"
        )
        return None

    if "allow" in spec:
        return _parse_choice(name, spec, problems)
    return _parse_numeric(name, spec, problems)


def _parse_choice(name: str, spec: Mapping[str, Any], problems: list[str]) -> AnyConstraint | None:
    unexpected = set(spec) - {"allow"}
    if unexpected:
        problems.append(f"constraint {name!r}: unexpected key(s) {', '.join(sorted(unexpected))}")
        return None
    allow = spec["allow"]
    if not isinstance(allow, list) or not allow or not all(isinstance(a, str) for a in allow):
        problems.append(f"constraint {name!r}: 'allow' must be a non-empty list of names")
        return None
    return ChoiceConstraint(name, tuple(str(a).strip() for a in allow))


def _parse_numeric(name: str, spec: Mapping[str, Any], problems: list[str]) -> AnyConstraint | None:
    unexpected = set(spec) - set(_NUMERIC_KEYS)
    if unexpected:
        problems.append(
            f"constraint {name!r}: unexpected key(s) {', '.join(sorted(unexpected))} "
            f"(expected: {', '.join(_NUMERIC_KEYS)}, or 'allow' for a list of names)"
        )
        return None

    unit = spec.get("unit")
    if unit is not None and not isinstance(unit, str):
        problems.append(f"constraint {name!r}: 'unit' must be text")
        return None

    numbers: dict[str, Quantity] = {}
    for key in ("min", "max", "value"):
        if key not in spec:
            continue
        quantity = _quantity(spec[key], unit)
        if quantity is None:
            problems.append(f"constraint {name!r}: cannot read {key} = {spec[key]!r}")
            return None
        numbers[key] = quantity

    tolerance = spec.get("tolerance")
    if tolerance is not None and (
        isinstance(tolerance, bool) or not isinstance(tolerance, int | float) or tolerance < 0
    ):
        problems.append(f"constraint {name!r}: 'tolerance' must be a fraction, e.g. 0.02 for ±2%")
        return None

    if "value" in numbers:
        if "min" in numbers or "max" in numbers:
            problems.append(f"constraint {name!r}: 'value' cannot be combined with min/max")
            return None
        constraint = Constraint(Comparison.EQUAL, numbers["value"])
    elif "min" in numbers and "max" in numbers:
        if numbers["min"].value > numbers["max"].value:
            problems.append(f"constraint {name!r}: min is above max")
            return None
        constraint = Constraint(Comparison.BETWEEN, numbers["min"], numbers["max"])
    elif "min" in numbers:
        constraint = Constraint(Comparison.AT_LEAST, numbers["min"])
    elif "max" in numbers:
        constraint = Constraint(Comparison.AT_MOST, numbers["max"])
    else:
        problems.append(f"constraint {name!r}: needs one of min, max or value")
        return None

    return NumericConstraint(name, constraint, float(tolerance) if tolerance is not None else None)


def _quantity(raw: Any, unit: str | None) -> Quantity | None:
    try:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int | float):
            return Quantity(float(raw), unit)
        if isinstance(raw, str):
            return parse_value(raw, unit=unit)
    except (ValueParseError, UnitError):
        return None
    return None


def _parse_preferences(raw: Any, problems: list[str]) -> tuple[Preference, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("'preferences' must be a list of tables")
        return ()

    parsed: list[Preference] = []
    for position, item in enumerate(raw, start=1):
        where = f"preference {position}"
        if not isinstance(item, Mapping):
            problems.append(f"{where}: expected a table")
            continue
        unexpected = set(item) - {"kind", "what", "value", "weight"}
        if unexpected:
            problems.append(f"{where}: unexpected key(s) {', '.join(sorted(unexpected))}")
            continue

        kind = item.get("kind")
        if kind not in tuple(Objective):
            problems.append(
                f"{where}: 'kind' must be one of {', '.join(o.value for o in Objective)}"
            )
            continue
        what = item.get("what")
        if not isinstance(what, str) or not what.strip():
            problems.append(f"{where}: 'what' is required — the field being preferred")
            continue
        value = item.get("value")
        if value is not None and not isinstance(value, str):
            problems.append(f"{where}: 'value' must be text")
            continue
        if Objective(kind) is not Objective.PREFER and value is not None:
            problems.append(f"{where}: 'value' only means something for kind = 'prefer'")
            continue
        weight = item.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, int | float) or weight <= 0:
            problems.append(f"{where}: 'weight' must be a positive number")
            continue
        parsed.append(Preference(Objective(kind), what.strip(), value, float(weight)))
    return tuple(parsed)


def _parse_sourcing(raw: Any, problems: list[str]) -> Sourcing:
    if raw is None:
        return Sourcing()
    if not isinstance(raw, Mapping):
        problems.append("'sourcing' must be a table")
        return Sourcing()

    unexpected = set(raw) - {"suppliers", "min_stock", "lifecycle"}
    if unexpected:
        problems.append(f"sourcing: unexpected key(s) {', '.join(sorted(unexpected))}")

    suppliers: tuple[str, ...] = ()
    if "suppliers" in raw:
        known = tuple(default_suppliers())
        listed = raw["suppliers"]
        if not isinstance(listed, list) or not all(isinstance(s, str) for s in listed):
            problems.append("sourcing: 'suppliers' must be a list of names")
        else:
            unknown = [s for s in listed if s.strip().lower() not in known]
            if unknown:
                # A typo here searches nowhere and returns nothing, which is
                # indistinguishable from a supplier that simply has no stock.
                problems.append(
                    f"sourcing: unknown supplier(s) {', '.join(unknown)} "
                    f"(klm knows: {', '.join(known)})"
                )
            suppliers = tuple(s.strip().lower() for s in listed)

    min_stock = raw.get("min_stock")
    if min_stock is not None and (
        isinstance(min_stock, bool) or not isinstance(min_stock, int) or min_stock < 0
    ):
        problems.append("sourcing: 'min_stock' must be a whole number of units")
        min_stock = None

    lifecycle: tuple[Lifecycle, ...] = ()
    if "lifecycle" in raw:
        listed = raw["lifecycle"]
        if not isinstance(listed, list) or not all(isinstance(s, str) for s in listed):
            problems.append("sourcing: 'lifecycle' must be a list")
        else:
            unknown = [s for s in listed if s.strip().lower() not in tuple(Lifecycle)]
            if unknown:
                problems.append(
                    f"sourcing: unknown lifecycle {', '.join(unknown)} "
                    f"(klm knows: {', '.join(item.value for item in Lifecycle)})"
                )
            lifecycle = tuple(
                Lifecycle(s.strip().lower())
                for s in listed
                if s.strip().lower() in tuple(Lifecycle)
            )

    return Sourcing(suppliers, min_stock, lifecycle)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _constraint_to_dict(constraint: AnyConstraint) -> Any:
    if isinstance(constraint, ChoiceConstraint):
        return {"allow": list(constraint.allow)}

    spec: dict[str, Any] = {}
    inner = constraint.constraint
    if inner.comparison is Comparison.EQUAL:
        spec["value"] = inner.low.value
    elif inner.comparison is Comparison.AT_LEAST:
        spec["min"] = inner.low.value
    elif inner.comparison is Comparison.AT_MOST:
        spec["max"] = inner.low.value
    else:
        assert inner.high is not None
        spec["min"] = inner.low.value
        spec["max"] = inner.high.value
    if inner.low.unit is not None:
        spec["unit"] = inner.low.unit
    if constraint.tolerance is not None:
        spec["tolerance"] = constraint.tolerance
    return spec


def _preference_to_dict(preference: Preference) -> dict[str, Any]:
    spec: dict[str, Any] = {"kind": preference.kind.value, "what": preference.what}
    if preference.value is not None:
        spec["value"] = preference.value
    if preference.weight != 1.0:
        spec["weight"] = preference.weight
    return spec


def _sourcing_to_dict(sourcing: Sourcing) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if sourcing.suppliers:
        spec["suppliers"] = list(sourcing.suppliers)
    if sourcing.min_stock is not None:
        spec["min_stock"] = sourcing.min_stock
    if sourcing.lifecycle:
        spec["lifecycle"] = [item.value for item in sourcing.lifecycle]
    return spec


def _toml(value: Any) -> str:
    """Just enough TOML to write back what this module reads."""
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{ " + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")  # pragma: no cover
