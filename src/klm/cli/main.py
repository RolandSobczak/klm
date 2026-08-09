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
from collections.abc import Callable, Sequence
from pathlib import Path

from klm import __version__
from klm.assets.qa import QaReport, QaStatus, check_model3d
from klm.cad.freecad import FreeCadUnavailable, convert_mesh
from klm.config import Config, load_config
from klm.environment import find_kicad_config, probe_all
from klm.hooks import HOOK_BLOCK, HOOK_ID, PRE_COMMIT_CONFIG, PRE_COMMIT_TEMPLATE
from klm.kicad.project import LOCK_FILE, KiCadProject, find_project
from klm.model import Confidence, Offer, Part, PartStatus, PriceBreak
from klm.serial.part_file import to_yaml
from klm.services.assets import (
    acquire_assets,
    register_asset,
    reuse_candidates,
    run_qa,
)
from klm.services.catalog import get_part, list_parts, save_part
from klm.services.exporter import PART_FILE, export_catalog, import_catalog
from klm.services.generate import generate
from klm.services.importer import import_symbol_library
from klm.services.lint import RULES, LintReport, Selector, Severity, lint_catalog
from klm.services.offers import (
    TIMESTAMP_FORMAT,
    delete_offer,
    list_offers,
    refresh_offers,
    save_offer,
    stale_part_ids,
)
from klm.services.part_add import add_part
from klm.services.register import apply_plan, plan_registration
from klm.services.sync import (
    SyncReport,
    SyncRow,
    SyncState,
    SyncStatus,
    adopt,
    promote,
    pull,
    push,
    sync_status,
)
from klm.services.vendor import VendorError, VendorPlan, plan_vendor, unvendor, vendor
from klm.store import AssetKind, AssetStore, Paths, connect, migrate
from klm.store.db import SCHEMA_VERSION, user_version
from klm.suppliers.lcsc import is_lcsc_pn, product_url
from klm.suppliers.registry import build_adapters

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

    refresh = sub.add_parser("refresh", help="Re-read stock and prices from the suppliers.")
    refresh.add_argument(
        "--stale",
        metavar="AGE",
        help="Only parts whose offers are older than this, e.g. 7d, 24h.",
    )
    refresh.add_argument("--part", metavar="ID_OR_MPN", help="Only this part.")
    refresh.add_argument("--supplier", metavar="NAME", help="Only this supplier.")
    refresh.add_argument(
        "--no-discover",
        action="store_true",
        help="Refresh existing links only; do not match new offers.",
    )
    refresh.add_argument(
        "--offline",
        action="store_true",
        help="Serve from the HTTP cache only, never reaching the network.",
    )
    refresh.set_defaults(func=cmd_refresh)

    offers = sub.add_parser("offers", help="Show, add or remove supplier offers.")
    offers.add_argument("part", nargs="?", metavar="ID_OR_MPN", help="Limit to one part.")
    offers.add_argument("--supplier", metavar="NAME", help="Limit to one supplier.")
    offers.add_argument(
        "--format", choices=("text", "json"), default="text", help="Output format."
    )
    offers.add_argument("--qty", type=int, default=1, help="Quantity to price at (default: 1).")
    offers.add_argument(
        "--add",
        metavar="SUPPLIER_PN",
        help="Record an offer by hand — the supported path for LCSC (docs/adr/0009).",
    )
    offers.add_argument("--remove", metavar="SUPPLIER_PN", help="Delete an offer.")
    offers.add_argument("--price", type=float, help="With --add: unit price.")
    offers.add_argument("--stock", type=int, help="With --add: units in stock.")
    offers.add_argument("--moq", type=int, default=1, help="With --add: minimum order quantity.")
    offers.add_argument("--currency", help="With --add: currency (default: the supplier's).")
    offers.set_defaults(func=cmd_offers)

    _add_part_parser(sub)
    _add_assets_parser(sub)

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

    _add_sync_parsers(sub)

    return parser


def _project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        metavar="DIR",
        default=".",
        help="The KiCad project to act on (default: the current directory).",
    )


