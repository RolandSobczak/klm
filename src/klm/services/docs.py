"""``klm docs`` and ``klm report`` — the artifacts people actually look at.

A schematic PDF and a board render are what a collaborator opens; the fab
package is what a machine consumes. Producing them in CI on every merge means
the current state of the board is always one click away, which is most of the
value of having CI at all.

``klm report --format github-summary`` writes a Markdown table into the Actions
run summary. That summary is visible without downloading anything, and being
visible is the point — an artifact nobody unzips reports nothing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from klm.cad.kicadcli import KiCadCli, KiCadCliError, KiCadCliUnavailable
from klm.kicad.project import KiCadProject
from klm.services.bom import BomReport, Variant, extract_bom

__all__ = ["DocsReport", "build_docs", "github_summary", "markdown_report"]


@dataclass
class DocsReport:
    output_dir: Path
    written: list[Path] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(artifact, reason)`` — absence is reported, never silently passed over."""


def build_docs(
    project: KiCadProject,
    output_dir: Path,
    *,
    pdf: bool = True,
    render: bool = True,
    step: bool = False,
    cli: KiCadCli | None = None,
) -> DocsReport:
    """Produce the human-facing artifacts, skipping what cannot be produced.

    One failing artifact must not take the others down: a board render is the
    path most likely to need an X server, and losing the schematic PDF because
    of it would be a poor trade (docs/14 Q11).
    """
    cli = cli or KiCadCli()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = DocsReport(output_dir=output_dir)

    if pdf:
        sheet = project.schematics[0] if project.schematics else None
        if sheet is None:
            report.skipped.append(("schematic.pdf", "the project has no schematic"))
        else:
            _attempt(
                report,
                "schematic.pdf",
                lambda target: cli.export_schematic_pdf(sheet, target),
                output_dir / "schematic.pdf",
            )

    if project.board is None:
        for name in (("board-top.png", "board-bottom.png") if render else ()) + (
            ("board.step",) if step else ()
        ):
            report.skipped.append((name, "the project has no board"))
        return report

    board = project.board
    if render:
        for side in ("top", "bottom"):
            _attempt(
                report,
                f"board-{side}.png",
                lambda target, side=side: cli.render_board(board, target, side=side),
                output_dir / f"board-{side}.png",
            )
    if step:
        _attempt(
            report,
            "board.step",
            lambda target: cli.export_step(board, target),
            output_dir / "board.step",
        )
    return report


def _attempt(report: DocsReport, name: str, action, target: Path) -> None:  # type: ignore[no-untyped-def]
    try:
        action(target)
    except (KiCadCliUnavailable, KiCadCliError) as exc:
        report.skipped.append((name, str(exc)))
        return
    if target.exists():
        report.written.append(target)
    else:  # pragma: no cover - kicad-cli returned 0 without writing
        report.skipped.append((name, "kicad-cli reported success but wrote nothing"))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _rows(project: KiCadProject, bom: BomReport, package: Path | None) -> list[tuple[str, str]]:
    rows = [
        ("Project", project.name),
        ("Variant", bom.variant or "(default)"),
        ("BOM lines", str(len(bom.lines))),
        ("Parts placed", str(bom.total_parts)),
        ("Not populated", str(sum(line.quantity for line in bom.excluded))),
    ]
    if bom.unresolved:
        rows.append(("Unresolved", f"{len(bom.unresolved)} — {', '.join(bom.unresolved[:6])}"))

    manifest = (package / "manifest.json") if package else None
    if manifest is not None and manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        checks = data.get("preflight") or []
        failed = [c["check"] for c in checks if c.get("status") == "fail"]
        rows.append(("Preflight", "passed" if not failed else f"failed: {', '.join(failed)}"))
        unconfirmed = data.get("unconfirmed") or []
        if unconfirmed:
            # ADR-0011: this is the number worth surfacing where someone sees it
            # without downloading anything.
            rows.append(
                ("Rotation unconfirmed", f"{len(unconfirmed)} — {', '.join(unconfirmed[:8])}")
            )
        if data.get("source_commit"):
            rows.append(("Commit", str(data["source_commit"])[:12]))
    return rows


def markdown_report(
    conn: sqlite3.Connection | None,
    project: KiCadProject,
    *,
    variant: Variant | None = None,
    package: Path | None = None,
) -> str:
    bom = extract_bom(conn, project, variant=variant)
    lines = [f"## {project.name}", "", "| | |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in _rows(project, bom, package)]

    if bom.lines:
        lines += [
            "",
            "<details><summary>Bill of materials</summary>",
            "",
            "| Qty | Value | Footprint | Designators |",
            "|---:|---|---|---|",
        ]
        lines += [
            f"| {line.quantity} | {line.value} | {line.footprint_name} | {line.designators} |"
            for line in bom.lines
        ]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def github_summary(
    conn: sqlite3.Connection | None,
    project: KiCadProject,
    *,
    variant: Variant | None = None,
    package: Path | None = None,
) -> str:
    """The same table. GitHub renders plain Markdown in a step summary."""
    return markdown_report(conn, project, variant=variant, package=package)


def json_report(
    conn: sqlite3.Connection | None,
    project: KiCadProject,
    *,
    variant: Variant | None = None,
    package: Path | None = None,
) -> str:
    bom = extract_bom(conn, project, variant=variant)
    return json.dumps(dict(_rows(project, bom, package)), indent=2, ensure_ascii=False)
