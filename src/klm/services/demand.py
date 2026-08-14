"""Build plan → gross demand → net demand → order quantities.

Solves the first half of [P4](../../docs/01-vision-and-problems.md): ordering as
a spreadsheet exercise. Each stage is kept separate and inspectable, because
`klm order plan --explain` has to be able to justify every number — a quantity a
user cannot check is a quantity they will not spend money against (docs/10 §1).

Two judgements are encoded here rather than left to the user.

**Buying exactly what the build needs is wrong for hobby work.** One dropped
0402 stalls a build for two weeks. The spares policy is per-category and
configurable, with an `applies_above` price threshold so expensive parts never
attract automatic spares regardless of category.

**Stock is advisory, not authoritative.** It drifts the moment a part is taken
out without being recorded, so an old count contributes less confidence to "we
already have these" than a fresh one.
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from klm.kicad.project import KiCadProject, find_project
from klm.model import Offer, Part
from klm.services.bom import Variant, extract_bom, load_variants
from klm.services.catalog import get_part
from klm.services.offers import list_offers

__all__ = [
    "BuildItem",
    "DemandLine",
    "DemandPlan",
    "SparesPolicy",
    "SparesRule",
    "parse_build_plan",
    "plan_demand",
]

#: `5x sensor-board:full` or `2 * psu-board`. The multiplication sign is also
#: accepted, because it is what the documentation uses and what a user copies.
_BUILD = re.compile(
    "^\\s*(\\d+)\\s*[x\u00d7*]\\s*([^:]+?)\\s*(?::\\s*(.+?)\\s*)?$", re.IGNORECASE
)


@dataclass(frozen=True)
class BuildItem:
    """"Five of this board, in this variant."""

    project: str
    quantity: int
    variant: str = ""

    def __str__(self) -> str:
        suffix = f":{self.variant}" if self.variant else ""
        return f"{self.quantity}x {self.project}{suffix}"


def parse_build_plan(text: str) -> list[BuildItem]:
    """Read ``5x sensor-board:full,2x psu-board:basic``.

    Raises on anything it cannot read rather than dropping the item — a build
    plan silently missing a board produces an order silently missing its parts.
    """
    items: list[BuildItem] = []
    for chunk in text.split(","):
        if not chunk.strip():
            continue
        match = _BUILD.match(chunk)
        if match is None:
            raise ValueError(f"cannot read {chunk.strip()!r} as QTYxPROJECT[:VARIANT]")
        count, project, variant = match.groups()
        items.append(BuildItem(project=project, quantity=int(count), variant=variant or ""))
    if not items:
        raise ValueError("the build plan is empty")
    return items


# ---------------------------------------------------------------------------
# Spares
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SparesRule:
    extra_pct: float = 0.0
    min_extra: int = 0
    applies_above: float | None = None
    """Unit price above which this rule stops adding spares.

    Pays for itself the first time it prevents ordering two spare 18-euro MCUs.
    """

    def spares(self, needed: int, unit_price: float | None) -> int:
        above = self.applies_above is not None and unit_price is not None
        if above and unit_price > self.applies_above:  # type: ignore[operator]
            return 0
        return max(int(needed * self.extra_pct / 100.0), self.min_extra)


@dataclass(frozen=True)
class SparesPolicy:
    """Category glob → rule, plus a default. Longest matching glob wins."""

    default: SparesRule = SparesRule(extra_pct=10.0, min_extra=2)
    rules: dict[str, SparesRule] = field(default_factory=dict)
    price_ceiling: SparesRule | None = None
    """A rule applied to every part above its own ``applies_above``."""

    def rule_for(self, category: str | None, unit_price: float | None) -> tuple[str, SparesRule]:
        ceiling = self.price_ceiling
        limit = ceiling.applies_above if ceiling is not None else None
        if limit is not None and unit_price is not None and unit_price > limit:
            return ("expensive", ceiling)  # type: ignore[return-value]
        path = (category or "").lower()
        best: tuple[str, SparesRule] | None = None
        for pattern, rule in self.rules.items():
            matched = fnmatch.fnmatch(path, pattern.lower())
            if matched and (best is None or len(pattern) > len(best[0])):
                best = (pattern, rule)
        return best if best is not None else ("default", self.default)

    @classmethod
    def from_config(cls, raw: dict[str, object]) -> SparesPolicy:
        """Read the ``[spares]`` table (docs/10 §2)."""
        default = SparesRule(extra_pct=10.0, min_extra=2)
        rules: dict[str, SparesRule] = {}
        ceiling: SparesRule | None = None
        for key, body in raw.items():
            if not isinstance(body, dict):
                continue
            rule = SparesRule(
                extra_pct=float(body.get("extra_pct", 0) or 0),
                min_extra=int(body.get("min_extra", 0) or 0),
                applies_above=(
                    float(body["applies_above"]) if body.get("applies_above") is not None else None
                ),
            )
            if key == "default":
                default = rule
            elif key == "expensive":
                ceiling = rule
            else:
                rules[key] = rule
        return cls(default=default, rules=rules, price_ceiling=ceiling)