def _add_sync_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    vendor_cmd = sub.add_parser("vendor", help="Copy the parts a project uses into the project.")
    _project_argument(vendor_cmd)
    vendor_cmd.add_argument("--name", metavar="NAME", help="Library name (default: the project's).")
    vendor_cmd.add_argument(
        "--with-3d", action="store_true", help="Include STEP models in the project."
    )
    vendor_cmd.add_argument(
        "--from-library",
        metavar="NICKNAME",
        action="append",
        default=[],
        dest="from_libraries",
        help="Also resolve this library's symbols by name — for projects predating klm.",
    )
    vendor_cmd.add_argument(
        "--dry-run", action="store_true", help="Print the plan and stop, changing nothing."
    )
    vendor_cmd.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="Vendor what resolves and leave the rest linked to their own libraries.",
    )
    vendor_cmd.add_argument(
        "--strict",
        action="store_true",
        help="Also refuse when any symbol comes from a library klm does not manage.",
    )
    vendor_cmd.add_argument(
        "--no-timestamp",
        action="store_true",
        help="Omit vendored_at from the lock file, for reproducible-build workflows.",
    )
    vendor_cmd.set_defaults(func=cmd_vendor)

    unvendor_cmd = sub.add_parser("unvendor", help="Point a project back at the global libraries.")
    _project_argument(unvendor_cmd)
    unvendor_cmd.add_argument("--dry-run", action="store_true", help="Print the plan and stop.")
    unvendor_cmd.add_argument(
        "--force",
        action="store_true",
        help="Discard local edits to the vendored copies rather than refusing.",
    )
    unvendor_cmd.set_defaults(func=cmd_unvendor)

    sync = sub.add_parser("sync", help="Compare a vendored project against the catalog.")
    actions = sync.add_subparsers(dest="action", metavar="ACTION", required=True)

    status = actions.add_parser("status", help="Report drift. Changes nothing.")
    _project_argument(status)
    status.add_argument(
        "--exit-code", action="store_true", help="Exit non-zero if anything needs attention."
    )
    status.add_argument("--format", choices=("text", "json"), default="text")
    status.set_defaults(func=cmd_sync_status)

    pull_cmd = actions.add_parser("pull", help="Bring catalog changes into the project.")
    _project_argument(pull_cmd)
    pull_cmd.add_argument("parts", nargs="*", metavar="PART", help="Limit to these MPNs or ids.")
    pull_cmd.add_argument("--strategy", choices=("prefer-global",), help="How to treat conflicts.")
    pull_cmd.add_argument("--dry-run", action="store_true")
    pull_cmd.set_defaults(func=cmd_sync_pull)

    push_cmd = actions.add_parser("push", help="Move a project's edits into the catalog.")
    _project_argument(push_cmd)
    push_cmd.add_argument("parts", nargs="*", metavar="PART", help="Limit to these MPNs or ids.")
    push_cmd.add_argument("--strategy", choices=("prefer-project",), help="How to treat conflicts.")
    push_cmd.add_argument("--dry-run", action="store_true")
    push_cmd.set_defaults(func=cmd_sync_push)

    resolve = actions.add_parser("resolve", help="Decide conflicts, one part at a time.")
    _project_argument(resolve)
    resolve.add_argument(
        "--strategy",
        choices=("prefer-global", "prefer-project"),
        help="Apply one decision to every conflict instead of asking.",
    )
    resolve.set_defaults(func=cmd_sync_resolve)

    adopt_cmd = actions.add_parser("adopt", help="Rebuild a lost lock file from what is on disk.")
    _project_argument(adopt_cmd)
    adopt_cmd.add_argument("--name", metavar="NAME", help="Vendored library name.")
    adopt_cmd.add_argument("--dry-run", action="store_true")
    adopt_cmd.set_defaults(func=cmd_sync_adopt)

    promote_cmd = sub.add_parser("promote", help="Add a project-only part to the catalog.")
    promote_cmd.add_argument("reference", metavar="PART", help="Symbol name, MPN or KLM_ID.")
    _project_argument(promote_cmd)
    promote_cmd.add_argument("--category", metavar="PATH", help="Category for the new part.")
    promote_cmd.add_argument("--dry-run", action="store_true")
    promote_cmd.set_defaults(func=cmd_promote)


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
        "WHERE fetched_at < strftime(?, 'now', '-30 days')",
        (TIMESTAMP_FORMAT,),
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
# refresh / offers
# ---------------------------------------------------------------------------

_AGE_UNITS = {"d": 1.0, "h": 1.0 / 24.0, "w": 7.0}


