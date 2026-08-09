"""``klm`` entry point.

Exit codes are uniform across every command (docs/12 §2):

* ``0`` success
* ``1`` the checked condition failed
* ``2`` klm itself errored
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Sequence

from klm import __version__
from klm.environment import find_kicad_config, probe_all
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
