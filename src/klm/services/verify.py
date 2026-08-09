"""``klm verify --clean-room`` — proving a project opens for someone else.

The argument for this whole module is in
[ADR-0007](../../docs/adr/0007-clean-room-verification.md): checking a vendored
project on the machine that vendored it is close to meaningless. That machine
has the `KLM` library registered, `KLM_LIBS` set and every 3D model on disk, so
a project that still references `KLM:STM32F103C8T6` in one forgotten sheet opens
perfectly there and shows a broken-symbol placeholder for everyone else.

**The implementation constraint that follows is the important one: nothing here
may touch the catalog or klm's configuration.** No `Paths`, no `connect`, no
`load_config`. Everything is answered from files inside the repository, plus
whatever a stock KiCad installation provides. That is what makes this a
different code path from `klm lint` rather than a flag on it — and the reason it
is worth the duplication is that a checker which could quietly fall back to the
catalog would pass on the one machine where passing means nothing.

What "a machine that has nothing" means, precisely: no klm catalog, no klm
global libraries, no user configuration — but a stock KiCad install. KiCad's own
standard libraries ship with KiCad, so any machine that can open the project has
them, and pretending otherwise would fail every real board on `power:GND`
(docs/14 Q11).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from klm.assets.kicad_libs import KicadLibraries, find_libraries
from klm.kicad import board as pcb
from klm.kicad import footprints as fp
from klm.kicad import schematic as sch
from klm.kicad.libtable import TableKind, load_table, read_entries
from klm.kicad.project import KiCadProject
from klm.kicad.sexpr import SExprError, loads
from klm.services.lockfile import LockError, read_lock

__all__ = [
    "GLOBAL_PREFIXES",
    "Finding",
    "VerifyReport",
    "verify_clean_room",
]

#: Library nicknames that only exist on a machine with klm installed. Finding
#: one in a vendored project is the definition of partial vendoring.
GLOBAL_PREFIXES = frozenset({"KLM"})

#: `${KIPRJMOD}` is KiCad's own project-relative variable and always resolves.
#: Anything else must be declared, or a collaborator gets an unresolved path.
_PROJECT_VAR = "KIPRJMOD"
_KNOWN_VARS = frozenset({_PROJECT_VAR, "KICAD6_3DMODEL_DIR", "KICAD7_3DMODEL_DIR",
                         "KICAD8_3DMODEL_DIR", "KICAD9_3DMODEL_DIR", "KICAD_3DMODEL_DIR"})

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: `/home/rs/…`, `C:\…`, `\\server\…`. The classic leak, and invisible locally.
_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\\\\)")

#: A POSIX string that is actually a *file* path, not something else that
#: happens to start with a slash. KiCad quotes net names (`/SDA`, `/BAT+`) and
#: hierarchical sheet paths (`/3a1f-…/c4d2-…`) exactly like paths, and on one
#: real board that produced 124 false errors against 2 true ones. A file path
#: has at least one directory component *and* a filename with an extension.
_POSIX_FILE = re.compile(r"^/(?:[^/\s]+/)+[^/\s]+\.[A-Za-z0-9]{1,6}$")
#: A drive letter or UNC prefix is unambiguous on its own — nothing else in a
#: KiCad file looks like `C:\` or `\\server\share`.
_WINDOWS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    """`error`, `warning` or `info`."""
    message: str
    file: str = ""
    line: int = 0

    @property
    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or ""


@dataclass
class VerifyReport:
    project: KiCadProject
    findings: list[Finding] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    """Check name → its summary line, in the order they ran."""

    def add(
        self,
        check: str,
        severity: str,
        message: str,
        *,
        file: str = "",
        line: int = 0,
    ) -> None:
        self.findings.append(Finding(check, severity, message, file, line))

    def note(self, check: str, summary: str) -> None:
        self.checks[check] = summary

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    @property
    def failed(self) -> bool:
        return self.count("error") > 0

    def status(self, check: str) -> str:
        severities = {f.severity for f in self.findings if f.check == check}
        if "error" in severities:
            return "error"
        if "warning" in severities:
            return "warning"
        return "info" if check in self.checks and self.checks[check].startswith("~") else "pass"


def verify_clean_room(
    project: KiCadProject,
    *,
    libraries: KicadLibraries | None = None,
    require_3d: bool = False,
) -> VerifyReport:
    """Check that this repository is enough to open the project.

    Takes a project and nothing else — no connection, no config, no ``Paths``.
    That signature is the guarantee.
    """
    report = VerifyReport(project=project)
    stock = libraries if libraries is not None else find_libraries()

    tables = _check_tables(project, report)
    _check_symbols(project, report, tables, stock)
    _check_footprints(project, report, tables, stock)
    _check_models(project, report, require_3d=require_3d)
    _check_paths(project, report)
    _check_lock(project, report)
    return report


# ---------------------------------------------------------------------------
# Library tables
# ---------------------------------------------------------------------------


@dataclass
class ProjectTables:
    symbols: dict[str, Path] = field(default_factory=dict)
    footprints: dict[str, Path] = field(default_factory=dict)
    """Nickname → resolved path inside the repository."""


def _check_tables(project: KiCadProject, report: VerifyReport) -> ProjectTables:
    """Project-level tables must exist and be ``${KIPRJMOD}``-relative.

    An absolute URI, or one built on a klm variable, resolves on exactly one
    machine — and it is the machine where nobody will notice.
    """
    tables = ProjectTables()
    check = "library tables"
    found_any = False

    for path, kind, target in (
        (project.sym_lib_table, TableKind.SYMBOL, tables.symbols),
        (project.fp_lib_table, TableKind.FOOTPRINT, tables.footprints),
    ):
        if not path.exists():
            continue
        found_any = True
        for entry in read_entries(load_table(path, kind)):
            resolved = _resolve_uri(project, entry.uri)
            if resolved is None:
                variables = sorted(set(_VARIABLE.findall(entry.uri)) - _KNOWN_VARS)
                if variables:
                    report.add(
                        check,
                        "error",
                        f"library {entry.name!r} uses ${{{variables[0]}}}, which a "
                        "collaborator will not have set",
                        file=path.name,
                    )
                else:
                    report.add(
                        check,
                        "error",
                        f"library {entry.name!r} has an absolute URI: {entry.uri}",
                        file=path.name,
                    )
                continue
            if not resolved.exists():
                report.add(
                    check,
                    "error",
                    f"library {entry.name!r} points at {entry.uri}, which is not in the repository",
                    file=path.name,
                )
                continue
            target[entry.name] = resolved

    if not found_any:
        report.note(check, "~none — the project has no project-level library tables")
    else:
        report.note(
            check,
            f"project-level, ${{{_PROJECT_VAR}}}-relative "
            f"({len(tables.symbols)} symbol, {len(tables.footprints)} footprint)",
        )
    return tables


def _resolve_uri(project: KiCadProject, uri: str) -> Path | None:
    """A URI as a path inside the repository, or ``None`` if it escapes it."""
    text = uri.strip()
    if not text:
        return None
    if _ABSOLUTE.match(text):
        return None
    expanded = text.replace(f"${{{_PROJECT_VAR}}}", str(project.root))
    if _VARIABLE.search(expanded):
        return None
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = project.root / candidate
    try:
        candidate.resolve().relative_to(project.root.resolve())
    except ValueError:
        return None
    return candidate


# ---------------------------------------------------------------------------
# Symbols and footprints
# ---------------------------------------------------------------------------


def _check_symbols(
    project: KiCadProject,
    report: VerifyReport,
    tables: ProjectTables,
    stock: KicadLibraries,
) -> None:
    check = "symbol resolution"
    in_repo = {name: _symbol_names(path) for name, path in tables.symbols.items()}
    resolved = 0
    from_stock = 0

    for sheet in project.schematics:
        document = _load(sheet, report, check)
        if document is None:
            continue
        for instance in sch.iter_symbol_instances(document, sheet=sheet.name):
            nickname, name = instance.library, instance.name
            if nickname in GLOBAL_PREFIXES:
                report.add(
                    check,
                    "error",
                    f"{instance.reference or name} still references {instance.lib_id} — "
                    "the project is only partly vendored",
                    file=sheet.name,
                )
                continue
            if nickname in in_repo:
                if name in in_repo[nickname]:
                    resolved += 1
                else:
                    report.add(
                        check,
                        "error",
                        f"{instance.reference or '?'}: {instance.lib_id} is not in "
                        f"{tables.symbols[nickname].name}",
                        file=sheet.name,
                    )
                continue
            if _stock_symbol(stock, nickname, name):
                from_stock += 1
                continue
            report.add(
                check,
                "error",
                f"{instance.reference or '?'}: nothing in this repository or in KiCad's own "
                f"libraries provides {instance.lib_id}",
                file=sheet.name,
            )

    report.note(
        check,
        f"{resolved} from the repository, {from_stock} from KiCad's standard libraries",
    )


def _symbol_names(path: Path) -> set[str]:
    from klm.kicad import symbols as sym

    try:
        with open(path, encoding="utf-8", newline="") as handle:
            document = loads(handle.read())
    except (OSError, SExprError):
        return set()
    return {n for n in (sym.symbol_name(s) for s in sym.extract_symbols(document)) if n}


def _stock_symbol(stock: KicadLibraries, nickname: str, name: str) -> bool:
    """Whether a stock KiCad install provides this symbol.

    Answered by looking, not by consulting a list of library names. A list would
    go stale with every KiCad release and would still say nothing about the
    user's own libraries, which is the case that actually matters
    (docs/adr/0010, docs/14 Q11).
    """
    return stock.find_symbol(f"{nickname}:{name}") is not None


def _check_footprints(
    project: KiCadProject,
    report: VerifyReport,
    tables: ProjectTables,
    stock: KicadLibraries,
) -> None:
    check = "footprint resolution"
    if project.board is None:
        report.note(check, "~no board in this project")
        return

    document = _load(project.board, report, check)
    if document is None:
        return

    resolved = 0
    from_stock = 0
    for placed in pcb.iter_footprints(document):
        nickname, name = placed.library, placed.name
        if nickname in GLOBAL_PREFIXES:
            report.add(
                check,
                "error",
                f"{placed.reference or name} still references {placed.lib_id} — "
                "the project is only partly vendored",
                file=project.board.name,
            )
            continue
        directory = tables.footprints.get(nickname)
        if directory is not None:
            if (directory / f"{name}.kicad_mod").exists():
                resolved += 1
            else:
                report.add(
                    check,
                    "error",
                    f"{placed.reference or '?'}: {placed.lib_id} is not in {directory.name}",
                    file=project.board.name,
                )
            continue
        if stock.find_footprint(f"{nickname}:{name}") is not None:
            from_stock += 1
            continue
        report.add(
            check,
            "error",
            f"{placed.reference or '?'}: nothing in this repository or in KiCad's own "
            f"libraries provides {placed.lib_id}",
            file=project.board.name,
        )

    report.note(
        check,
        f"{resolved} from the repository, {from_stock} from KiCad's standard libraries",
    )


# ---------------------------------------------------------------------------
# 3D models, paths, lock
# ---------------------------------------------------------------------------


def _check_models(project: KiCadProject, report: VerifyReport, *, require_3d: bool) -> None:
    """An absent model is fine and expected; a *broken* path is not.

    Vendoring without `--with-3d` is the documented default for a collaboration
    repository, so a footprint with no model at all is the normal case. What
    must never happen is a reference that points somewhere and finds nothing.
    """
    check = "3D models"
    referenced = 0
    missing = 0

    for pretty in _pretty_dirs(project):
        for path in sorted(pretty.glob("*.kicad_mod")):
            try:
                root = loads(path.read_text(encoding="utf-8")).root
            except (OSError, SExprError, ValueError):
                continue
            for reference in fp.model_paths(root):
                referenced += 1
                resolved = _resolve_uri(project, reference)
                if resolved is None or not resolved.exists():
                    missing += 1
                    report.add(
                        check,
                        "error",
                        f"{path.name} references a 3D model that is not in the repository: "
                        f"{reference}",
                        file=path.name,
                    )

    if referenced == 0:
        severity_note = "~omitted — no footprint in the repository references one"
        if require_3d:
            report.add(
                check, "error", "the project policy requires 3D models, and none are present"
            )
        report.note(check, severity_note)
    else:
        report.note(check, f"{referenced - missing}/{referenced} resolve inside the repository")


def _check_paths(project: KiCadProject, report: VerifyReport) -> None:
    """No absolute filesystem path in any project file.

    `/home/rsobczak/…` in a `.kicad_pcb` is the classic leak: it resolves for
    the author forever and for nobody else, ever.
    """
    check = "absolute paths"
    found = 0
    for path in _project_files(project):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for quoted in re.findall(r'"([^"]{3,})"', line):
                if _looks_like_a_file_path(quoted):
                    found += 1
                    report.add(
                        check,
                        "error",
                        f"absolute path {quoted!r} resolves only on the machine that wrote it",
                        file=str(path.relative_to(project.root)),
                        line=number,
                    )
    report.note(check, "none" if not found else f"{found} found")


def _looks_like_a_file_path(value: str) -> bool:
    """Whether an absolute-looking string is really a filesystem path.

    Deliberately conservative. The precise checks — `(model …)` references and
    library-table URIs — are done by name elsewhere; this is the backstop that
    catches a leak somewhere nobody thought to look, and a backstop that cries
    wolf 124 times per board is one people learn to ignore.
    """
    return bool(_WINDOWS.match(value) or _POSIX_FILE.match(value))


def _project_files(project: KiCadProject) -> list[Path]:
    files = [*project.schematics]
    if project.board is not None:
        files.append(project.board)
    for pretty in _pretty_dirs(project):
        files.extend(sorted(pretty.glob("*.kicad_mod")))
    if project.libraries.is_dir():
        files.extend(sorted(project.libraries.glob("*.kicad_sym")))
    for table in (project.sym_lib_table, project.fp_lib_table):
        if table.exists():
            files.append(table)
    return files


def _pretty_dirs(project: KiCadProject) -> list[Path]:
    """Every `.pretty` directory the repository carries."""
    if not project.libraries.is_dir():
        return []
    return sorted(p for p in project.libraries.glob("*.pretty") if p.is_dir())


def _check_lock(project: KiCadProject, report: VerifyReport) -> None:
    """The lock must still describe the library beside it.

    Catches a vendored library edited by hand and never pushed back — the state
    where the repository and the catalog have silently disagreed for weeks.
    """
    check = "lock file"
    if not project.lock_file.exists():
        report.note(check, "~absent — this project is not vendored")
        return
    try:
        lock = read_lock(project.lock_file)
    except LockError as exc:
        report.add(check, "error", str(exc), file=project.lock_file.name)
        report.note(check, "unreadable")
        return

    # Imported here so the module's promise holds for every other path: nothing
    # above this line can reach the catalog even by accident.
    from klm.services.vendor import read_vendored

    library = read_vendored(project, lock.library_name)
    drifted = []
    for entry in lock.sorted_entries():
        symbol_now = library.symbol_hash(entry.symbol_name)
        if entry.symbol_name and symbol_now != entry.vendored_symbol_hash:
            drifted.append(entry.symbol_name)
        elif (
            entry.footprint_name
            and library.footprint_hash(entry.footprint_name) != entry.vendored_footprint_hash
        ):
            drifted.append(entry.footprint_name)

    if drifted:
        for name in drifted:
            report.add(
                check,
                "error",
                f"{name} differs from what {project.lock_file.name} records",
                file=project.lock_file.name,
            )
    report.note(check, f"consistent, {len(lock.entries)} parts" if not drifted else "inconsistent")


def _load(path: Path, report: VerifyReport, check: str):  # type: ignore[no-untyped-def]
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return loads(handle.read())
    except (OSError, SExprError) as exc:
        report.add(check, "error", f"could not read {path.name}: {exc}", file=path.name)
        return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def to_json(report: VerifyReport) -> str:
    payload = {
        "project": report.project.name,
        "failed": report.failed,
        "checks": [
            {"name": name, "summary": summary.lstrip("~"), "status": report.status(name)}
            for name, summary in report.checks.items()
        ],
        "findings": [
            {
                "check": f.check,
                "severity": f.severity,
                "message": f.message,
                "file": f.file,
                "line": f.line,
            }
            for f in report.findings
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