def parse_age(raw: str) -> int:
    """`7d`, `24h`, `2w` → whole days, rounded up.

    Rounded up because the threshold means "at least this fresh": asking for
    `12h` and getting something 20 hours old would be a lie by rounding.
    """
    text = raw.strip().lower()
    unit = text[-1] if text and text[-1] in _AGE_UNITS else "d"
    number = text[:-1] if text and text[-1] in _AGE_UNITS else text
    try:
        value = float(number)
    except ValueError as exc:
        raise ValueError(f"cannot read {raw!r} as an age (try 7d, 24h, 2w)") from exc
    if value <= 0:
        raise ValueError(f"age must be positive, got {raw!r}")
    return max(1, -(-int(value * _AGE_UNITS[unit] * 1000) // 1000))


def _resolve_part(conn: sqlite3.Connection, reference: str) -> Part:
    """Find a part by KLM_ID or MPN. Ambiguity is reported, never resolved."""
    part = get_part(conn, reference)
    if part is not None:
        return part
    matches = [p for p in list_parts(conn) if p.mpn.lower() == reference.lower()]
    if not matches:
        raise LookupError(f"no part with id or MPN {reference!r}")
    if len(matches) > 1:
        ids = ", ".join(p.klm_id for p in matches)
        raise LookupError(f"{reference!r} matches several parts: {ids}")
    return matches[0]


def cmd_refresh(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    config = load_config(paths.config)

    adapters = build_adapters(
        config, paths.supplier_cache, offline=args.offline, only=args.supplier
    )
    if not adapters:
        which = f" named {args.supplier}" if args.supplier else ""
        print(f"{_WARN} no enabled supplier{which} — see [suppliers] in {paths.config}")
        return EXIT_CHECK_FAILED

    conn = connect(paths.db, create=False)
    try:
        parts = _refresh_targets(conn, config, args)
        report = refresh_offers(conn, adapters, parts, discover=not args.no_discover)
    finally:
        conn.close()

    for offer in report.discovered:
        price = offer.unit_price()
        detail = f"{price:.4f} {offer.currency}" if price is not None else "no price"
        print(f"  linked  {offer.supplier}:{offer.supplier_pn}  {detail}")
    for klm_id, offer, reason in report.proposed:
        print(f"{_WARN} {klm_id}: {offer.supplier}:{offer.supplier_pn} not linked — {reason}")

    print(
        f"{_OK} {report.parts_checked} part(s): {len(report.refreshed)} offer(s) refreshed, "
        f"{len(report.discovered)} newly linked"
    )
    if report.proposed:
        print(f"{_INFO} {len(report.proposed)} candidate(s) need a human: klm offers --add")

    for supplier, reason in report.unavailable:
        print(f"{_FAIL} {supplier}: {reason}")
    if report.unavailable:
        print(f"{_INFO} cached offers are unchanged; klm keeps what it had")
        return EXIT_CHECK_FAILED
    return EXIT_OK


def _refresh_targets(
    conn: sqlite3.Connection, config: Config, args: argparse.Namespace
) -> list[Part]:
    if args.part:
        return [_resolve_part(conn, args.part)]
    if args.stale:
        stale = set(stale_part_ids(conn, parse_age(args.stale)))
        return [p for p in list_parts(conn) if p.klm_id in stale]
    return [p for p in list_parts(conn) if p.status is not PartStatus.DEPRECATED]


def cmd_offers(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    config = load_config(paths.config)

    conn = connect(paths.db, create=False)
    try:
        if args.add:
            return _offers_add(conn, config, args)
        if args.remove:
            if not args.supplier:
                print("klm: --remove needs --supplier", file=sys.stderr)
                return EXIT_ERROR
            removed = delete_offer(conn, args.supplier, args.remove)
            print(f"{_OK if removed else _WARN} {'removed' if removed else 'no such offer:'} "
                  f"{args.supplier}:{args.remove}")
            return EXIT_OK if removed else EXIT_CHECK_FAILED

        klm_id = _resolve_part(conn, args.part).klm_id if args.part else None
        offers = list_offers(conn, klm_id=klm_id, supplier=args.supplier)
        parts = {p.klm_id: p for p in list_parts(conn)}
    finally:
        conn.close()

    if args.format == "json":
        print(_offers_json(offers, args.qty))
        return EXIT_OK
    return _print_offers(offers, parts, args.qty)


def _offers_add(conn: sqlite3.Connection, config: Config, args: argparse.Namespace) -> int:
    """`klm offers --add` — manual entry, first-class rather than a fallback.

    This is the supported path for LCSC and the escape hatch for every supplier
    klm has no adapter for. The confidence is `high` because a human made the
    link, which is a better signal than any string comparison klm can make.
    """
    if not args.part or not args.supplier:
        print("klm: --add needs a part and --supplier", file=sys.stderr)
        return EXIT_ERROR

    part = _resolve_part(conn, args.part)
    supplier = config.suppliers.get(args.supplier)
    offer = Offer(
        supplier=args.supplier,
        supplier_pn=args.add.strip(),
        klm_id=part.klm_id,
        mpn=part.mpn,
        manufacturer=part.manufacturer,
        description=part.description,
        moq=args.moq,
        stock=args.stock,
        currency=args.currency or (supplier.currency if supplier else None),
        price_breaks=[PriceBreak(args.moq, args.price)] if args.price is not None else [],
        url=product_url(args.add) if args.supplier == "lcsc" and is_lcsc_pn(args.add) else None,
        match_confidence=Confidence.HIGH,
    )
    save_offer(conn, offer)
    print(f"{_OK} {part.klm_id} ({part.mpn}) ← {offer.supplier}:{offer.supplier_pn}")
    return EXIT_OK


def _print_offers(offers: list[Offer], parts: dict[str, Part], qty: int) -> int:
    if not offers:
        print(f"{_INFO} no offers recorded")
        print("    → run: klm refresh, or record one: klm offers <part> --supplier lcsc --add C…")
        return EXIT_OK

    grouped: dict[str, list[Offer]] = {}
    for offer in offers:
        grouped.setdefault(offer.klm_id or "", []).append(offer)

    for klm_id, group in sorted(grouped.items()):
        part = parts.get(klm_id)
        label = f"{part.mpn} ({part.manufacturer})" if part else klm_id
        print(f"{label}  {klm_id}")
        for offer in group:
            price = offer.unit_price(qty)
            # A price of "—" and a price of 0 must not look alike; the first
            # means the supplier quoted nothing at this quantity.
            money = f"{price:.4f} {offer.currency or ''}".strip() if price is not None else "—"
            stock = "unknown" if offer.stock is None else str(offer.stock)
            flag = "  [low confidence]" if offer.match_confidence is Confidence.LOW else ""
            print(
                f"    {offer.supplier:<6} {offer.supplier_pn:<18} "
                f"{money:>14} @{qty:<6} stock {stock:>8}  {offer.fetched_at or 'never'}{flag}"
            )
        print()
    return EXIT_OK


def _offers_json(offers: list[Offer], qty: int) -> str:
    payload = [
        {
            "supplier": o.supplier,
            "supplier_pn": o.supplier_pn,
            "klm_id": o.klm_id,
            "mpn": o.mpn,
            "manufacturer": o.manufacturer,
            "packaging": str(o.packaging),
            "moq": o.moq,
            "stock": o.stock,
            "currency": o.currency,
            "unit_price": o.unit_price(qty),
            "price_breaks": [[b.qty, b.unit_price] for b in o.sorted_breaks()],
            "url": o.url,
            "match_confidence": str(o.match_confidence),
            "fetched_at": o.fetched_at,
        }
        for o in offers
    ]
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# part / assets
# ---------------------------------------------------------------------------


def _add_part_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    part = sub.add_parser("part", help="Create and inspect catalog parts.")
    actions = part.add_subparsers(dest="part_command", metavar="ACTION")

    add = actions.add_parser("add", help="Create a part and acquire its assets.")
    add.add_argument("--mpn", required=True, help="Manufacturer part number.")
    add.add_argument("--mfr", default="", help="Manufacturer. Left blank if unknown.")
    add.add_argument("--category", help="Taxonomy path, e.g. Passive/Resistor.")
    add.add_argument("--package", help="Physical package, e.g. 0402, SOT-23-5.")
    add.add_argument("--value", default="", help="Display value; normalized where possible.")
    add.add_argument("--description", default="", help="One-line human summary.")
    add.add_argument("--datasheet", help="Datasheet URL.")
    add.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Extra symbol field, repeatable (e.g. --field Tolerance=1%%).",
    )
    add.add_argument("--lcsc", metavar="C12345", help="Record an LCSC part number as an offer.")
    add.add_argument(
        "--status",
        choices=[str(s) for s in PartStatus],
        default=str(PartStatus.DRAFT),
        help="Status for the new part (default: draft).",
    )
    add.add_argument(
        "--offline",
        action="store_true",
        help="Do not consult suppliers to fill in missing details.",
    )
    add.set_defaults(func=cmd_part_add)


def _add_assets_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    assets = sub.add_parser("assets", help="Acquire, check and convert a part's files.")
    actions = assets.add_subparsers(dest="assets_command", metavar="ACTION")

    acquire = actions.add_parser("acquire", help="(Re)run asset acquisition for a part.")
    acquire.add_argument("part", metavar="ID_OR_MPN")
    acquire.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace assets the part already has, discarding hand corrections.",
    )
    acquire.set_defaults(func=cmd_assets_acquire)

    qa = actions.add_parser("qa", help="Re-run the QA gate on a part's stored assets.")
    qa.add_argument("part", nargs="?", metavar="ID_OR_MPN", help="Default: every part.")
    qa.add_argument("--format", choices=("text", "json"), default="text")
    qa.set_defaults(func=cmd_assets_qa)

    convert = actions.add_parser("convert-3d", help="Convert a mesh to a solid STEP model.")
    convert.add_argument("mesh", metavar="PATH", help="An .obj, .wrl, .stl, .ply or .off file.")
    convert.add_argument("--part", metavar="ID_OR_MPN", help="Attach the result to this part.")
    convert.add_argument(
        "--tolerance", type=float, default=0.1, help="Mesh tolerance in mm (default: 0.1)."
    )
    convert.set_defaults(func=cmd_assets_convert)

    reuse = actions.add_parser("reuse-check", help="Find near-duplicate footprints.")
    reuse.set_defaults(func=cmd_assets_reuse)


def _print_qa(label: str, report: QaReport, *, verbose: bool = False) -> None:
    mark = {QaStatus.PASS: _OK, QaStatus.WARN: _WARN, QaStatus.FAIL: _FAIL}.get(
        report.status, _INFO
    )
    print(f"{mark} {label:<10} {report.status}")
    for result in report.results:
        if verbose or result.status in (QaStatus.FAIL, QaStatus.WARN):
            print(f"      {result}")


def _parse_fields(pairs: Sequence[str]) -> dict[str, str]:
    """`--field Tolerance=1%` → `{"Tolerance": "1%"}`.

    A pair with no `=` is an error rather than a field with an empty value:
    `--field Tolerance` almost certainly means the value was forgotten, and
    silently writing an empty field would trip lint rule S006 later with no
    clue why.
    """
    fields: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--field expects NAME=VALUE, got {pair!r}")
        fields[name.strip()] = value.strip()
    return fields


def cmd_part_add(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    config = load_config(paths.config)

    adapters = (
        {} if args.offline else build_adapters(config, paths.supplier_cache, offline=False)
    )
    store = AssetStore(paths.assets)
    conn = connect(paths.db, create=False)
    try:
        report = add_part(
            conn,
            store,
            mpn=args.mpn,
            manufacturer=args.mfr,
            category=args.category,
            package=args.package,
            value=args.value,
            description=args.description,
            datasheet=args.datasheet,
            fields=_parse_fields(args.field),
            lcsc=args.lcsc,
            adapters=adapters,
            status=PartStatus(args.status),
        )
    finally:
        conn.close()

    part = report.part
    verb = "created" if report.created else "updated"
    who = part.manufacturer or "manufacturer unknown"
    print(f"{_OK} {verb} {part.klm_id}  {part.mpn}  ({who})")
    for note in report.notes:
        print(f"{_INFO} {note}")

    assets = report.assets
    if assets is not None:
        for acquired in assets.acquired:
            print(f"  {acquired.kind.value:<10} {acquired.origin:<16} {acquired.detail}")
            if acquired.qa is not None and acquired.qa.status is not QaStatus.PASS:
                _print_qa(acquired.kind.value, acquired.qa)
        for kind, reason in assets.unavailable:
            print(f"{_WARN} {kind}: {reason}")

    for offer in report.offers:
        print(f"  offer      {offer.supplier}:{offer.supplier_pn}")

    print()
    print("Next: klm lint --select S,V,A --fix --dry-run, then approve it")
    return EXIT_OK if report.ok else EXIT_CHECK_FAILED


def cmd_assets_acquire(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    store = AssetStore(paths.assets)

    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
        report = acquire_assets(conn, store, part, overwrite=args.overwrite)
    finally:
        conn.close()

    print(f"{part.klm_id}  {part.mpn}")
    for acquired in report.acquired:
        note = "  [reused]" if acquired.reused else ""
        print(f"  {acquired.kind.value:<10} {acquired.origin:<16} {acquired.detail}{note}")
        if acquired.qa is not None:
            _print_qa(acquired.kind.value, acquired.qa)
    for kind, reason in report.unavailable:
        print(f"{_WARN} {kind}: {reason}")

    if not report.acquired and not report.unavailable:
        print(f"{_OK} nothing to do — every asset is already present")
        return EXIT_OK
    return EXIT_OK if report.ok else EXIT_CHECK_FAILED


def cmd_assets_qa(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    store = AssetStore(paths.assets)

    conn = connect(paths.db, create=False)
    try:
        parts = [_resolve_part(conn, args.part)] if args.part else list_parts(conn)
        results = {part.klm_id: (part, run_qa(conn, store, part)) for part in parts}
    finally:
        conn.close()

    if args.format == "json":
        print(
            json.dumps(
                {
                    klm_id: {kind.value: json.loads(report.to_json())
                             for kind, report in reports.items()}
                    for klm_id, (_part, reports) in results.items()
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        failed = any(
            not report.passed for _part, reports in results.values() for report in reports.values()
        )
        return EXIT_CHECK_FAILED if failed else EXIT_OK

    failures = 0
    for _klm_id, (part, reports) in sorted(results.items()):
        if not reports:
            continue
        print(f"{part.klm_id}  {part.mpn}")
        for kind, report in reports.items():
            _print_qa(kind.value, report, verbose=bool(args.part))
            failures += 0 if report.passed else 1
        print()

    if failures:
        print(f"{_FAIL} {failures} asset(s) fail the QA gate and cannot be approved")
        return EXIT_CHECK_FAILED
    print(f"{_OK} {len(results)} part(s) checked, no blocking failures")
    return EXIT_OK


def cmd_assets_convert(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    source = Path(args.mesh).expanduser()

    try:
        result = convert_mesh(source, paths.cache / "models3d", tolerance=args.tolerance)
    except FreeCadUnavailable as exc:
        print(f"{_WARN} {exc}")
        return EXIT_CHECK_FAILED

    state = "already converted" if result.cached else "converted"
    print(f"{_OK} {state}: {result.output}")
    if not result.watertight:
        print(f"{_WARN} the source mesh was not watertight; the solid has gaps")

    data = result.output.read_bytes()
    report = check_model3d(data)
    _print_qa("model3d", report, verbose=True)

    if not args.part:
        return EXIT_OK if report.passed else EXIT_CHECK_FAILED

    store = AssetStore(paths.assets)
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
        part.model3d_hash = store.add_bytes(data, AssetKind.MODEL3D)
        register_asset(
            conn,
            part.model3d_hash,
            AssetKind.MODEL3D,
            filename=source.stem,
            source="generated",
            qa=report,
        )
        save_part(conn, part)
    finally:
        conn.close()

    print(f"{_OK} attached to {part.klm_id} ({part.mpn})")
    return EXIT_OK if report.passed else EXIT_CHECK_FAILED


def cmd_assets_reuse(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    store = AssetStore(paths.assets)

    conn = connect(paths.db, create=False)
    try:
        candidates = reuse_candidates(conn, store)
    finally:
        conn.close()

    if not candidates:
        print(f"{_OK} no duplicate footprints found")
        return EXIT_OK

    for candidate in candidates:
        print(f"{_WARN} {candidate.left}")
        print(f"       {candidate.right}")
        print(f"       {candidate.detail}")
    print(f"\n{len(candidates)} duplicate footprint(s) — merging them shrinks the catalog")
    return EXIT_CHECK_FAILED


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


# ---------------------------------------------------------------------------
# vendor / unvendor / sync / promote
# ---------------------------------------------------------------------------


def _open_project(args: argparse.Namespace) -> tuple[Paths, KiCadProject]:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    return paths, find_project(args.project)


def _require_vendored(project: KiCadProject) -> None:
    """A linked project is a normal state, not a klm error — say so and stop.

    Letting `LockError` reach the top would exit 2 ("klm itself errored") for a
    project that is simply not vendored yet.
    """
    if not project.is_vendored:
        raise SystemExit(
            f"klm: {project.root} is not vendored — there is no {LOCK_FILE} to compare against.\n"
            "      Run `klm vendor` first, or `klm sync adopt` if the lock was lost."
        )


def _selection(parts: Sequence[str], status: SyncStatus) -> set[str] | None:
    """Turn part arguments into klm_ids, or None for "everything applicable"."""
    if not parts:
        return None
    wanted = set(parts)
    chosen = {row.klm_id for row in status.rows if wanted & {row.klm_id, row.mpn, row.symbol_name}}
    missing = wanted - {row.klm_id for row in status.rows} - {row.mpn for row in status.rows}
    missing -= {row.symbol_name or "" for row in status.rows}
    if missing:
        raise SystemExit(f"klm: not in this project: {', '.join(sorted(missing))}")
    return chosen


def cmd_vendor(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    conn = connect(paths.db, create=False)
    try:
        store = AssetStore(paths.assets)
        try:
            report = vendor(
                conn,
                store,
                project,
                library_name=args.name,
                include_3d=args.with_3d,
                from_libraries=args.from_libraries,
                allow_unresolved=args.allow_unresolved,
                strict=args.strict,
                dry_run=args.dry_run,
                timestamp=not args.no_timestamp,
            )
        except VendorError as exc:
            plan = plan_vendor(
                conn,
                store,
                project,
                library_name=args.name,
                from_libraries=args.from_libraries,
            )
            _print_unvendored(plan)
            print(f"\n{_FAIL} {exc}")
            return EXIT_CHECK_FAILED
    finally:
        conn.close()

    verb = "would vendor" if args.dry_run else "vendored"
    print(f"{_OK} {verb} {report.symbols} symbols and {report.footprints} footprints", end="")
    print(f" (+{report.models} 3D models)" if report.models else "")
    print(f"    into {project.libraries}")
    for name, count in sorted(report.rewritten.items()):
        print(f"    {name}: {count} reference(s) rewritten")
    if report.plan.footprint_only:
        print(f"{_INFO} {len(report.plan.footprint_only)} footprint-only part(s) from the board")
    _print_unvendored(report.plan)
    if report.build.skipped:
        for klm_id, reason in report.build.skipped:
            print(f"{_WARN} {klm_id}: {reason}")

    if args.dry_run:
        print(f"\n{_INFO} nothing written; {'changes' if report.changed else 'no changes'} pending")
        return EXIT_OK
    if not report.changed:
        print(f"\n{_OK} already up to date")
        return EXIT_OK
    print(f"\n{_OK} {project.lock_file.name} written; originals kept as *.klm-bak")
    return EXIT_OK


def _print_unvendored(plan: VendorPlan) -> None:
    """Blocking problems in full; the rest grouped, or it drowns them out.

    A real schematic carries dozens of `power:GND` flags. Printing one line each
    would bury the two symbols that actually need a decision.
    """
    if plan.unresolved:
        print()
        for item in plan.unresolved:
            where = f"{item.where}:" if item.where else ""
            print(f"{_FAIL} {where}{item.reference or '?'} {item.lib_id} — {item.reason}")

    if plan.external:
        by_library: dict[str, list[str]] = {}
        for item in plan.external:
            nickname = item.lib_id.partition(":")[0]
            by_library.setdefault(nickname, []).append(item.reference or "?")
        print()
        print(f"{_INFO} left linked — klm does not manage these libraries:")
        for nickname, refs in sorted(by_library.items()):
            shown = ", ".join(sorted(refs)[:4])
            more = f", +{len(refs) - 4} more" if len(refs) > 4 else ""
            print(f"      {nickname:<22} {len(refs):>3} symbol(s)   {shown}{more}")
        print("      → --from-library NICKNAME resolves one of these against the catalog")


def cmd_unvendor(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    conn = connect(paths.db, create=False)
    try:
        try:
            report = unvendor(
                conn, AssetStore(paths.assets), project, dry_run=args.dry_run, force=args.force
            )
        except VendorError as exc:
            print(f"{_FAIL} {exc}")
            return EXIT_CHECK_FAILED
    finally:
        conn.close()

    verb = "would restore" if args.dry_run else "restored"
    print(f"{_OK} {verb} global library references")
    for name, count in sorted(report.rewritten.items()):
        print(f"    {name}: {count} reference(s) rewritten")
    for path in report.removed:
        print(f"    {'would remove' if args.dry_run else 'removed'} {path.name}")
    return EXIT_OK


_STATE_GLYPH = {
    SyncState.CLEAN: _OK,
    SyncState.DEPRECATED_UPSTREAM: _INFO,
    SyncState.GLOBAL_AHEAD: _WARN,
    SyncState.PROJECT_AHEAD: _WARN,
    SyncState.CONFLICT: _FAIL,
    SyncState.MISSING: _FAIL,
    SyncState.ORPHAN: _WARN,
}


def cmd_sync_status(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    _require_vendored(project)
    conn = connect(paths.db, create=False)
    try:
        status = sync_status(conn, project)
    finally:
        conn.close()

    if args.format == "json":
        print(
            json.dumps(
                {
                    "project": str(project.root),
                    "library_name": status.library_name,
                    "parts": [
                        {
                            "klm_id": row.klm_id,
                            "mpn": row.mpn,
                            "symbol": row.symbol_name,
                            "state": str(row.state),
                            "detail": row.detail,
                        }
                        for row in sorted(status.rows, key=lambda r: (r.mpn, r.klm_id))
                    ],
                },
                indent=2,
            )
        )
    else:
        for state, rows in status.by_state().items():
            print(f"  {_STATE_GLYPH[state]} {state!s:<20} {len(rows)} part(s)")
            if state is SyncState.CLEAN:
                continue
            for row in rows:
                print(f"      {row.mpn or row.symbol_name or row.klm_id:<24} {row.detail}")
        if status.attention:
            print(f"\n{status.attention} part(s) need attention.")
            print("See: klm sync pull | klm sync push | klm sync resolve | klm promote")
        else:
            print(f"\n{_OK} in step with the catalog")

    return EXIT_CHECK_FAILED if (args.exit_code and status.attention) else EXIT_OK


def cmd_sync_pull(args: argparse.Namespace) -> int:
    return _run_sync(args, pull, "pulled", "would pull")


def cmd_sync_push(args: argparse.Namespace) -> int:
    return _run_sync(args, push, "pushed", "would push")


def _run_sync(
    args: argparse.Namespace,
    operation: Callable[..., SyncReport],
    done: str,
    planned: str,
) -> int:
    paths, project = _open_project(args)
    _require_vendored(project)
    conn = connect(paths.db, create=False)
    try:
        store = AssetStore(paths.assets)
        selection = _selection(args.parts, sync_status(conn, project))
        report = operation(
            conn, store, project, only=selection, strategy=args.strategy, dry_run=args.dry_run
        )
    finally:
        conn.close()

    if not report.applied and not report.skipped:
        print(f"{_OK} nothing to {done.rstrip('ed')}")
        return EXIT_OK

    verb = planned if args.dry_run else done
    for row in report.applied:
        print(f"{_OK} {verb} {row.mpn or row.symbol_name}: {row.detail}")
    for kind_reports in report.qa.values():
        for kind, qa in sorted(kind_reports.items(), key=lambda item: str(item[0])):
            _print_qa(str(kind), qa)
    for row, reason in report.skipped:
        print(f"{_WARN} skipped {row.mpn or row.symbol_name}: {reason}")
    return EXIT_CHECK_FAILED if report.skipped else EXIT_OK


def cmd_sync_resolve(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    _require_vendored(project)
    conn = connect(paths.db, create=False)
    try:
        store = AssetStore(paths.assets)
        status = sync_status(conn, project)
        conflicts = status.select(SyncState.CONFLICT)
        if not conflicts:
            print(f"{_OK} no conflicts")
            return EXIT_OK

        decisions = _decide(conflicts, args.strategy)
        take_global = {row.klm_id for row, choice in decisions if choice == "use-global"}
        take_project = {row.klm_id for row, choice in decisions if choice == "use-project"}

        if take_global:
            pull(conn, store, project, only=take_global, strategy="prefer-global")
        if take_project:
            push(conn, store, project, only=take_project, strategy="prefer-project")
    finally:
        conn.close()

    skipped = len(conflicts) - len(take_global) - len(take_project)
    print(
        f"\n{_OK} {len(take_global)} resolved from the catalog, "
        f"{len(take_project)} from the project, {skipped} left alone"
    )
    return EXIT_CHECK_FAILED if skipped else EXIT_OK


def _decide(conflicts: list[SyncRow], strategy: str | None) -> list[tuple[SyncRow, str]]:
    """Ask about each conflict, or apply one answer to all of them."""
    if strategy is not None:
        choice = "use-global" if strategy == "prefer-global" else "use-project"
        return [(row, choice) for row in conflicts]
    if not sys.stdin.isatty():
        raise SystemExit(
            "klm: resolving conflicts needs a terminal, or --strategy "
            "prefer-global|prefer-project"
        )

    decisions: list[tuple[SyncRow, str]] = []
    for row in conflicts:
        print(f"\n{_FAIL} {row.mpn or row.symbol_name}")
        print(f"    {row.detail}")
        answer = input("    use-global / use-project / skip? [skip] ").strip() or "skip"
        while answer not in ("use-global", "use-project", "skip"):
            answer = input("    use-global / use-project / skip? [skip] ").strip() or "skip"
        decisions.append((row, answer))
    return decisions


def cmd_sync_adopt(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    conn = connect(paths.db, create=False)
    try:
        report = adopt(
            conn,
            AssetStore(paths.assets),
            project,
            library_name=args.name,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()

    verb = "would rebuild" if args.dry_run else "rebuilt"
    print(f"{_OK} {verb} the lock from {len(report.matched)} matched symbol(s)")
    for name, klm_id in report.matched:
        print(f"    {name} → {klm_id}")
    for name in report.unmatched:
        print(f"{_WARN} {name}: no catalog part matches — it will report as an orphan")
    return EXIT_OK


def cmd_promote(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    conn = connect(paths.db, create=False)
    try:
        try:
            _require_vendored(project)
            report = promote(
                conn,
                AssetStore(paths.assets),
                project,
                args.reference,
                category=args.category,
                dry_run=args.dry_run,
            )
        except VendorError as exc:
            print(f"{_FAIL} {exc}")
            return EXIT_CHECK_FAILED
    finally:
        conn.close()

    verb = "would promote" if args.dry_run else "promoted"
    print(f"{_OK} {verb} {report.symbol_name} → {report.mpn} ({report.klm_id})")
    for kind, qa in sorted(report.qa.items(), key=lambda item: str(item[0])):
        _print_qa(str(kind), qa)
    if not args.dry_run:
        print(f"\n{_INFO} it landed as a draft — review it, then approve")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
