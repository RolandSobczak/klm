"""``klm fab`` — the fabrication package, and the preflight that gates it.

Solves [P5](../../docs/01-vision-and-problems.md): a fab package that needs no
hand-fixing. The pipeline is docs/09 §2, and the split it describes is the
important part — steps 1-4 are generic, steps 5-6 belong to a
:class:`~klm.fab.profiles.FabProfile`.

Two things govern the design.

**The package is not written unless preflight passes.** A fab package that
exists is a fab package someone will upload, so a half-checked one is worse than
none. `--check` runs preflight alone and writes nothing, which is what CI wants.

**`manifest.json` is for six weeks later**, when a board comes back wrong and
the question is why. It records the source commit, tool versions, the variant,
and every correction applied to every reference — including the ones that were
*not* confirmed against a physical board, because that is usually the answer.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import sqlite3
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from klm import __version__
from klm.cad.kicadcli import KiCadCli, KiCadCliError, KiCadCliUnavailable
from klm.config import Config
from klm.fab.profiles import JLCPCB, FabProfile, Placement, render_csv
from klm.kicad import board as pcb
from klm.kicad import footprints as fp
from klm.kicad.project import KiCadProject
from klm.kicad.sexpr import SExp, loads
from klm.services import corrections
from klm.services.bom import BomLine, BomReport, Variant, extract_bom
from klm.services.lint import Selector, Severity, lint_catalog
from klm.services.offers import list_offers
from klm.store.assets import AssetStore

__all__ = [
    "Check",
    "FabReport",
    "PreflightReport",
    "fab_package",
    "parse_pos_csv",
    "preflight",
]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    blocking: bool
    detail: str = ""

    @property
    def status(self) -> str:
        if self.passed:
            return "pass"
        return "fail" if self.blocking else "warn"


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, *, blocking: bool, detail: str = "") -> None:
        self.checks.append(Check(name, passed, blocking, detail))

    @property
    def blocked(self) -> bool:
        return any(not c.passed and c.blocking for c in self.checks)

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and not c.blocking]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.blocking]


@dataclass
class FabReport:
    project: KiCadProject
    output_dir: Path
    profile: FabProfile
    preflight: PreflightReport
    bom: BomReport | None = None
    placements: list[Placement] = field(default_factory=list)
    written: bool = False
    unconfirmed: list[str] = field(default_factory=list)
    """References whose rotation no physical board has confirmed (ADR-0011)."""


# ---------------------------------------------------------------------------
# The placement list
# ---------------------------------------------------------------------------

#: `kicad-cli pcb export pos --format csv` writes these headers. Matched
#: case-insensitively and by several spellings, because they have moved between
#: KiCad releases and klm cannot test against every one.
_POS_ALIASES = {
    "ref": "reference",
    "refdes": "reference",
    "designator": "reference",
    "val": "value",
    "value": "value",
    "package": "footprint",
    "footprint": "footprint",
    "posx": "x",
    "mid x": "x",
    "posy": "y",
    "mid y": "y",
    "rot": "rotation",
    "rotation": "rotation",
    "side": "side",
    "layer": "side",
}


def parse_pos_csv(text: str) -> list[Placement]:
    """Read KiCad's placement CSV into placements, before any correction.

    KiCad's own export is used rather than the board's `(at …)` nodes because
    the position file has conventions of its own — a flipped Y axis and an
    origin that may be the drill/place origin. Getting either wrong moves every
    part on the board, and the output would still look plausible.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if row]
    if not rows:
        return []

    header = [_POS_ALIASES.get(cell.strip().lower(), cell.strip().lower()) for cell in rows[0]]
    placements: list[Placement] = []
    for row in rows[1:]:
        record = dict(zip(header, (cell.strip() for cell in row), strict=False))
        if not record.get("reference"):
            continue
        placements.append(
            Placement(
                reference=record["reference"],
                value=record.get("value", ""),
                footprint=record.get("footprint", ""),
                x=_float(record.get("x")),
                y=_float(record.get("y")),
                rotation=_float(record.get("rotation")),
                side=_side(record.get("side", "top")),
            )
        )
    return placements


def _float(raw: str | None) -> float:
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def _side(raw: str) -> str:
    return "bottom" if raw.strip().lower() in ("bottom", "b", "back") else "top"


