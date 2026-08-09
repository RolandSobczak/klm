"""`klm lint` — the rules that keep a library consistent.

Every rule has an ID, a severity, and an honest answer to "can this be fixed
mechanically?" (docs/05 §4). The division that matters:

* A **fixable** rule has exactly one correct resolution and touches no
  semantics. Renaming `Manufacturer_Part_Number` to `MPN` moves no value;
  rewriting `0.1uF` to `100nF` changes no capacitance.
* Everything else is **reported**. `Value: 100nF` disagreeing with a datasheet
  parameter of 1 µF is a decision, and klm does not get to make it.

Fixes are applied to the stored assets, not to `generated/` — the generated
library is rebuilt from the catalog, so a fix written there would survive
exactly until the next `klm generate`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from klm import fields as field_schema
from klm.categories import find_category
from klm.config import Config
from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.sexpr import Document, SExp, dumps, loads
from klm.model import Parameter, Part, PartStatus
from klm.services.catalog import list_parts, save_part
from klm.services.register import MODELS_VAR
from klm.store.assets import AssetError, AssetKind, AssetStore
from klm.units import Quantity, ValueParseError, format_quantity, parse_value, try_parse_value

__all__ = ["RULES", "Finding", "LintReport", "Rule", "Selector", "Severity", "lint_catalog"]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    summary: str
    fixable: bool = False

    @property
    def group(self) -> str:
        return self.id[0]


_RULE_LIST = (
    Rule("S001", Severity.ERROR, "Required field missing"),
    Rule("S002", Severity.WARNING, "Field uses a known alias instead of the canonical name", True),
    Rule("S003", Severity.WARNING, "Unknown field not declared in config"),
    Rule("S004", Severity.ERROR, "Symbol carries a KLM_ID that is not this part's"),
    Rule("S005", Severity.ERROR, "Part is deprecated but still generated"),
    Rule("S006", Severity.WARNING, "Field present but empty"),
    Rule("S007", Severity.WARNING, "Category not set, so value rules cannot run"),
    Rule("V001", Severity.WARNING, "Value not in canonical display form", True),
    Rule("V002", Severity.ERROR, "Value unparseable for a category that implies a number"),
    Rule("V003", Severity.ERROR, "Value contradicts a stored parameter"),
    Rule("V004", Severity.WARNING, "Field expected for this category is missing"),
    Rule("A001", Severity.ERROR, "Footprint does not resolve"),
    Rule("A002", Severity.ERROR, "Footprint references a 3D model by absolute path", True),
    Rule("A003", Severity.WARNING, "No 3D model attached"),
    Rule("A004", Severity.ERROR, "Referenced 3D model is missing from the store"),
    Rule("A005", Severity.WARNING, "Asset QA status is warn or fail"),
    Rule("A006", Severity.ERROR, "Referenced asset is missing from the store or unreadable"),
)

RULES: dict[str, Rule] = {rule.id: rule for rule in _RULE_LIST}


@dataclass(frozen=True)
class Finding:
    rule: str
    location: str
    message: str
    fixed: bool = False

    @property
    def severity(self) -> Severity:
        return RULES[self.rule].severity

    @property
    def fixable(self) -> bool:
        return RULES[self.rule].fixable

    def __str__(self) -> str:
        suffix = "  [fixed]" if self.fixed else ("  [fixable]" if self.fixable else "")
        return f"{self.location}  {self.rule}  {self.severity}  {self.message}{suffix}"


@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    parts_checked: int = 0

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == severity and not f.fixed)

    @property
    def fixed(self) -> list[Finding]:
        return [f for f in self.findings if f.fixed]

    def failed(self, max_severity: str) -> bool:
        """True when the outstanding findings should make the command exit non-zero."""
        if self.count(Severity.ERROR):
            return True
        return max_severity == "warning" and bool(self.count(Severity.WARNING))


class Selector:
    """`--select` / `--ignore`, matching either a rule ID or a group letter."""

    def __init__(self, select: tuple[str, ...] = (), ignore: tuple[str, ...] = ()) -> None:
        self._select = tuple(item.strip().upper() for item in select if item.strip())
        self._ignore = tuple(item.strip().upper() for item in ignore if item.strip())

    def __contains__(self, rule_id: str) -> bool:
        if any(rule_id.startswith(item) for item in self._ignore):
            return False
        if not self._select:
            return True
        return any(rule_id.startswith(item) for item in self._select)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lint_catalog(
    conn: sqlite3.Connection,
    store: AssetStore,
    config: Config,
    *,
    selector: Selector | None = None,
    fix: bool = False,
    dry_run: bool = False,
) -> LintReport:
    """Check every part in the catalog.

    With ``fix``, mechanical findings are resolved and the affected assets
    re-stored; ``dry_run`` reports what would be fixed and writes nothing. The
    two share one code path, so what is printed is what would happen.
    """
    active = selector or Selector()
    report = LintReport()

    for part in list_parts(conn):
        report.parts_checked += 1
        _lint_part(conn, store, config, active, part, report, fix=fix, dry_run=dry_run)

    return report


def _lint_part(
    conn: sqlite3.Connection,
    store: AssetStore,
    config: Config,
    selector: Selector,
    part: Part,
    report: LintReport,
    *,
    fix: bool,
    dry_run: bool,
) -> None:
    where = f"catalog/{part.klm_id}"
    findings: list[Finding] = []

    symbol_doc = _load(store, part.symbol_hash, AssetKind.SYMBOL, where, "symbol", findings)
    symbol = None
    if symbol_doc is not None:
        extracted = sym.extract_symbols(symbol_doc)
        symbol = extracted[0] if extracted else None
        if symbol is None:
            findings.append(Finding("A006", where, "symbol asset contains no symbol"))

    properties = sym.properties(symbol) if symbol is not None else {}

    findings += _schema_rules(config, part, properties, where)
    findings += _value_rules(config, part, properties, where)
    findings += _asset_rules(conn, store, part, where)

    symbol_changed = symbol is not None and fix and _fix_symbol(config, part, symbol, findings)
    footprint_doc = _fix_footprint(store, part, findings) if fix else None

    if (symbol_changed or footprint_doc is not None) and not dry_run:
        _restore_assets(
            conn,
            store,
            part,
            symbol_doc if symbol_changed else None,
            footprint_doc,
        )

    report.findings.extend(f for f in findings if f.rule in selector)


# ---------------------------------------------------------------------------
# Schema rules
# ---------------------------------------------------------------------------


def _schema_rules(
    config: Config, part: Part, properties: dict[str, str], where: str
) -> list[Finding]:
    findings: list[Finding] = []

    if not part.mpn.strip():
        findings.append(Finding("S001", where, "MPN is empty"))
    if not part.manufacturer.strip() or part.manufacturer == "Unknown":
        findings.append(Finding("S001", where, "Manufacturer is not set"))
    if not part.description.strip():
        findings.append(Finding("S001", where, "Description is empty"))

    if part.status is PartStatus.DEPRECATED and part.symbol_hash is not None:
        findings.append(
            Finding("S005", where, "deprecated part still has a symbol; it will not be generated")
        )

    if not part.category:
        findings.append(Finding("S007", where, "category is not set"))

    for name, value in properties.items():
        if field_schema.is_kicad_internal(name):
            continue
        canonical = config.fields.canonical(name)
        if canonical is not None and canonical != name:
            findings.append(Finding("S002", where, f"field '{name}' → '{canonical}'"))
        elif canonical is None and not field_schema.is_klm_reserved(name):
            findings.append(
                Finding("S003", where, f"field '{name}' is not canonical or declared in config")
            )
        if (
            canonical is not None
            and not value.strip()
            and canonical not in field_schema.KICAD_EMPTY_BY_DEFAULT
        ):
            findings.append(Finding("S006", where, f"field '{name}' is empty"))

    stored_id = properties.get(field_schema.KLM_ID, "").strip()
    if stored_id and stored_id != part.klm_id:
        findings.append(
            Finding("S004", where, f"symbol carries KLM_ID {stored_id}, not {part.klm_id}")
        )

    return findings


# ---------------------------------------------------------------------------
# Value rules
# ---------------------------------------------------------------------------


def _value_rules(
    config: Config, part: Part, properties: dict[str, str], where: str
) -> list[Finding]:
    findings: list[Finding] = []
    category = find_category(part.category)
    if category is None:
        return findings

    for expected in category.expects:
        if not properties.get(expected, "").strip() and part.parameter(expected) is None:
            findings.append(
                Finding("V004", where, f"{category.name} has no '{expected}' field")
            )

    unit = category.unit
    raw = properties.get("Value", "").strip()
    if unit is None or not raw:
        return findings

    try:
        quantity = parse_value(raw, unit=unit)
    except ValueParseError as exc:
        findings.append(Finding("V002", where, f"Value {raw!r} is not a {unit} value: {exc}"))
        return findings

    canonical = format_quantity(quantity, significant=config.significant_figures)
    if canonical != raw:
        findings.append(Finding("V001", where, f"Value {raw!r} → {canonical!r}"))

    stated = _parameter_quantity(part.parameter("Value"), unit)
    if stated is not None and not stated.matches(quantity, rel_tol=1e-3):
        findings.append(
            Finding(
                "V003",
                where,
                f"Value {raw!r} contradicts the stored parameter {format_quantity(stated)!r}",
            )
        )
    return findings


def _parameter_quantity(parameter: Parameter | None, unit: str) -> Quantity | None:
    """A parameter read as a quantity, or ``None`` if it does not state one."""
    if parameter is None:
        return None
    if parameter.value_num is not None:
        return Quantity(parameter.value_num, parameter.unit or unit)
    if parameter.value_text:
        return try_parse_value(parameter.value_text, unit=parameter.unit or unit)
    return None


# ---------------------------------------------------------------------------
# Asset rules
# ---------------------------------------------------------------------------


def _asset_rules(
    conn: sqlite3.Connection, store: AssetStore, part: Part, where: str
) -> list[Finding]:
    findings: list[Finding] = []
    findings += _qa_rules(conn, part, where)

    if part.footprint_hash is None:
        findings.append(Finding("A001", where, "part has no footprint"))
    elif not store.exists(part.footprint_hash, AssetKind.FOOTPRINT):
        findings.append(
            Finding("A006", where, f"footprint {part.footprint_hash} is not in the store")
        )
    else:
        document = _read(store, part.footprint_hash, AssetKind.FOOTPRINT)
        if document is None:
            findings.append(Finding("A006", where, "footprint asset is unreadable"))
        else:
            findings += [
                Finding("A002", where, f"3D model path is absolute: {path}")
                for path in fp.absolute_model_paths(document)
            ]

    if part.model3d_hash is None:
        findings.append(Finding("A003", where, "no 3D model attached"))
    elif not store.exists(part.model3d_hash, AssetKind.MODEL3D):
        findings.append(
            Finding("A004", where, f"3D model {part.model3d_hash} is not in the store")
        )
    return findings


def _qa_rules(conn: sqlite3.Connection, part: Part, where: str) -> list[Finding]:
    """A005 — an asset the QA gate was unhappy with is still in use.

    The gate itself arrives with the asset pipeline; until then this reports
    whatever status a row carries, and `unchecked` is not a complaint.
    """
    hashes = [h for h in (part.symbol_hash, part.footprint_hash, part.model3d_hash) if h]
    if not hashes:
        return []
    placeholders = ",".join("?" * len(hashes))
    rows = conn.execute(
        f"SELECT content_hash, kind, qa_status FROM asset WHERE content_hash IN ({placeholders})",
        hashes,
    ).fetchall()
    return [
        Finding("A005", where, f"{row['kind']} asset QA status is {row['qa_status']}")
        for row in rows
        if row["qa_status"] in ("warn", "fail")
    ]


def _load(
    store: AssetStore,
    content_hash: str | None,
    kind: AssetKind,
    where: str,
    label: str,
    findings: list[Finding],
) -> Document | None:
    if content_hash is None:
        findings.append(Finding("A006", where, f"part has no {label}"))
        return None
    document = _read(store, content_hash, kind)
    if document is None:
        findings.append(Finding("A006", where, f"{label} {content_hash} is missing or unreadable"))
    return document


def _read(store: AssetStore, content_hash: str, kind: AssetKind) -> Document | None:
    try:
        return loads(store.read_text(content_hash, kind))
    except (AssetError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Fixes
# ---------------------------------------------------------------------------


def _fix_symbol(config: Config, part: Part, symbol: SExp, findings: list[Finding]) -> bool:
    """Apply S002 and V001 to a symbol in memory. Returns True if it changed."""
    changed = False

    for node in sym.iter_properties(symbol):
        name = sym.property_name(node)
        canonical = config.fields.canonical(name)
        if canonical is not None and canonical != name:
            sym.rename_property(node, canonical)
            _mark_fixed(findings, "S002", f"field '{name}' → '{canonical}'")
            changed = True

    category = find_category(part.category)
    if category is None or category.unit is None:
        return changed

    value_node = sym.find_property(symbol, "Value")
    if value_node is None:
        return changed
    raw = sym.property_value(value_node).strip()
    try:
        quantity = parse_value(raw, unit=category.unit)
    except ValueParseError:
        # Reported as V002; guessing at what the author meant is exactly the
        # thing this codebase refuses to do.
        return changed

    canonical_value = format_quantity(quantity, significant=config.significant_figures)
    if canonical_value != raw:
        sym.set_property(symbol, "Value", canonical_value, hidden=False)
        _mark_fixed(findings, "V001", f"Value {raw!r} → {canonical_value!r}")
        changed = True
    return changed


def _fix_footprint(store: AssetStore, part: Part, findings: list[Finding]) -> Document | None:
    """Apply A002. Returns the rewritten footprint, or ``None`` if unchanged."""
    if part.footprint_hash is None:
        return None
    document = _read(store, part.footprint_hash, AssetKind.FOOTPRINT)
    if document is None:
        return None
    if not fp.rewrite_model_paths(document, env_var=MODELS_VAR):
        return None
    _mark_fixed(findings, "A002", None)
    return document


def _mark_fixed(findings: list[Finding], rule: str, message: str | None) -> None:
    """Flip reported findings to fixed, so nothing is counted twice.

    ``message`` of ``None`` marks every finding for the rule: one call to
    :func:`klm.kicad.footprints.rewrite_model_paths` resolves all of a
    footprint's absolute paths at once.
    """
    for index, finding in enumerate(findings):
        if finding.rule != rule or finding.fixed:
            continue
        if message is not None and finding.message != message:
            continue
        findings[index] = Finding(finding.rule, finding.location, finding.message, fixed=True)
        if message is not None:
            return


def _restore_assets(
    conn: sqlite3.Connection,
    store: AssetStore,
    part: Part,
    symbol_doc: Document | None,
    footprint_doc: Document | None,
) -> None:
    """Re-store the changed assets and point the part at them.

    Assets are immutable and content-addressed, so a fix produces a *new*
    asset; the old one stays where it is. That is what makes `--fix`
    recoverable — the pre-fix symbol is still in the store, byte for byte.

    `dumps`, not `dumps_canonical`: the lossless writer reproduces every node
    klm did not touch exactly as it was stored, so the diff between the two
    assets is only the fix.
    """
    if symbol_doc is not None:
        part.symbol_hash = store.add_bytes(dumps(symbol_doc).encode("utf-8"), AssetKind.SYMBOL)
    if footprint_doc is not None:
        part.footprint_hash = store.add_bytes(
            dumps(footprint_doc).encode("utf-8"), AssetKind.FOOTPRINT
        )
    save_part(conn, part)
