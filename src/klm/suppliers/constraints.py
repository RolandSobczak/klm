"""Turning a human constraint into a supplier's parametric filter.

TME's parametric search filters by **numeric identifiers**: *parameter 2 has
value 156 or 179*. It cannot express "Vin_max ≥ 18 V" — values are discrete IDs,
so a numeric constraint is not a comparison the API performs but a *set of value
IDs whose parsed number satisfies it* (docs/14 Q10).

That translation lives here, and it is the reason the research agent never sees
an identifier. Asked for `parameters[0][id]=2`, a model would eventually supply a
plausible wrong one — and a wrong parameter ID does not error, it returns
confidently wrong parts. Same failure as an invented MPN, same answer: make it
structurally impossible rather than ask a prompt to prevent it.

Two rules the rest of klm will recognise:

* **A constraint that cannot be mapped is reported, never dropped.** Silently
  discarding it would widen the search and then present the results as though
  they had been filtered.
* **A value that does not parse is skipped, and said so.** Supplier parameter
  lists contain free text — "see datasheet", "-" — beside the numbers, and
  guessing at those is how a filter quietly excludes the right part.

Nothing here talks to a network. It takes the parameter list a supplier already
returned and decides what to ask for next, which is what makes it testable
without an API key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from klm.units import Quantity, UnitError, ValueParseError, parse_value

__all__ = [
    "Comparison",
    "Constraint",
    "FilterGroup",
    "ParameterValue",
    "Resolution",
    "SupplierParameter",
    "normalise_name",
    "parse_constraint",
    "resolve",
]


class Comparison(StrEnum):
    AT_LEAST = ">="
    AT_MOST = "<="
    EQUAL = "=="
    BETWEEN = ".."


#: `>=18V`, `<= 6.5 V`, `3.3V`, `4.5..18V`, `-40..85C`. The unit is whatever
#: `klm.units` accepts, which is the same parser the field schema uses — so a
#: constraint and a stored parameter cannot disagree about what `4k7` means.
_OPERATOR = re.compile(r"^\s*(>=|<=|=>|=<|>|<|=)?\s*(.+?)\s*$")
_RANGE = re.compile(r"^\s*(.+?)\s*(?:\.\.|…|\bto\b)\s*(.+?)\s*$")


@dataclass(frozen=True)
class Constraint:
    """One requirement, in the units a human would state it in."""

    comparison: Comparison
    low: Quantity
    high: Quantity | None = None
    """Only set for :attr:`Comparison.BETWEEN`."""

    def satisfied_by(self, value: Quantity, *, rel_tol: float = 1e-9) -> bool:
        """Whether a candidate value meets this constraint.

        A unit mismatch is `False` rather than an error: a category's parameter
        list legitimately mixes units (a regulator has volts, amps and degrees),
        and comparing across them is a question with no answer, not a fault.
        """
        if self.low.unit is not None and value.unit is not None and self.low.unit != value.unit:
            return False
        if self.comparison is Comparison.AT_LEAST:
            return value.value >= self.low.value or value.matches(self.low, rel_tol=rel_tol)
        if self.comparison is Comparison.AT_MOST:
            return value.value <= self.low.value or value.matches(self.low, rel_tol=rel_tol)
        if self.comparison is Comparison.EQUAL:
            return value.matches(self.low, rel_tol=rel_tol)
        assert self.high is not None
        return (
            (value.value >= self.low.value or value.matches(self.low, rel_tol=rel_tol))
            and (value.value <= self.high.value or value.matches(self.high, rel_tol=rel_tol))
        )

    def __str__(self) -> str:
        if self.comparison is Comparison.BETWEEN:
            return f"{self.low}..{self.high}"
        return f"{self.comparison}{self.low}"


def parse_constraint(text: str, *, unit: str | None = None) -> Constraint:
    """Read `>=18V`, `3.3V`, `4.5..18V`.

    A bare value means equality rather than a minimum. "100nF" is a request for
    a 100 nF capacitor, and reading it as "at least 100 nF" would return a
    parts list starting at the right answer and continuing past it.
    """
    ranged = _RANGE.match(text)
    if ranged is not None:
        low = parse_value(ranged.group(1), unit=unit)
        high = parse_value(ranged.group(2), unit=unit or low.unit)
        if low.unit is None and high.unit is not None:
            low = low.with_unit(high.unit)
        if low.value > high.value:
            raise ValueParseError(f"range {text!r} runs backwards")
        return Constraint(Comparison.BETWEEN, low, high)

    match = _OPERATOR.match(text)
    if match is None or not match.group(2):  # pragma: no cover - regex always matches
        raise ValueParseError(f"cannot read {text!r} as a constraint")
    operator, rest = match.group(1), match.group(2)
    comparison = {
        ">=": Comparison.AT_LEAST, "=>": Comparison.AT_LEAST, ">": Comparison.AT_LEAST,
        "<=": Comparison.AT_MOST, "=<": Comparison.AT_MOST, "<": Comparison.AT_MOST,
        "=": Comparison.EQUAL, None: Comparison.EQUAL,
    }[operator]
    return Constraint(comparison, parse_value(rest, unit=unit))


@dataclass(frozen=True)
class ParameterValue:
    """One selectable value of a supplier parameter, with its identifier."""

    value_id: str
    text: str
    """As the supplier spells it — `"18 V"`, `"see datasheet"`."""

    def quantity(self, *, unit: str | None = None) -> Quantity | None:
        """The value as a number, or ``None`` if it is not one."""
        try:
            return parse_value(self.text, unit=unit)
        except (ValueParseError, UnitError):
            return None


@dataclass(frozen=True)
class SupplierParameter:
    """A filterable parameter of a category, as the supplier reports it."""

    parameter_id: str
    name: str
    values: tuple[ParameterValue, ...] = ()

    def matching(self, constraint: Constraint) -> tuple[list[str], list[str]]:
        """Value IDs satisfying ``constraint``, and the texts that did not parse."""
        chosen: list[str] = []
        unparsed: list[str] = []
        for value in self.values:
            quantity = value.quantity(unit=constraint.low.unit)
            if quantity is None:
                unparsed.append(value.text)
            elif constraint.satisfied_by(quantity):
                chosen.append(value.value_id)
        return chosen, unparsed


@dataclass(frozen=True)
class FilterGroup:
    """What one constraint became: a parameter and the values that satisfy it."""

    parameter_id: str
    name: str
    value_ids: tuple[str, ...]


@dataclass
class Resolution:
    """The outcome of mapping a whole requirement onto one category.

    `unmapped` and `narrowed` exist so a caller can say what happened. A search
    that quietly ignored two of five constraints and returned forty parts looks
    exactly like a search that applied them and found forty.
    """

    groups: list[FilterGroup] = field(default_factory=list)
    unmapped: list[tuple[str, str]] = field(default_factory=list)
    """``(constraint name, why)`` — no such parameter, or nothing satisfied it."""
    unparsed: list[tuple[str, str]] = field(default_factory=list)
    """``(parameter name, value text)`` the supplier offered and klm could not read."""

    @property
    def complete(self) -> bool:
        return not self.unmapped

    def explain(self) -> list[str]:
        """Lines for a human, or for the agent to reason about."""
        lines = [f"{g.name}: {len(g.value_ids)} matching value(s)" for g in self.groups]
        lines += [f"{name}: not applied — {why}" for name, why in self.unmapped]
        if self.unparsed:
            unreadable = ", ".join(f"{p}={v!r}" for p, v in self.unparsed[:5])
            lines.append(f"values klm could not read (ignored): {unreadable}")
        return lines


def normalise_name(name: str) -> str:
    """The key two spellings of the same parameter share.

    Public because a requirement's constraint and a supplier's parameter have
    to agree on what "the same name" means. Two normalisers would agree until
    one of them learned about a hyphen.
    """
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def resolve(
    parameters: list[SupplierParameter], constraints: Mapping[str, str | Constraint]
) -> Resolution:
    """Map ``{"Vin max": ">=18V"}`` onto the supplier's parameter identifiers.

    Matching a constraint to a parameter is by normalised name — case, spaces
    and punctuation removed — and nothing cleverer. A fuzzy match that picked
    "Output voltage" for "voltage" would filter on the wrong axis and report
    success, which is worse than saying it could not find the parameter.

    A value may be the text a human wrote or an already-parsed
    :class:`Constraint`, which is what a structured requirement holds — so a
    requirement reaches a supplier search without a round trip through text.
    """
    by_name = {normalise_name(p.name): p for p in parameters}
    result = Resolution()

    for name, text in constraints.items():
        parameter = by_name.get(normalise_name(name))
        if parameter is None:
            available = ", ".join(sorted(p.name for p in parameters)[:8]) or "none"
            result.unmapped.append((name, f"no such parameter in this category (has: {available})"))
            continue

        if isinstance(text, Constraint):
            constraint = text
        else:
            try:
                constraint = parse_constraint(text)
            except (ValueParseError, UnitError) as exc:
                result.unmapped.append((name, f"cannot read {text!r}: {exc}"))
                continue

        value_ids, unparsed = parameter.matching(constraint)
        result.unparsed.extend((parameter.name, text) for text in unparsed)
        if not value_ids:
            result.unmapped.append(
                (name, f"no value in {parameter.name!r} satisfies {constraint}")
            )
            continue
        result.groups.append(
            FilterGroup(
                parameter_id=parameter.parameter_id,
                name=parameter.name,
                value_ids=tuple(value_ids),
            )
        )

    return result