def apply_corrections(
    conn: sqlite3.Connection,
    placements: list[Placement],
    bom: BomReport,
    profile: FabProfile,
) -> list[Placement]:
    """Apply the resolved correction to each placement, recording its origin."""
    by_reference: dict[str, BomLine] = {
        reference: line for line in bom.lines for reference in line.references
    }

    corrected: list[Placement] = []
    for placement in placements:
        line = by_reference.get(placement.reference)
        if not profile.apply_rotation_corrections or line is None:
            corrected.append(placement)
            continue
        correction = corrections.resolve(
            conn,
            klm_id=line.klm_id,
            footprint=placement.footprint or line.footprint_name,
            field_value=_fab_field(line),
        )
        corrected.append(
            Placement(
                reference=placement.reference,
                value=placement.value or line.value,
                footprint=placement.footprint,
                x=placement.x + correction.offset_x,
                y=placement.y + correction.offset_y,
                rotation=correction.apply(placement.rotation),
                side=placement.side,
                klm_id=line.klm_id or "",
                lcsc=line.lcsc,
                correction_source=correction.source,
                confirmed=correction.confirmed,
            )
        )
    return corrected


def _fab_field(line: BomLine) -> str | None:
    if line.part is None:
        return None
    parameter = line.part.parameter(corrections.FAB_ROTATION_FIELD)
    if parameter is None:
        return None
    return parameter.value_text or (str(parameter.value_num) if parameter.value_num else None)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(
    conn: sqlite3.Connection,
    store: AssetStore,
    config: Config,
    project: KiCadProject,
    *,
    bom: BomReport,
    cli: KiCadCli,
    work_dir: Path,
    assembly: bool = True,
    allow_dirty: bool = False,
) -> PreflightReport:
    """Every gate from docs/09 §7. Writes only into ``work_dir``."""
    report = PreflightReport()

    report.add(
        "board present",
        project.board is not None,
        blocking=True,
        detail="" if project.board else "no .kicad_pcb in the project",
    )
    if project.board is None:
        return report

    _check_erc(cli, project, work_dir, report)
    _check_drc(cli, project.board, work_dir, report)
    _check_lint(conn, store, config, bom, report)
    _check_resolution(bom, report)
    _check_board_graphics(project.board, report)
    if assembly:
        _check_assembly(conn, bom, report)
    _check_git(project, report, allow_dirty=allow_dirty)
    return report


def _check_erc(
    cli: KiCadCli, project: KiCadProject, work_dir: Path, report: PreflightReport
) -> None:
    sheet = project.schematics[0] if project.schematics else None
    if sheet is None:
        report.add("ERC", False, blocking=True, detail="no schematic to check")
        return
    try:
        result = cli.run_erc(sheet, work_dir / "erc-report.txt")
    except (KiCadCliUnavailable, KiCadCliError) as exc:
        report.add("ERC", False, blocking=True, detail=str(exc))
        return
    report.add("ERC", result.ok, blocking=True, detail="" if result.ok else result.message())


def _check_drc(cli: KiCadCli, board: Path, work_dir: Path, report: PreflightReport) -> None:
    try:
        result = cli.run_drc(board, work_dir / "drc-report.txt")
    except (KiCadCliUnavailable, KiCadCliError) as exc:
        report.add("DRC", False, blocking=True, detail=str(exc))
        return
    report.add("DRC", result.ok, blocking=True, detail="" if result.ok else result.message())


def _check_lint(
    conn: sqlite3.Connection,
    store: AssetStore,
    config: Config,
    bom: BomReport,
    report: PreflightReport,
) -> None:
    """Lint only the parts this board uses.

    An unrelated draft elsewhere in the catalog missing a datasheet is not a
    reason to refuse to fabricate this board.
    """
    used = {line.klm_id for line in bom.lines if line.klm_id}
    if not used:
        report.add("catalog lint", True, blocking=True, detail="no catalog parts on this board")
        return
    result = lint_catalog(conn, store, config, selector=Selector(), only=used)
    errors = result.count(Severity.ERROR)
    report.add(
        "catalog lint",
        errors == 0,
        blocking=True,
        detail="" if errors == 0 else f"{errors} error(s) in the parts this board uses",
    )


def _check_resolution(bom: BomReport, report: PreflightReport) -> None:
    report.add(
        "every part resolves to the catalog",
        not bom.unresolved,
        blocking=True,
        detail=", ".join(bom.unresolved[:8]) + ("…" if len(bom.unresolved) > 8 else ""),
    )