# ---------------------------------------------------------------------------
# Demand
# ---------------------------------------------------------------------------

#: A count older than this contributes less confidence to "we already have
#: these". Not a hard cutoff — a discount, because an old count is evidence,
#: just weaker evidence than a fresh one (docs/10 §6).
STALE_COUNT_DAYS = 365
STALE_COUNT_TRUST = 0.5


@dataclass
class DemandLine:
    klm_id: str
    part: Part | None
    gross: int = 0
    """What the builds need."""
    on_hand: int = 0
    counted_at: str | None = None
    trusted_stock: int = 0
    """On-hand discounted for staleness — what klm is willing to subtract."""
    net: int = 0
    spares: int = 0
    spares_rule: str = "default"
    target: int = 0
    order_qty: int = 0
    """Target rounded up to the chosen offer's MOQ and multiple."""
    offers: list[Offer] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    """Which builds contributed, for `--explain`."""
    alternates: list[Part] = field(default_factory=list)
    """Approved substitutes worth buying instead, when this part cannot be.

    Reported, never swapped in. klm knows the substitution was approved; it does
    not know it was approved for *this* build, and quietly ordering a different
    part than the BOM names is how the wrong component reaches a board.
    """

    @property
    def mpn(self) -> str:
        return self.part.mpn if self.part else self.klm_id

    def explain(self) -> list[str]:
        lines = [f"{self.gross} needed for {', '.join(self.sources)}"]
        if self.on_hand:
            stale = self.trusted_stock != self.on_hand
            note = f" (counted {self.counted_at}, discounted for age)" if stale else ""
            lines.append(f"- {self.trusted_stock} on hand{note}")
        if self.spares:
            lines.append(f"+ {self.spares} spares [{self.spares_rule}]")
        if self.order_qty != self.target:
            lines.append(f"→ {self.order_qty} after MOQ and order multiple")
        for part in self.alternates:
            lines.append(f"approved substitute {part.mpn} is orderable ({part.klm_id})")
        return lines


@dataclass
class DemandPlan:
    builds: list[BuildItem] = field(default_factory=list)
    lines: list[DemandLine] = field(default_factory=list)
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    """Project → references with no catalog part. Reported, never ordered blind."""

    @property
    def orderable(self) -> list[DemandLine]:
        return [line for line in self.lines if line.order_qty > 0]


def plan_demand(
    conn: sqlite3.Connection,
    builds: list[BuildItem],
    *,
    projects_root: dict[str, KiCadProject] | str | None = None,
    policy: SparesPolicy | None = None,
    use_stock: bool = True,
    now: str | None = None,
) -> DemandPlan:
    """Expand a build plan into quantities to order.

    ``projects_root`` is where project directories are looked up; each build
    item names one. Passing an explicit mapping of name → :class:`KiCadProject`
    is also accepted, which is what the tests and the desktop app use.
    """
    spares_policy = policy or SparesPolicy()
    plan = DemandPlan(builds=list(builds))
    gross: dict[str, int] = {}
    sources: dict[str, list[str]] = {}

    for item in builds:
        project = _locate(item.project, projects_root)
        variant = _variant_for(project, item.variant)
        bom = extract_bom(conn, project, variant=variant)
        if bom.unresolved:
            plan.unresolved[item.project] = bom.unresolved
        for line in bom.lines:
            if not line.klm_id:
                continue
            gross[line.klm_id] = gross.get(line.klm_id, 0) + line.quantity * item.quantity
            sources.setdefault(line.klm_id, []).append(str(item))

    stamp = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for klm_id, quantity in sorted(gross.items()):
        plan.lines.append(
            _line(conn, klm_id, quantity, sources[klm_id], spares_policy, use_stock, stamp)
        )
    return plan


