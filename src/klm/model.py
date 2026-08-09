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
    "Offer",
    "Packaging",
    "Parameter",
    "Part",
    "PartStatus",
    "PriceBreak",
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


class Packaging(StrEnum):
    """How the supplier ships the part.

    Not cosmetic: JLCPCB assembly and a hand-soldered prototype want different
    packaging of the same die, and the price usually differs between them.
    """

    CUT_TAPE = "cut tape"
    REEL = "reel"
    TRAY = "tray"
    TUBE = "tube"
    BAG = "bag"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PriceBreak:
    """One row of a supplier's quantity/price ladder."""

    qty: int
    unit_price: float

    def __post_init__(self) -> None:
        if self.qty < 1:
            raise ValueError(f"price break quantity must be positive, got {self.qty}")
        if self.unit_price < 0:
            raise ValueError(f"price break price cannot be negative, got {self.unit_price}")


@dataclass(slots=True)
class Offer:
    """A specific orderable item at a specific supplier (docs/02 §1).

    Offers are **derived data**: a cache of a remote fact, always stamped with
    when it was fetched. They are never hand-edited as truth and losing them
    costs one API refresh — which is why ``fetched_at`` is not optional in
    spirit even where the type allows it.

    ``klm_id`` is ``None`` on an offer an adapter has just returned and not yet
    matched to a part. Persisting one in that state is an error, not a default.
    """

    supplier: str
    supplier_pn: str
    klm_id: str | None = None
    mpn: str | None = None
    """The supplier's own idea of the MPN, kept for auditing the match."""
    manufacturer: str | None = None
    description: str = ""
    packaging: Packaging = Packaging.UNKNOWN
    moq: int | None = None
    multiple: int | None = None
    stock: int | None = None
    currency: str | None = None
    price_breaks: list[PriceBreak] = field(default_factory=list)
    lead_time_days: int | None = None
    url: str | None = None
    datasheet_url: str | None = None
    match_confidence: Confidence = Confidence.HIGH
    """How sure klm is that this offer is the same part, not merely a similar one."""
    fetched_at: str | None = None

    @property
    def in_stock(self) -> bool:
        """Unknown stock is not the same as zero, and must not read as it."""
        return self.stock is not None and self.stock > 0

    def sorted_breaks(self) -> list[PriceBreak]:
        return sorted(self.price_breaks, key=lambda b: b.qty)

    def unit_price(self, qty: int = 1) -> float | None:
        """Price per unit at ``qty``: the last break whose quantity it reaches.

        Below the smallest break there is no price to quote — the supplier has
        not offered one — so this returns ``None`` rather than extrapolating.
        """
        applicable = [b for b in self.sorted_breaks() if b.qty <= qty]
        return applicable[-1].unit_price if applicable else None

    def order_qty(self, needed: int) -> int:
        """``needed`` rounded up to the supplier's MOQ and order multiple."""
        qty = max(needed, self.moq or 1)
        multiple = self.multiple or 1
        if multiple > 1:
            qty = -(-qty // multiple) * multiple
        return qty