def _check_board_graphics(board: Path, report: PreflightReport) -> None:
    """Courtyards on every footprint, and a board outline that closes.

    An open outline is the failure that gets furthest before anyone notices: the
    gerbers plot, the upload succeeds, and the fab either guesses the edge or
    asks — after the order was placed.
    """
    with open(board, encoding="utf-8", newline="") as handle:
        root = loads(handle.read()).root

    missing = [
        pcb.footprint_reference(node) or "?"
        for node in pcb.footprint_nodes(root)
        if not (fp.graphics_on(node, "F.CrtYd") or fp.graphics_on(node, "B.CrtYd"))
    ]
    report.add(
        "every footprint has a courtyard",
        not missing,
        blocking=True,
        detail=", ".join(sorted(missing)[:8]),
    )

    closed, detail = _outline_closed(root)
    report.add("board outline is closed", closed, blocking=True, detail=detail)


#: How far apart two Edge.Cuts endpoints may be and still count as joined.
#: On real boards KiCad's arc endpoints disagree with the lines they meet by a
#: few microns — an artefact of how it snaps arcs, not a gap. 10 µm is three
#: orders of magnitude below any fab's tolerance and absorbs it.
OUTLINE_TOLERANCE_MM = 0.01


def _outline_closed(root: SExp) -> tuple[bool, str]:
    """Every endpoint on Edge.Cuts must be shared by an even number of segments.

    A rectangle or circle is closed by construction; an arc or line contributes
    two endpoints, and an outline drawn correctly pairs every one of them.

    Endpoints are *clustered* rather than rounded, because rounding fails
    exactly where it matters: two points 6 µm apart either side of a rounding
    boundary land in different buckets however fine the grid is made.
    """
    items = fp.graphics_on(root, "Edge.Cuts")
    if not items:
        return False, "nothing on Edge.Cuts"

    clusters: list[tuple[float, float]] = []
    counts: Counter[int] = Counter()
    for node in items:
        if node.name in ("fp_rect", "fp_circle", "gr_rect", "gr_circle"):
            continue
        ends = fp.endpoints(node)
        if ends is None:
            continue
        for point in ends:
            counts[_cluster(clusters, point)] += 1

    loose = [clusters[index] for index, count in counts.items() if count % 2]
    if loose:
        return False, f"{len(loose)} open endpoint(s), e.g. ({loose[0][0]:.3f}, {loose[0][1]:.3f})"
    return True, ""


def _cluster(clusters: list[tuple[float, float]], point: tuple[float, float]) -> int:
    """The index of the cluster this point joins, creating one if it joins none."""
    for index, existing in enumerate(clusters):
        if (
            abs(existing[0] - point[0]) <= OUTLINE_TOLERANCE_MM
            and abs(existing[1] - point[1]) <= OUTLINE_TOLERANCE_MM
        ):
            return index
    clusters.append(point)
    return len(clusters) - 1


def _check_assembly(conn: sqlite3.Connection, bom: BomReport, report: PreflightReport) -> None:
    missing = [line.designators for line in bom.assembly_lines() if not line.lcsc]
    report.add(
        "assembly parts have LCSC numbers",
        not missing,
        blocking=True,
        detail=", ".join(missing[:8]),
    )

    short: list[str] = []
    for line in bom.assembly_lines():
        if not line.klm_id:
            continue
        stock = max(
            (offer.stock or 0 for offer in list_offers(conn, klm_id=line.klm_id)), default=0
        )
        if stock < line.quantity:
            short.append(f"{line.mpn or line.value} ({stock} < {line.quantity})")
    report.add(
        "assembly parts are in stock",
        not short,
        blocking=False,
        detail=", ".join(short[:6]) + "  (from stored offers; run klm refresh)",
    )


def _check_git(project: KiCadProject, report: PreflightReport, *, allow_dirty: bool) -> None:
    dirty, detail = _git_dirty(project.root)
    report.add(
        "git tree is clean",
        not dirty or allow_dirty,
        blocking=False,
        detail=detail,
    )