def _line(
    conn: sqlite3.Connection,
    klm_id: str,
    gross: int,
    sources: list[str],
    policy: SparesPolicy,
    use_stock: bool,
    now: str,
) -> DemandLine:
    part = get_part(conn, klm_id)
    offers = list_offers(conn, klm_id=klm_id)
    line = DemandLine(klm_id=klm_id, part=part, gross=gross, sources=sorted(set(sources)))

    if use_stock:
        line.on_hand, line.counted_at = _stock_for(conn, klm_id)
        line.trusted_stock = _trusted(line.on_hand, line.counted_at, now)
    line.net = max(0, gross - line.trusted_stock)

    unit = min((o.unit_price(max(line.net, 1)) or 0.0) for o in offers) if offers else None
    name, rule = policy.rule_for(part.category if part else None, unit)
    line.spares_rule = name
    line.spares = rule.spares(line.net, unit) if line.net else 0
    line.target = line.net + line.spares

    line.offers = offers
    line.order_qty = min((o.order_qty(line.target) for o in offers), default=line.target)
    if line.target == 0:
        line.order_qty = 0
    if line.order_qty and _unsourceable(offers):
        line.alternates = _alternates(conn, klm_id)
    return line


def _unsourceable(offers: list[Offer]) -> bool:
    """No offer, or every offer says it has none.

    Unknown stock is not zero (``Offer.in_stock``'s rule), so an offer that
    never stated a figure keeps the part sourceable — reporting a substitute
    for a part that is probably in stock trains people to ignore the report.
    """
    return not offers or all(o.stock is not None and o.stock <= 0 for o in offers)


def _alternates(conn: sqlite3.Connection, klm_id: str) -> list[Part]:
    from klm.services.substitutes import list_substitutions

    found = []
    for substitution in list_substitutions(conn, klm_id):
        part = get_part(conn, substitution.substitute_id)
        if part is None or _unsourceable(list_offers(conn, klm_id=part.klm_id)):
            continue
        found.append(part)
    return found


def _stock_for(conn: sqlite3.Connection, klm_id: str) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT SUM(quantity) AS total, MIN(last_counted) AS oldest "
        "FROM stock_item WHERE klm_id = ?",
        (klm_id,),
    ).fetchone()
    if row is None or row["total"] is None:
        return 0, None
    return int(row["total"]), row["oldest"]


def _trusted(on_hand: int, counted_at: str | None, now: str) -> int:
    """Discount an old count rather than ignoring or believing it outright."""
    if on_hand <= 0:
        return 0
    if counted_at is None:
        return int(on_hand * STALE_COUNT_TRUST)
    try:
        counted = datetime.strptime(counted_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        current = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return int(on_hand * STALE_COUNT_TRUST)
    age = (current - counted).days
    return on_hand if age <= STALE_COUNT_DAYS else int(on_hand * STALE_COUNT_TRUST)


def _locate(name: str, projects_root: dict[str, KiCadProject] | str | None) -> KiCadProject:
    if isinstance(projects_root, dict):
        project = projects_root.get(name)
        if project is None:
            raise ValueError(f"no project named {name!r} in the build plan's project set")
        return project
    from pathlib import Path

    root = Path(projects_root) if projects_root is not None else Path.cwd()
    candidate = root / name
    if not candidate.is_dir():
        raise ValueError(f"no project directory at {candidate}")
    return find_project(candidate)


def _variant_for(project: KiCadProject, name: str) -> Variant | None:
    if not name:
        return None
    import tomllib

    path = project.root / "klm.toml"
    if not path.is_file():
        raise ValueError(f"{project.name} has no klm.toml, so it defines no variant {name!r}")
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    variants = load_variants(dict(raw.get("variants") or {}))
    if name not in variants:
        known = ", ".join(sorted(variants)) or "none"
        raise ValueError(f"{project.name} has no variant {name!r} (known: {known})")
    return variants[name]
