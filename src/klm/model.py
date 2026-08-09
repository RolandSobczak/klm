"""Domain types.

Plain dataclasses with no persistence knowledge. The storage layer maps them to
rows; the serialisation layer maps them to YAML. Neither concern belongs here.

See docs/02-domain-model.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Confidence",
    "Lifecycle",
    "Parameter",
    "Part",
    "PartStatus",
    "SourceKind",
]


class PartStatus(StrEnum):
    DRAFT = "draft"
    """Proposed or imported. Not usable in a design, not exported to libraries."""
    APPROVED = "approved"
    DEPRECATED = "deprecated"
    """Soft-deleted. Hard deletion would orphan existing schematics."""


class Lifecycle(StrEnum):
    ACTIVE = "active"
    NRND = "nrnd"
    OBSOLETE = "obsolete"
    UNKNOWN = "unknown"


class SourceKind(StrEnum):
    """Where a parameter came from, in descending order of trust."""

    USER = "user"
    DATASHEET = "datasheet"
    SUPPLIER = "supplier"
    INFERRED = "inferred"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(slots=True)
class Parameter:
    """One measured or stated property of a part, with its provenance.

    A parameter is never a bare value. When a datasheet and a supplier disagree,
    both are stored — the conflict is surfaced for a human, never silently
    resolved (docs/02 §4). That is why ``source`` is part of the identity.
    """

    name: str
    source: SourceKind
    value_num: float | None = None
    """Numeric value in SI base units."""
    value_text: str | None = None
    """Used when the value is not numeric, e.g. a dielectric code."""
    unit: str | None = None
    tolerance: float | None = None
    source_ref: str | None = None
    """URL, datasheet page, or quoted snippet backing the value."""
    confidence: Confidence = Confidence.MEDIUM

    @property
    def key(self) -> tuple[str, str]:
        """Identity within a part: same name from two sources is two parameters."""
        return (self.name, str(self.source))

    @property
    def value(self) -> float | str | None:
        return self.value_num if self.value_num is not None else self.value_text


@dataclass(slots=True)
class Part:
    """The design-time object: one row per electrically distinct component."""

    klm_id: str
    mpn: str
    manufacturer: str
    description: str = ""
    category: str | None = None
    """Taxonomy path, e.g. ``IC/Power/Regulator/Switching``."""
    package: str | None = None
    lifecycle: Lifecycle = Lifecycle.UNKNOWN
    status: PartStatus = PartStatus.DRAFT
    datasheet_url: str | None = None
    datasheet_sha: str | None = None
    symbol_hash: str | None = None
    footprint_hash: str | None = None
    model3d_hash: str | None = None
    notes: str | None = None
    parameters: list[Parameter] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None

    def sorted_parameters(self) -> list[Parameter]:
        """Parameters in a stable order, so exports are byte-reproducible."""
        return sorted(self.parameters, key=lambda p: p.key)

    def parameter(self, name: str, source: SourceKind | None = None) -> Parameter | None:
        """Look up a parameter, preferring the most trustworthy source."""
        matches = [p for p in self.parameters if p.name == name]
        if source is not None:
            matches = [p for p in matches if p.source == source]
        if not matches:
            return None
        order = list(SourceKind)
        return min(matches, key=lambda p: order.index(p.source))

    def conflicts(self) -> list[tuple[str, list[Parameter]]]:
        """Parameter names carrying disagreeing values from different sources."""
        grouped: dict[str, list[Parameter]] = {}
        for parameter in self.parameters:
            grouped.setdefault(parameter.name, []).append(parameter)
        return [
            (name, sorted(group, key=lambda p: str(p.source)))
            for name, group in sorted(grouped.items())
            if len({p.value for p in group}) > 1
        ]