def _git_dirty(root: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "not a git repository"
    if completed.returncode != 0:
        return False, "not a git repository"
    changed = [line for line in completed.stdout.splitlines() if line.strip()]
    return bool(changed), f"{len(changed)} uncommitted change(s)" if changed else ""


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


# ---------------------------------------------------------------------------
# The package
# ---------------------------------------------------------------------------


def fab_package(
    conn: sqlite3.Connection,
    store: AssetStore,
    config: Config,
    project: KiCadProject,
    *,
    output_dir: Path | None = None,
    profile: FabProfile = JLCPCB,
    variant: Variant | None = None,
    assembly: bool = True,
    check_only: bool = False,
    allow_dirty: bool = False,
    cli: KiCadCli | None = None,
    timestamp: bool = True,
) -> FabReport:
    """Run the pipeline. Writes nothing unless preflight passes."""
    cli = cli or KiCadCli()
    suffix = f"-{variant.name}" if variant else ""
    target = output_dir or (project.root / "fab" / f"{project.name}{suffix}")

    bom = extract_bom(conn, project, variant=variant)
    # Staged beside the project rather than inside `fab/`, so a run that stops
    # at preflight leaves no directory behind that looks like a package.
    work = project.root / ".klm-fab"
    _remove(work)
    work.mkdir(parents=True, exist_ok=True)

    try:
        checks = preflight(
            conn,
            store,
            config,
            project,
            bom=bom,
            cli=cli,
            work_dir=work,
            assembly=assembly,
            allow_dirty=allow_dirty,
        )
        report = FabReport(
            project=project, output_dir=target, profile=profile, preflight=checks, bom=bom
        )
        if check_only or checks.blocked or project.board is None:
            return report

        _build(conn, cli, project, bom, profile, work, report, assembly=assembly)
        _write_manifest(work, project, report, variant, cli, timestamp=timestamp)
        _write_readme(work, project, report, variant)

        _remove(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work), str(target))
        report.output_dir = target
        report.written = True
        return report
    finally:
        _remove(work)


def _build(
    conn: sqlite3.Connection,
    cli: KiCadCli,
    project: KiCadProject,
    bom: BomReport,
    profile: FabProfile,
    work: Path,
    report: FabReport,
    *,
    assembly: bool,
) -> None:
    assert project.board is not None
    gerber_dir = work / "gerbers"
    cli.export_gerbers(
        project.board, gerber_dir, protel_extensions=profile.gerber_protel_extensions
    )
    cli.export_drill(project.board, work / "drill", merge_pth_npth=profile.drill_merge_pth_npth)
    _zip_directory(gerber_dir, work / "gerbers.zip")
    shutil.rmtree(gerber_dir, ignore_errors=True)

    if not assembly:
        return

    pos_file = work / "pos-raw.csv"
    cli.export_pos(project.board, pos_file)
    raw = parse_pos_csv(pos_file.read_text(encoding="utf-8")) if pos_file.exists() else []
    pos_file.unlink(missing_ok=True)

    populated = {reference for line in bom.lines for reference in line.references}
    placed = [p for p in raw if p.reference in populated]
    report.placements = apply_corrections(conn, placed, bom, profile)
    report.unconfirmed = sorted(
        p.reference for p in report.placements if not p.confirmed
    )

    (work / "cpl.csv").write_text(
        render_csv(profile.cpl_columns, [profile.cpl_row(p) for p in report.placements]),
        encoding="utf-8",
        newline="",
    )
    (work / "bom.csv").write_text(
        render_csv(profile.bom_columns, [profile.bom_row(line) for line in bom.assembly_lines()]),
        encoding="utf-8",
        newline="",
    )


