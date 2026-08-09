"""Extracting a bill of materials from a project's schematics.

Read directly from the ``.kicad_sch`` files rather than through
``kicad-cli sch export bom``, for one reason that matters: a BOM must be
obtainable on a machine with no KiCad installed. CI checks it, the clean-room
verifier needs it, and a cost estimate should not depend on whether the person
asking has KiCad on their PATH. Everything else in the fab pipeline does shell
out, because gerbers genuinely require KiCad's own plotting code — a BOM does
not.

Three details are easy to get wrong and expensive to notice late:

* **Hierarchy multiplies.** A sheet used twice places one symbol node and two
  parts on the board. The count comes from each symbol's ``instances`` block,
  not from counting nodes.
* **DNP is KiCad's own flag** (7 and later), and it is honoured rather than
  inferred. A part excluded from the build still appears in the report, under
  its own heading, because a part that silently disappears from a BOM is how a
  board arrives unpopulated.
* **Power symbols are not components.** KiCad marks them with a reference
  beginning with ``#``; they carry no line.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from klm.kicad import schematic as sch
from klm.kicad.project import KiCadProject
from klm.kicad.sexpr import loads
from klm.model import Part
from klm.services.catalog import get_part

__all__ = [
    "BomLine",
    "BomReport",
    "Variant",
    "extract_bom",
    "reference_sort_key",
]

#: `R10` sorts after `R9`, which a plain string sort gets wrong — and a BOM
#: whose designators read R1, R10, R2 looks broken to whoever checks it.
_REFERENCE = re.compile(r"^([^\d]*)(\d*)(.*)$")


def reference_sort_key(reference: str) -> tuple[str, int, str]:
    match = _REFERENCE.match(reference)
    if match is None:  # pragma: no cover - the regex matches everything
        return (reference, 0, "")
    prefix, digits, rest = match.groups()
    return (prefix, int(digits) if digits else 0, rest)


@dataclass(frozen=True)
class Variant:
    """A build configuration: what is left off, and what is changed."""

    name: str
    dnp: frozenset[str] = frozenset()
    """References not populated in this variant, on top of the schematic's own."""
    overrides: dict[str, str] = field(default_factory=dict)
    """Reference → replacement ``Value``."""

    def excludes(self, reference: str) -> bool:
        return reference in self.dnp


@dataclass
class BomLine:
    """One orderable row: a group of references sharing a part."""

    value: str
    footprint: str
    references: list[str] = field(default_factory=list)
    klm_id: str | None = None
    mpn: str = ""
    manufacturer: str = ""
    lcsc: str = ""
    dnp: bool = False
    part: Part | None = None

    @property
    def quantity(self) -> int:
        return len(self.references)

    @property
    def designators(self) -> str:
        return ",".join(sorted(self.references, key=reference_sort_key))

    @property
    def footprint_name(self) -> str:
        """The footprint without its library nickname, as a fab expects it."""
        return self.footprint.partition(":")[2] or self.footprint

    def sort_key(self) -> tuple[str, int, str]:
        first = min(self.references, key=reference_sort_key, default="")
        return reference_sort_key(first)


@dataclass
class BomReport:
    project: KiCadProject
    variant: str = ""
    lines: list[BomLine] = field(default_factory=list)
    excluded: list[BomLine] = field(default_factory=list)
    """DNP and variant-depopulated groups. Reported, never silently dropped."""
    unresolved: list[str] = field(default_factory=list)
    """References klm could not tie to a catalog part."""

    @property
    def total_parts(self) -> int:
        return sum(line.quantity for line in self.lines)

    def assembly_lines(self) -> list[BomLine]:
        """Lines an assembly house would place: populated and surface-mount."""
        return [line for line in self.lines if not line.dnp]


def extract_bom(
    conn: sqlite3.Connection | None,
    project: KiCadProject,
    *,
    variant: Variant | None = None,
) -> BomReport:
    """Group every placed symbol into orderable lines.

    ``conn`` may be ``None``: without a catalog the BOM still comes out, just
    without MPNs. That is what makes it usable from the clean-room check, which
    has no catalog by definition.
    """
    report = BomReport(project=project, variant=variant.name if variant else "")
    groups: dict[tuple[str, str, str], BomLine] = {}
    excluded: dict[tuple[str, str, str], BomLine] = {}

    for sheet in project.schematics:
        with open(sheet, encoding="utf-8", newline="") as handle:
            document = loads(handle.read())
        for instance in sch.iter_symbol_instances(document, sheet=sheet.name):
            if instance.is_power or not instance.in_bom:
                continue
            _place(conn, report, groups, excluded, instance, variant)

    report.lines = sorted(groups.values(), key=lambda line: line.sort_key())
    report.excluded = sorted(excluded.values(), key=lambda line: line.sort_key())
    report.unresolved = sorted(
        {
            reference
            for line in (*report.lines, *report.excluded)
            if line.klm_id is None
            for reference in line.references
        },
        key=reference_sort_key,
    )
    return report


def _place(
    conn: sqlite3.Connection | None,
    report: BomReport,
    groups: dict[tuple[str, str, str], BomLine],
    excluded: dict[tuple[str, str, str], BomLine],
    instance: sch.SymbolInstance,
    variant: Variant | None,
) -> None:
    part = _resolve(conn, instance)
    for reference in instance.bom_references():
        value = (variant.overrides.get(reference) if variant else None) or instance.value
        dnp = instance.dnp or bool(variant and variant.excludes(reference))
        # Grouping is by what would be ordered, so an override splits a group.
        key = (value, instance.footprint or "", part.klm_id if part else instance.lib_id)
        target = excluded if dnp else groups
        line = target.get(key)
        if line is None:
            line = BomLine(
                value=value,
                footprint=instance.footprint or "",
                klm_id=part.klm_id if part else None,
                mpn=part.mpn if part else instance.field("MPN"),
                manufacturer=part.manufacturer if part else instance.field("Manufacturer"),
                lcsc=instance.field("LCSC"),
                dnp=dnp,
                part=part,
            )
            target[key] = line
        line.references.append(reference)


def _resolve(conn: sqlite3.Connection | None, instance: sch.SymbolInstance) -> Part | None:
    """Tie a placed symbol to its catalog part, by `KLM_ID` and nothing else.

    Name matching is deliberately not attempted here. A BOM that quietly
    attributes a line to the wrong part produces a wrong order, and unlike a
    vendoring mistake nothing downstream catches it.
    """
    if conn is None or not instance.klm_id:
        return None
    return get_part(conn, instance.klm_id)


def load_variants(raw: dict[str, object]) -> dict[str, Variant]:
    """Read the ``[variants.*]`` tables from a project's ``klm.toml``."""
    variants: dict[str, Variant] = {}
    for name, body in raw.items():
        if not isinstance(body, dict):
            continue
        dnp = body.get("dnp") or []
        overrides = body.get("overrides") or {}
        variants[name] = Variant(
            name=name,
            dnp=frozenset(str(r) for r in dnp if isinstance(dnp, list)),
            overrides={
                str(k): str(v)
                for k, v in (overrides.items() if isinstance(overrides, dict) else ())
            },
        )
    return variants
