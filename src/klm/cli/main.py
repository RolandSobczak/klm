"""``klm`` entry point.

Exit codes are uniform across every command (docs/12 §2):

* ``0`` success
* ``1`` the checked condition failed
* ``2`` klm itself errored
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from klm import __version__
from klm.config import load_config
from klm.environment import find_kicad_config, probe_all
from klm.hooks import HOOK_BLOCK, HOOK_ID, PRE_COMMIT_CONFIG, PRE_COMMIT_TEMPLATE
from klm.model import PartStatus
from klm.serial.part_file import to_yaml
from klm.services.catalog import list_parts
from klm.services.exporter import PART_FILE, export_catalog, import_catalog
from klm.services.generate import generate
from klm.services.importer import import_symbol_library
from klm.services.lint import RULES, LintReport, Selector, Severity, lint_catalog
from klm.services.register import apply_plan, plan_registration
from klm.store import AssetKind, AssetStore, Paths, connect, migrate
from klm.store.db import SCHEMA_VERSION, user_version

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_ERROR = 2

_OK = "✓"
_FAIL = "✗"
_WARN = "⚠"
_INFO = "ℹ"  # noqa: RUF001 - deliberate UI glyph


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="klm",
        description="KiCad Library Manager — parts, libraries, sourcing and fabrication.",
    )
    parser.add_argument("--version", action="version", version=f"klm {__version__}")
    parser.add_argument(
        "--catalog",
        metavar="DIR",
        help="Catalog directory (overrides $KLM_HOME for this invocation).",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = sub.add_parser("init", help="Create the catalog and its directory layout.")
    init.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if the directory already holds a catalog.",
    )
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="Check tools, configuration and catalog health.")
    doctor.add_argument(
        "--deep",
        action="store_true",
        help="Also re-hash every stored asset to detect on-disk corruption.",
    )
    doctor.set_defaults(func=cmd_doctor)

    export = sub.add_parser("export", help="Write the catalog to its git-versioned mirror.")
    export.add_argument(
        "--prune",
        action="store_true",
        help="Delete exported directories for parts no longer in the database.",
    )
    export.add_argument(
        "--check",
        action="store_true",
        help="Report what would change without writing; non-zero if anything would.",
    )
    export.set_defaults(func=cmd_export)

    importer = sub.add_parser("import", help="Rebuild the database from the mirror.")
    importer.add_argument(
        "--strict",
        action="store_true",
        help="Stop at the first malformed file instead of collecting errors.",
    )
    importer.add_argument(
        "--from-kicad",
        metavar="FILE",
        help="Import symbols from an existing .kicad_sym library instead.",
    )
    importer.add_argument(
        "--status",
        choices=[str(s) for s in PartStatus],
        default=str(PartStatus.DRAFT),
        help="Status for imported parts (default: draft).",
    )
    importer.add_argument(
        "--category",
        metavar="PATH",
        help="Category to file imported parts under, e.g. Passive/Resistor.",
    )
    importer.set_defaults(func=cmd_import)

    lint = sub.add_parser("lint", help="Check the catalog against the field schema.")
    lint.add_argument(
        "--select",
        metavar="RULES",
        help="Only these rules or groups, comma-separated (e.g. S,V001).",
    )
    lint.add_argument("--ignore", metavar="RULES", help="Skip these rules or groups.")
    lint.add_argument("--fix", action="store_true", help="Apply the mechanical fixes.")
    lint.add_argument(
        "--dry-run",
        action="store_true",
        help="With --fix, report what would change without writing.",
    )
    lint.add_argument(
        "--format", choices=("text", "json"), default="text", help="Output format."
    )
    lint.add_argument(
        "--max-severity",
        choices=("error", "warning"),
        help="Lowest severity that fails the command (default: error).",
    )
    lint.add_argument("--rules", action="store_true", help="List every rule and exit.")
    lint.set_defaults(func=cmd_lint)

    hook = sub.add_parser("hook", help="Install klm's pre-commit hook in a repository.")
    hook.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Repository to install into (default: the current directory).",
    )
    hook.add_argument(
        "--check",
        action="store_true",
        help="Report whether the hook is configured; non-zero if it is not.",
    )
    hook.set_defaults(func=cmd_hook)

    gen = sub.add_parser("generate", help="Rebuild the KiCad libraries from the catalog.")
    gen.set_defaults(func=cmd_generate)

    reg = sub.add_parser("register", help="Register klm's libraries with KiCad.")
    reg.add_argument(
        "--check",
        action="store_true",
        help="Report what is missing without writing; non-zero if anything is.",
    )
    reg.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the exact changes that would be made, then stop.",
    )
    reg.add_argument(
        "--kicad-config",
        metavar="DIR",
        help="KiCad configuration directory (default: the newest one found).",
    )
    reg.set_defaults(func=cmd_register)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        print(f"klm: {exc}", file=sys.stderr)
        return EXIT_ERROR


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)

    if paths.exists() and not args.force:
        print(f"{_INFO} catalog already exists at {paths.home}")
        print("  nothing to do (use --force to re-run migrations)")
        return EXIT_OK

    paths.create()
    conn = connect(paths.db)
    try:
        applied = migrate(conn, backup_path=paths.db)
    finally:
        conn.close()

    print(f"{_OK} catalog created at {paths.home}")
    for directory in paths.all_dirs()[1:]:
        print(f"    {directory.relative_to(paths.home)}/")
    if applied:
        names = ", ".join(f"{m.version}:{m.name}" for m in applied)
        print(f"{_OK} schema at version {SCHEMA_VERSION} ({names})")
    else:
        print(f"{_OK} schema already at version {SCHEMA_VERSION}")

    print()
    print("Next: klm doctor")
    return EXIT_OK


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    problems = 0

    print(f"catalog home     {paths.home}")
    print()

    # External tools. Absence is a degradation, never a failure.
    for tool in probe_all():
        if tool.found:
            tool_version = tool.version or "unknown"
            print(f"{_OK} {tool.name:<14} {tool_version:<9} {tool.path}")
        else:
            print(f"{_FAIL} {tool.name:<14} not found")
            print(f"    → {tool.consequence}")
            print(f"    → install: {tool.install_hint}")

    # KiCad user configuration.
    config_dir, kicad_version = find_kicad_config()
    if config_dir is None:
        print(f"{_WARN} KiCad config   not found")
        print("    → klm register cannot add its library tables yet")
    else:
        print(f"{_OK} KiCad config   {kicad_version or 'unversioned':<9} {config_dir}")

    # Anthropic credentials gate the agent only; everything else works without.
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(f"{_OK} Anthropic API  key present")
    else:
        print(f"{_WARN} Anthropic API  no key")
        print("    → agent features unavailable. Set ANTHROPIC_API_KEY.")

    print()

    # Catalog.
    if not paths.exists():
        print(f"{_FAIL} catalog        not initialised")
        print("    → run: klm init")
        return EXIT_CHECK_FAILED

    conn = connect(paths.db, create=False)
    try:
        version = user_version(conn)
        if version < SCHEMA_VERSION:
            print(f"{_WARN} schema         version {version}, expected {SCHEMA_VERSION}")
            print("    → run: klm init --force")
            problems += 1
        elif version > SCHEMA_VERSION:
            print(f"{_FAIL} schema         version {version} is newer than this klm understands")
            print("    → upgrade klm")
            problems += 1
        else:
            print(f"{_OK} schema         version {version}")

        problems += _report_catalog(conn)
    except sqlite3.DatabaseError as exc:
        print(f"{_FAIL} catalog        unreadable: {exc}")
        return EXIT_CHECK_FAILED
    finally:
        conn.close()

    problems += _report_assets(paths, deep=args.deep)

    print()
    if problems:
        print(f"{problems} issue{'s' if problems != 1 else ''} need attention.")
        return EXIT_CHECK_FAILED
    print("No issues found.")
    return EXIT_OK


def _report_catalog(conn: sqlite3.Connection) -> int:
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM part GROUP BY status").fetchall())
    total = sum(counts.values())
    if total == 0:
        print(f"{_INFO} parts          catalog is empty")
        return 0

    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    print(f"{_OK} parts          {total} ({summary})")

    problems = 0
    stale = conn.execute(
        "SELECT COUNT(DISTINCT klm_id) FROM offer "
        "WHERE fetched_at < datetime('now', '-30 days')"
    ).fetchone()[0]
    if stale:
        print(f"{_WARN} offers         {stale} parts have offers older than 30 days")
        print("    → run: klm refresh --stale 30d")
        problems += 1

    unbuyable = conn.execute(
        "SELECT COUNT(*) FROM part p WHERE p.status = 'approved' "
        "AND NOT EXISTS (SELECT 1 FROM offer o WHERE o.klm_id = p.klm_id)"
    ).fetchone()[0]
    if unbuyable:
        print(f"{_WARN} sourcing       {unbuyable} approved parts have no supplier offer")
        problems += 1

    return problems


def _report_assets(paths: Paths, *, deep: bool) -> int:
    store = AssetStore(paths.assets)
    counts = {kind: len(store.list_hashes(kind)) for kind in AssetKind}
    total = sum(counts.values())
    if total == 0:
        print(f"{_INFO} assets         none stored")
        return 0

    detail = ", ".join(f"{n} {kind.value}" for kind, n in counts.items() if n)
    size_mb = store.total_bytes() / (1024 * 1024)
    print(f"{_OK} assets         {total} ({detail}), {size_mb:.1f} MB")

    if not deep:
        return 0

    corrupt = [
        (kind, h)
        for kind in AssetKind
        for h in store.list_hashes(kind)
        if not store.verify(h, kind)
    ]
    if corrupt:
        print(f"{_FAIL} integrity      {len(corrupt)} assets do not match their content hash")
        for kind, content_hash in corrupt[:5]:
            print(f"    {kind.value}: {content_hash}")
        return 1

    print(f"{_OK} integrity      all {total} assets verified")
    return 0


# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------


def _require_catalog(paths: Paths) -> None:
    if not paths.exists():
        raise FileNotFoundError(f"no catalog at {paths.home} — run: klm init")


def cmd_export(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)

    conn = connect(paths.db, create=False)
    try:
        if args.check:
            return _export_check(conn, paths)
        result = export_catalog(conn, paths.catalog, prune=args.prune)
    finally:
        conn.close()

    if not result.written and not result.removed:
        print(f"{_OK} {result.total} parts already up to date in {paths.catalog}")
        return EXIT_OK

    for klm_id in result.written:
        print(f"  wrote   {klm_id}")
    for klm_id in result.removed:
        print(f"  removed {klm_id}")
    print(
        f"{_OK} exported {result.total} parts "
        f"({len(result.written)} changed, {len(result.unchanged)} unchanged)"
    )
    return EXIT_OK


def _export_check(conn: sqlite3.Connection, paths: Paths) -> int:
    """Report drift without writing. Used in a pre-commit hook and in CI."""
    stale: list[str] = []
    for part in list_parts(conn):
        target = paths.part_dir(part.klm_id) / PART_FILE
        content = to_yaml(part)
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            stale.append(part.klm_id)

    if not stale:
        print(f"{_OK} export is up to date")
        return EXIT_OK

    for klm_id in stale:
        print(f"  stale   {klm_id}")
    print(f"{_FAIL} {len(stale)} parts differ from the mirror — run: klm export")
    return EXIT_CHECK_FAILED


def cmd_import(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)

    if args.from_kicad:
        return _import_from_kicad(paths, args)

    conn = connect(paths.db, create=False)
    try:
        result = import_catalog(conn, paths.catalog, strict=args.strict)
    finally:
        conn.close()

    print(
        f"{_OK} imported: {len(result.created)} created, "
        f"{len(result.updated)} updated, {len(result.unchanged)} unchanged"
    )
    if result.errors:
        print()
        for path, message in result.errors:
            print(f"{_FAIL} {path}")
            print(f"    {message}")
        print(f"\n{len(result.errors)} file(s) could not be imported.")
        return EXIT_CHECK_FAILED
    return EXIT_OK


def _import_from_kicad(paths: Paths, args: argparse.Namespace) -> int:
    """`klm import --from-kicad` — the on-ramp for an existing library."""
    source = Path(args.from_kicad).expanduser()
    if not source.exists():
        print(f"klm: no such file: {source}", file=sys.stderr)
        return EXIT_ERROR

    config = load_config(paths.config)
    store = AssetStore(paths.assets)
    conn = connect(paths.db, create=False)
    try:
        report = import_symbol_library(
            conn,
            store,
            source,
            config=config,
            status=PartStatus(args.status),
            category=args.category,
        )
    finally:
        conn.close()

    print(
        f"{_OK} imported {len(report.imported)} symbol(s) from {source.name}: "
        f"{report.created} created, {report.updated} updated"
    )
    for name, reason in report.skipped:
        print(f"{_WARN} skipped {name}: {reason}")
    print()
    print("Next: klm lint --select S002,V001 --fix --dry-run")
    return EXIT_OK if report.ok else EXIT_CHECK_FAILED


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def cmd_lint(args: argparse.Namespace) -> int:
    if args.rules:
        for rule in RULES.values():
            mark = "fixable" if rule.fixable else ""
            print(f"{rule.id}  {rule.severity:<8}{mark:<9}{rule.summary}")
        return EXIT_OK

    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    config = load_config(paths.config)

    selector = Selector(
        select=_rule_list(args.select) or config.lint.select,
        ignore=_rule_list(args.ignore) or config.lint.ignore,
    )
    store = AssetStore(paths.assets)
    conn = connect(paths.db, create=False)
    try:
        report = lint_catalog(
            conn, store, config, selector=selector, fix=args.fix, dry_run=args.dry_run
        )
    finally:
        conn.close()

    max_severity = args.max_severity or config.lint.max_severity
    if args.format == "json":
        print(_lint_json(report))
    else:
        _print_lint(report, fix=args.fix, dry_run=args.dry_run)
    return EXIT_CHECK_FAILED if report.failed(max_severity) else EXIT_OK


def _rule_list(raw: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


def _print_lint(report: LintReport, *, fix: bool, dry_run: bool) -> None:
    for finding in report.findings:
        print(finding)

    errors = report.count(Severity.ERROR)
    warnings = report.count(Severity.WARNING)
    fixed = len(report.fixed)
    print()
    if fix and fixed:
        verb = "would fix" if dry_run else "fixed"
        print(f"{_OK} {verb} {fixed} finding(s)")
    if not errors and not warnings:
        print(f"{_OK} {report.parts_checked} part(s) checked, nothing to report")
        return
    print(
        f"{_FAIL if errors else _WARN} {report.parts_checked} part(s) checked: "
        f"{errors} error(s), {warnings} warning(s)"
    )
    if any(f.fixable for f in report.findings if not f.fixed) and not fix:
        print("    → some findings are fixable: klm lint --fix --dry-run")


def _lint_json(report: LintReport) -> str:
    payload = {
        "parts_checked": report.parts_checked,
        "errors": report.count(Severity.ERROR),
        "warnings": report.count(Severity.WARNING),
        "findings": [
            {
                "rule": f.rule,
                "severity": str(f.severity),
                "location": f.location,
                "message": f.message,
                "fixable": f.fixable,
                "fixed": f.fixed,
            }
            for f in report.findings
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# hook
# ---------------------------------------------------------------------------


def cmd_hook(args: argparse.Namespace) -> int:
    directory = Path(args.directory).expanduser().resolve()
    target = directory / PRE_COMMIT_CONFIG

    if args.check:
        if target.exists() and HOOK_ID in target.read_text(encoding="utf-8"):
            print(f"{_OK} {target} runs klm lint")
            return EXIT_OK
        print(f"{_FAIL} {target} does not run klm lint")
        return EXIT_CHECK_FAILED

    if not target.exists():
        target.write_text(PRE_COMMIT_TEMPLATE, encoding="utf-8")
        print(f"{_OK} wrote {target}")
    elif HOOK_ID in target.read_text(encoding="utf-8"):
        print(f"{_OK} {target} already runs klm lint")
        return EXIT_OK
    else:
        # Merging into someone's existing hook config means parsing YAML klm
        # did not write. Printing the block is honest and costs one paste.
        print(f"{_WARN} {target} exists; add this to its `repos:` list:")
        print()
        print(HOOK_BLOCK)
        return EXIT_CHECK_FAILED

    print()
    print("Next: pre-commit install")
    return EXIT_OK


# ---------------------------------------------------------------------------
# generate / register
# ---------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)

    conn = connect(paths.db, create=False)
    try:
        result = generate(conn, paths)
    finally:
        conn.close()

    state = "rebuilt" if result.changed else "already up to date"
    print(
        f"{_OK} {state}: {result.symbols} symbols, "
        f"{result.footprints} footprints, {result.models} 3D models"
    )
    print(f"    {paths.generated}")

    if result.skipped:
        print()
        for klm_id, reason in result.skipped:
            print(f"{_WARN} skipped {klm_id}: {reason}")
        print(f"\n{len(result.skipped)} part(s) could not be generated.")
        return EXIT_CHECK_FAILED
    return EXIT_OK


def cmd_register(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)

    if args.kicad_config:
        config_dir = Path(args.kicad_config).expanduser()
    else:
        found, _version = find_kicad_config()
        if found is None:
            print(f"{_FAIL} no KiCad configuration directory found")
            print("    → is KiCad installed? Otherwise pass --kicad-config DIR")
            return EXIT_CHECK_FAILED
        config_dir = found

    plan = (
        plan_registration(paths, config_dir)
        if (args.check or args.dry_run)
        else apply_plan(paths, config_dir)
    )

    print(f"KiCad config     {plan.kicad_config}")
    for item in plan.already_correct:
        print(f"{_OK} {item}")

    if not plan.needed:
        print(f"\n{_OK} klm is registered with KiCad")
        return EXIT_OK

    print()
    for change in plan.changes:
        verb = "would" if (args.check or args.dry_run) else "did"
        print(f"  {verb}: {change.description}")
        print(f"         in {change.target}")

    if args.check or args.dry_run:
        print(f"\n{_FAIL} {len(plan.changes)} change(s) needed — run: klm register")
        return EXIT_CHECK_FAILED

    print(f"\n{_OK} registered ({len(plan.changes)} change(s)); originals kept as *.klm-bak")
    print(f"{_INFO} restart KiCad for the new libraries to appear")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