def _zip_directory(source: Path, target: Path) -> None:
    """Zip deterministically: sorted names, a fixed timestamp, no directories.

    A zip whose bytes change on every run makes artifact diffing useless and
    every fab package look modified (docs/06 §5 applies here too).
    """
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(
                path.relative_to(source).as_posix(), date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def _write_manifest(
    work: Path,
    project: KiCadProject,
    report: FabReport,
    variant: Variant | None,
    cli: KiCadCli,
    *,
    timestamp: bool,
) -> None:
    """Everything needed to answer "why did this board come back wrong?"."""
    bom = report.bom
    payload: dict[str, object] = {
        "klm_version": __version__,
        "kicad_cli": cli.version(),
        "project": project.name,
        "profile": report.profile.name,
        "variant": variant.name if variant else None,
        "source_commit": _git_commit(project.root),
        "parts": bom.total_parts if bom else 0,
        "lines": len(bom.lines) if bom else 0,
        "excluded": [line.designators for line in (bom.excluded if bom else [])],
        # Every placement, not only the corrected ones: `klm fab feedback` maps
        # a reference back to a part from here, and by the time a board returns
        # the schematic may have moved on.
        "placements": [
            {
                "reference": p.reference,
                "klm_id": p.klm_id,
                "footprint": p.footprint,
                "rotation": round(p.rotation, 4),
                "source": p.correction_source,
                "confirmed": p.confirmed,
            }
            for p in sorted(report.placements, key=lambda p: p.reference)
        ],
        "unconfirmed": report.unconfirmed,
        "preflight": [
            {"check": c.name, "status": c.status, "detail": c.detail}
            for c in report.preflight.checks
        ],
    }
    if timestamp:
        payload["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (work / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_readme(
    work: Path, project: KiCadProject, report: FabReport, variant: Variant | None
) -> None:
    bom = report.bom
    lines = [
        f"{project.name} — fabrication package",
        f"profile: {report.profile.name}",
        f"variant: {variant.name if variant else '(none)'}",
        "",
        f"{len(bom.lines) if bom else 0} BOM lines, {bom.total_parts if bom else 0} parts placed",
    ]
    if bom and bom.excluded:
        lines += ["", "Not populated (DNP or excluded by the variant):"]
        lines += [f"  {line.designators}  {line.value}" for line in bom.excluded]
    if report.unconfirmed:
        # ADR-0011: klm bundles no rotation data, so a package's first run of a
        # package has nothing behind it. Saying so is the only mitigation there is.
        lines += [
            "",
            "Rotation NOT confirmed against a physical board — check the fab's DFM preview:",
            f"  {', '.join(report.unconfirmed)}",
            "",
            "After the board arrives:  klm fab feedback <this dir> --wrong U3:180 --confirm-rest",
        ]
    (work / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Learning from a board that came back
# ---------------------------------------------------------------------------


@dataclass
class FeedbackReport:
    corrected: list[tuple[str, str, float]] = field(default_factory=list)
    """``(reference, mpn, rotation applied)``."""
    confirmed: list[tuple[str, str]] = field(default_factory=list)
    generalized: list[tuple[str, float]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    """References named on the command line that this package does not contain."""


def fab_feedback(
    conn: sqlite3.Connection,
    package_dir: Path,
    *,
    wrong: dict[str, float],
    confirm_rest: bool = False,
    generalize: bool = False,
) -> FeedbackReport:
    """Record what a physical board showed, against the package that made it.

    This is the feature that actually solves the rotation problem, and it works
    because the manifest says exactly what was sent — including which references
    had no confirmed correction behind them.

    The *confirming* half is the more valuable one. Turning "untested" into
    "this part came back placed correctly" is knowledge nothing else records,
    and it is most of the board.
    """
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no manifest.json in {package_dir}; is that a klm fab package?")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    placed = {
        str(entry["reference"]): entry
        for entry in manifest.get("placements", [])
        if isinstance(entry, dict) and entry.get("reference")
    }
    if not placed:
        raise ValueError(
            f"{manifest_path} records no placements — it was written by an older klm, "
            "or the package was built with --no-assembly"
        )
    parts = {ref: str(entry.get("klm_id") or "") for ref, entry in placed.items()}

    report = FeedbackReport()
    for reference, degrees in sorted(wrong.items()):
        klm_id = parts.get(reference) or None
        if klm_id is None:
            report.unknown.append(reference)
            continue
        corrections.learn_part(
            conn,
            klm_id,
            degrees,
            note=f"{reference} on {manifest.get('project', package_dir.name)}",
        )
        report.corrected.append((reference, _mpn(conn, klm_id), degrees))
        if generalize:
            pattern = _pattern_for(placed.get(reference, {}).get("footprint", ""))
            if pattern:
                corrections.set_pattern(
                    conn, pattern, degrees, source=corrections.CorrectionSource.LEARNED
                )
                report.generalized.append((pattern, degrees))

    if confirm_rest:
        for reference in sorted(set(placed) - set(wrong)):
            klm_id = parts.get(reference) or None
            if klm_id is None:
                continue
            corrections.confirm_part(
                conn, klm_id, note=f"confirmed on {manifest.get('project', package_dir.name)}"
            )
            report.confirmed.append((reference, _mpn(conn, klm_id)))

    return report


def _mpn(conn: sqlite3.Connection, klm_id: str) -> str:
    row = conn.execute("SELECT mpn FROM part WHERE klm_id = ?", (klm_id,)).fetchone()
    return str(row["mpn"]) if row else klm_id


def _pattern_for(footprint: str) -> str:
    name = (footprint or "").partition(":")[2] or footprint
    return f"^{re.escape(name)}$" if name else ""
