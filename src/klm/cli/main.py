"""``klm`` entry point.

Exit codes are uniform across every command (docs/12 §2):

* ``0`` success
* ``1`` the checked condition failed
* ``2`` klm itself errored
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import textwrap
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from klm import __version__
from klm.assets.qa import QaReport, QaStatus, check_model3d
from klm.cad.freecad import FreeCadUnavailable, convert_mesh
from klm.config import Config, load_config
from klm.environment import find_kicad_config, probe_all
from klm.fab.profiles import GENERIC, profile_for, render_csv
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
from klm.services.bom import BomReport, Variant, extract_bom, load_variants
from klm.services.catalog import get_part, list_parts, save_part
from klm.services.corrections import delete_pattern, list_patterns, part_corrections, set_pattern
from klm.services.demand import DemandLine, SparesPolicy, parse_build_plan, plan_demand
from klm.services.docs import build_docs, github_summary, json_report
from klm.services.exporter import PART_FILE, export_catalog, import_catalog
from klm.services.fab import fab_feedback, fab_package
from klm.services.generate import generate
from klm.services.importer import import_symbol_library
from klm.services.labels import labels_for_parts, render_png, resolve_short_id, write_pdf
from klm.services.lint import RULES, LintReport, Selector, Severity, lint_catalog
from klm.services.lockfile import read_lock
from klm.services.offers import (
    TIMESTAMP_FORMAT,
    delete_offer,
    list_offers,
    refresh_offers,
    save_offer,
    stale_part_ids,
)
from klm.services.orders import (
    STATES,
    cart_summary,
    create_order,
    export_cart,
    get_order,
    list_orders,
    mark_placed,
    pin_supplier,
    pins,
    receive,
)
from klm.services.part_add import add_part
from klm.services.preview import PreviewError, render_part
from klm.services.register import apply_plan, plan_registration
from klm.services.scaffold import KICAD_IMAGE, apply_scaffold, plan_scaffold
from klm.services.split import Assignment, split_order
from klm.services.stock import (
    StockItem,
    adjust,
    consume,
    list_stock,
    low_stock,
    set_threshold,
    where,
)
from klm.services.sync import (
    SyncReport,
    SyncRow,
    SyncState,
    SyncStatus,
    adopt,
    diff_part,
    promote,
    pull,
    push,
    sync_status,
)
from klm.services.vendor import VendorError, VendorPlan, plan_vendor, unvendor, vendor
from klm.services.verify import to_json, verify_clean_room
from klm.store import AssetKind, AssetStore, Paths, connect, migrate
from klm.store.db import SCHEMA_VERSION, user_version
from klm.suppliers.base import SupplierAdapter
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

    render = sub.add_parser("render", help="Draw a part's symbol or footprint as SVG.")
    render.add_argument("part", metavar="PART", help="KLM_ID or MPN.")
    render.add_argument(
        "--footprint",
        dest="kind",
        action="store_const",
        const="footprint",
        default="symbol",
        help="Draw the footprint instead of the symbol.",
    )
    render.add_argument(
        "--output", "-o", metavar="FILE", help="Write here instead of standard output."
    )
    render.set_defaults(func=cmd_render)

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
    _add_fab_parsers(sub)
    _add_repo_parsers(sub)
    _add_ordering_parsers(sub)
    _add_research_parsers(sub)

    app = sub.add_parser("app", help="Open klm in a desktop window.")
    app.add_argument("--port", type=int, help="Localhost port (default: a free one).")
    app.add_argument(
        "--serve",
        action="store_true",
        help="Print a URL and stay in the terminal instead of opening a window.",
    )
    app.set_defaults(func=cmd_app)

    serve = sub.add_parser("serve", help="Serve the UI on localhost without a window.")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    return parser


def _add_ordering_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    order = sub.add_parser("order", help="Plan, export, place and receive supplier orders.")
    actions = order.add_subparsers(dest="action", metavar="ACTION", required=True)

    plan = actions.add_parser("plan", help="Build plan to per-supplier carts.")
    plan.add_argument("--build", required=True, metavar="PLAN", help="e.g. 5xsensor-board:full")
    plan.add_argument("--projects", metavar="DIR", default=".", help="Where the projects live.")
    plan.add_argument("--explain", action="store_true", help="Justify every quantity.")
    plan.add_argument("--no-stock", action="store_true", help="Ignore what is on hand.")
    plan.add_argument("--save", action="store_true", help="Record draft orders in the catalog.")
    plan.set_defaults(func=cmd_order_plan)

    export = actions.add_parser("export", help="Write a cart file for one supplier.")
    export.add_argument("supplier")
    export.add_argument("--order", metavar="ID", help="An existing order, instead of a new plan.")
    export.add_argument("--output", metavar="DIR", default=".")
    export.set_defaults(func=cmd_order_export)

    show = actions.add_parser("list", help="Orders and their state.")
    show.add_argument("--state", choices=STATES)
    show.set_defaults(func=cmd_order_list)

    placed = actions.add_parser("mark-placed", help="Record that a cart was submitted.")
    placed.add_argument("order_id", metavar="ID")
    placed.add_argument("--total", type=float, help="What it actually cost.")
    placed.set_defaults(func=cmd_order_mark_placed)

    receiving = actions.add_parser("receive", help="Book a delivery in and stock it.")
    receiving.add_argument("order_id", metavar="ID")
    receiving.add_argument("--location", default="unfiled")
    receiving.add_argument(
        "--partial", metavar="PN:QTY", action="append", default=[], help="Repeatable."
    )
    receiving.set_defaults(func=cmd_order_receive)

    pin = actions.add_parser("pin", help="Force a part to one supplier. Absolute.")
    pin.add_argument("part", metavar="ID_OR_MPN")
    pin.add_argument("--supplier", help="Omit to remove the pin.")
    pin.set_defaults(func=cmd_order_pin)

    stock = sub.add_parser("stock", help="What is on the bench, and where.")
    stock_actions = stock.add_subparsers(dest="action", metavar="ACTION", required=True)

    listing = stock_actions.add_parser("list", help="Everything on hand.")
    listing.add_argument("--location", metavar="GLOB")
    listing.add_argument("--low", action="store_true", help="Only parts below their threshold.")
    listing.set_defaults(func=cmd_stock_list)

    adjusting = stock_actions.add_parser("adjust", help="Record a physical count.")
    adjusting.add_argument("part", metavar="ID_OR_MPN")
    adjusting.add_argument("--location", required=True)
    adjusting.add_argument("--set", dest="set_to", type=int)
    adjusting.add_argument("--delta", type=int, default=0)
    adjusting.set_defaults(func=cmd_stock_adjust)

    finding = stock_actions.add_parser("where", help="Where did I put those?")
    finding.add_argument("part", metavar="ID_OR_MPN")
    finding.set_defaults(func=cmd_stock_where)

    consuming = stock_actions.add_parser("consume", help="Decrement after building.")
    consuming.add_argument("--build", required=True, metavar="PLAN")
    consuming.add_argument("--projects", metavar="DIR", default=".")
    consuming.set_defaults(func=cmd_stock_consume)

    threshold = stock_actions.add_parser("threshold", help="Set a reorder threshold.")
    threshold.add_argument("part", metavar="ID_OR_MPN")
    threshold.add_argument("count", type=int)
    threshold.set_defaults(func=cmd_stock_threshold)

    labels = sub.add_parser("labels", help="Drawer labels, as a PDF sheet or PNGs.")
    label_actions = labels.add_subparsers(dest="action", metavar="ACTION", required=True)

    printing = label_actions.add_parser("print", help="Write labels.")
    printing.add_argument("--order", metavar="ID", help="Everything that just arrived.")
    printing.add_argument("--location", metavar="GLOB")
    printing.add_argument("--part", metavar="ID_OR_MPN", action="append", default=[])
    printing.add_argument("--output", metavar="PATH", default="labels.pdf")
    printing.add_argument("--format", choices=("pdf", "png"), default="pdf")
    printing.add_argument("--dpi", type=int, default=300)
    printing.set_defaults(func=cmd_labels_print)

    scanning = label_actions.add_parser("scan", help="Resolve a short ID from a drawer.")
    scanning.add_argument("short")
    scanning.set_defaults(func=cmd_labels_scan)


def _add_research_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    research = sub.add_parser("research", help="Find a part that meets a stated requirement.")
    actions = research.add_subparsers(dest="action", metavar="ACTION", required=True)

    check = actions.add_parser("check", help="Read a requirement file and say what it means.")
    check.add_argument("file", metavar="FILE")
    check.add_argument(
        "--toml",
        action="store_true",
        help="Print the requirement back, normalised — what klm actually understood.",
    )
    check.set_defaults(func=cmd_research_check)

    tools = actions.add_parser("tools", help="What the agent can do on this machine.")
    tools.add_argument("--format", choices=("text", "json"), default="text")
    tools.set_defaults(func=cmd_research_tools)

    run = actions.add_parser("run", help="Research a requirement. Costs money; proposes only.")
    run.add_argument("file", metavar="FILE", help="A requirement file (klm research check).")
    run.add_argument("--max-spend", type=float, default=2.0, metavar="USD")
    run.add_argument("--max-iterations", type=int, default=20)
    run.add_argument("--max-tokens", type=int, default=400_000, help="Across the whole session.")
    run.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="high")
    run.add_argument("--quiet", action="store_true", help="Don't stream the answer as it arrives.")
    run.set_defaults(func=cmd_research_run)

    review = actions.add_parser("review", help="Work the proposal queue.")
    review.add_argument("proposal", metavar="ID", nargs="?", type=int, help="Show one in full.")
    review.add_argument("--all", action="store_true", help="Decided ones too.")
    review.add_argument("--approve", metavar="ID", type=int, help="Create a draft part from it.")
    review.add_argument("--reject", metavar="ID", type=int)
    review.add_argument("--reason", metavar="TEXT", help="Required with --reject.")
    review.add_argument("--format", choices=("text", "json"), default="text")
    review.set_defaults(func=cmd_research_review)

    sheet = sub.add_parser("datasheet", help="Fetch a datasheet, or read parameters out of one.")
    sheet_actions = sheet.add_subparsers(dest="action", metavar="ACTION", required=True)

    getting = sheet_actions.add_parser("fetch", help="Download and cache a datasheet PDF.")
    getting.add_argument("target", metavar="URL_OR_PART")
    getting.add_argument("--refresh", action="store_true", help="Ignore the cached copy.")
    getting.set_defaults(func=cmd_datasheet_fetch)

    reading = sheet_actions.add_parser(
        "extract", help="Read parameters out of a datasheet, with the page and the quote."
    )
    reading.add_argument("target", metavar="URL_OR_PART")
    reading.add_argument(
        "--parameter", metavar="NAME", action="append", default=[], required=True,
        help="Repeatable. Ask in your own words: --parameter 'Vin max'.",
    )
    reading.add_argument("--format", choices=("text", "json"), default="text")
    reading.set_defaults(func=cmd_datasheet_extract)


def _add_repo_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    verify = sub.add_parser("verify", help="Prove a project opens for somebody else.")
    _project_argument(verify)
    verify.add_argument(
        "--clean-room",
        action="store_true",
        help="Resolve using only the repository and a stock KiCad. The real check.",
    )
    verify.add_argument("--format", choices=("text", "json", "github"), default="text")
    verify.add_argument(
        "--require-3d", action="store_true", help="Fail if no 3D model is vendored."
    )
    verify.set_defaults(func=cmd_verify)

    scaffold = sub.add_parser("scaffold", help="Write the repository furniture around a project.")
    _project_argument(scaffold)
    scaffold.add_argument("--preset", choices=("publish", "private"), default="publish")
    scaffold.add_argument(
        "--check",
        action="store_true",
        help="Report drift from this klm's templates; non-zero if any.",
    )
    scaffold.add_argument(
        "--update", action="store_true", help="Regenerate, preserving edits outside the markers."
    )
    scaffold.set_defaults(func=cmd_scaffold)

    docs = sub.add_parser("docs", help="Schematic PDF, board renders and a STEP model.")
    _project_argument(docs)
    docs.add_argument("--output", metavar="DIR", default="artifacts", help="Where to write them.")
    docs.add_argument("--pdf", action="store_true")
    docs.add_argument("--render", action="store_true")
    docs.add_argument("--step", action="store_true")
    docs.add_argument("--all", action="store_true", help="Everything the project supports.")
    docs.set_defaults(func=cmd_docs)

    report = sub.add_parser("report", help="A summary of the board, for a person or for CI.")
    _project_argument(report)
    report.add_argument("--variant", metavar="NAME")
    report.add_argument("--package", metavar="DIR", help="A fab package to summarise alongside.")
    report.add_argument(
        "--format", choices=("markdown", "github-summary", "json"), default="markdown"
    )
    report.set_defaults(func=cmd_report)


def _add_fab_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    bom = sub.add_parser("bom", help="Extract the bill of materials from a project.")
    _project_argument(bom)
    bom.add_argument("--variant", metavar="NAME", help="Build variant from the project klm.toml.")
    bom.add_argument("--format", choices=("text", "csv", "json"), default="text")
    bom.set_defaults(func=cmd_bom)

    fab = sub.add_parser("fab", help="Build a fabrication package, or check one could be.")
    fab_actions = fab.add_subparsers(dest="action", metavar="ACTION")
    _project_argument(fab)
    fab.add_argument("--variant", metavar="NAME", help="Build variant from the project klm.toml.")
    fab.add_argument("--profile", default="jlcpcb", help="Fab profile (default: jlcpcb).")
    fab.add_argument("--output", metavar="DIR", help="Where to write the package.")
    fab.add_argument(
        "--check", action="store_true", help="Run preflight only; write nothing. For CI."
    )
    fab.add_argument(
        "--no-assembly", action="store_true", help="Bare boards: gerbers and drill only."
    )
    fab.add_argument(
        "--allow-dirty", action="store_true", help="Do not warn about uncommitted changes."
    )
    fab.add_argument(
        "--normalize-timestamps",
        action="store_true",
        help="Rewrite gerber/drill generation timestamps so two runs match byte for byte.",
    )
    fab.add_argument("--format", choices=("text", "github"), default="text")
    fab.add_argument(
        "--no-timestamp",
        action="store_true",
        help="Omit generated_at from the manifest, for byte-reproducible artifacts.",
    )
    fab.set_defaults(func=cmd_fab)

    feedback = fab_actions.add_parser(
        "feedback", help="Record how a physical board actually came back."
    )
    feedback.add_argument("package", metavar="DIR", help="The fab package that made the board.")
    feedback.add_argument(
        "--wrong",
        metavar="REF:DEGREES",
        action="append",
        default=[],
        help="A reference that was placed wrong, and by how much. Repeatable.",
    )
    feedback.add_argument(
        "--confirm-rest",
        action="store_true",
        help="Mark every other part as verified against a real board.",
    )
    feedback.add_argument(
        "--generalize",
        action="store_true",
        help="Also write a footprint-pattern rule. One board is one data point.",
    )
    feedback.set_defaults(func=cmd_fab_feedback)

    fixes = fab_actions.add_parser("corrections", help="Inspect and edit the correction table.")
    fixes.add_argument("operation", choices=("list", "set", "remove"))
    fixes.add_argument("pattern", nargs="?", help="Footprint-name regex, for set/remove.")
    fixes.add_argument("rotation", nargs="?", type=float, help="Degrees, for set.")
    fixes.add_argument("--offset-x", type=float, default=0.0)
    fixes.add_argument("--offset-y", type=float, default=0.0)
    fixes.add_argument("--source", choices=("user", "learned", "bundled"), default="user")
    fixes.set_defaults(func=cmd_fab_corrections)


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

    diff_cmd = actions.add_parser("diff", help="Show what moved under one vendored part.")
    _project_argument(diff_cmd)
    diff_cmd.add_argument("part", metavar="PART", help="MPN, symbol name or KLM_ID.")
    diff_cmd.add_argument(
        "--side",
        choices=("both", "catalog", "project"),
        default="both",
        help="Which side's drift to print.",
    )
    diff_cmd.add_argument("--format", choices=("text", "json"), default="text")
    diff_cmd.set_defaults(func=cmd_sync_diff)

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


def _make_output_encodable() -> None:
    """Stop klm's own output from raising on a non-UTF-8 stream.

    klm prints `✓`, `✗`, `⚠` and `→`. On Windows, a *redirected* stream encodes
    with the ANSI code page — cp1252 for most of the world — and none of those
    characters exist in it, so `print` raises `UnicodeEncodeError`. The symptom
    is absurd and was real: `klm doctor` worked in a terminal and died with
    "'charmap' codec can't encode character" the moment anyone wrote it to a
    file or a CI log.

    UTF-8 is the right encoding for a redirected stream, and a Windows console
    already renders these glyphs. `errors="replace"` is the belt-and-braces
    part: whatever the encoding turns out to be, a character klm cannot spell
    must never become an exception in place of the answer.
    """
    for stream in (sys.stdout, sys.stderr):
        # A stream that is not a reconfigurable text wrapper — pytest's capture,
        # a pipe someone replaced — is left alone. Nothing to do about it, and
        # nothing that justifies refusing to run.
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main(argv: Sequence[str] | None = None) -> int:
    _make_output_encodable()
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

    # The GUI could approve and deprecate and the CLI could not, which by this
    # project's own rule is a bug in the CLI rather than a feature of the GUI.
    approve = actions.add_parser("approve", help="Mark a part usable in a design.")
    approve.add_argument("part", metavar="ID_OR_MPN")
    approve.set_defaults(func=cmd_part_approve, status=PartStatus.APPROVED)

    deprecate = actions.add_parser("deprecate", help="Retire a part without deleting it.")
    deprecate.add_argument("part", metavar="ID_OR_MPN")
    deprecate.set_defaults(func=cmd_part_approve, status=PartStatus.DEPRECATED)

    show = actions.add_parser("show", help="Print one part, its offers and its stock.")
    show.add_argument("part", metavar="ID_OR_MPN")
    show.add_argument("--format", choices=("text", "json"), default="text")
    show.set_defaults(func=cmd_part_show)


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


def cmd_part_approve(args: argparse.Namespace) -> int:
    """`klm part approve` / `klm part deprecate` — the status write.

    Deliberately does not check lint first. Approval is a human's judgement and
    a linter's opinion is advice; making the command refuse would mean the only
    way to approve a part klm mis-reads is to edit the database by hand.
    """
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
        if part.status is args.status:
            print(f"{_INFO} {part.mpn} is already {args.status}")
            return EXIT_OK
        was, part.status = part.status, args.status
        part.updated_at = None
        save_part(conn, part)
    finally:
        conn.close()

    print(f"{_OK} {part.mpn}  {was} → {args.status}")
    if args.status is PartStatus.DEPRECATED and part.symbol_hash is not None:
        # S005: a deprecated part is not generated, so anything still using it
        # keeps working from its vendored copy and nothing new can pick it up.
        print(f"  {_INFO} it will stop being generated; `klm generate` to apply")
    return EXIT_OK


def cmd_part_show(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
        offers = list_offers(conn, klm_id=part.klm_id)
        locations = where(conn, part.klm_id)
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(_part_payload(part, offers, locations), indent=2))
        return EXIT_OK

    print(f"{part.mpn}  ({part.manufacturer or 'manufacturer unknown'})")
    print(f"  {part.description or 'no description'}")
    for label, value in (
        ("klm_id", part.klm_id),
        ("category", part.category or "—"),
        ("package", part.package or "—"),
        ("status", str(part.status)),
        ("lifecycle", str(part.lifecycle)),
        ("datasheet", part.datasheet_url or "—"),
    ):
        print(f"  {label:<12} {value}")

    print(f"\n  offers ({len(offers)})")
    for offer in offers:
        stock = "stock unknown" if offer.stock is None else f"{offer.stock} in stock"
        print(f"    {offer.supplier:<6} {offer.supplier_pn:<18} {stock}")
    if not offers:
        print("    none — `klm refresh` fetches them")

    print(f"\n  stock ({sum(item.quantity for item in locations)})")
    for item in locations:
        print(f"    {item.quantity:>7}  {item.location}")
    if not locations:
        print("    not recorded anywhere")
    return EXIT_OK


def _part_payload(part: Part, offers: list[Offer], locations: list[StockItem]) -> dict[str, object]:
    """The JSON shape of `klm part show`, matching the API's part payload."""
    return {
        "klm_id": part.klm_id,
        "mpn": part.mpn,
        "manufacturer": part.manufacturer,
        "description": part.description,
        "category": part.category,
        "package": part.package,
        "status": str(part.status),
        "lifecycle": str(part.lifecycle),
        "datasheet_url": part.datasheet_url,
        "symbol_hash": part.symbol_hash,
        "footprint_hash": part.footprint_hash,
        "model3d_hash": part.model3d_hash,
        "offers": [
            {"supplier": o.supplier, "supplier_pn": o.supplier_pn, "stock": o.stock}
            for o in offers
        ],
        "stock": [{"location": s.location, "quantity": s.quantity} for s in locations],
    }


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


def cmd_sync_diff(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    _require_vendored(project)
    conn = connect(paths.db, create=False)
    try:
        klm_id = _resolve_vendored(conn, project, args.part)
        result = diff_part(conn, AssetStore(paths.assets), project, klm_id)
    finally:
        conn.close()

    if args.format == "json":
        print(
            json.dumps(
                {
                    "klm_id": result.row.klm_id,
                    "mpn": result.row.mpn,
                    "state": str(result.row.state),
                    "assets": [
                        {
                            "kind": str(d.kind),
                            "side": d.side,
                            "name": d.name,
                            "changed": d.changed,
                            "note": d.note,
                            "unified": d.unified,
                        }
                        for d in result.diffs
                        if args.side in ("both", d.side)
                    ],
                },
                indent=2,
            )
        )
        return EXIT_OK

    print(f"{result.row.mpn or result.row.klm_id}  [{result.row.state}]")
    if result.row.detail:
        print(f"  {result.row.detail}")
    shown = [d for d in result.diffs if args.side in ("both", d.side)]
    for entry in shown:
        head = f"\n{'~' if entry.changed else _OK} {entry.side}: {entry.kind} {entry.name}"
        print(head if entry.changed else f"{head} — unchanged")
        if entry.note:
            print(f"  {_INFO} {entry.note}")
        if entry.changed and entry.unified:
            print(entry.unified.rstrip("\n"))
    if not shown:
        print(f"  {_INFO} nothing recorded to compare against")
    return EXIT_OK


def _resolve_vendored(conn: sqlite3.Connection, project: KiCadProject, reference: str) -> str:
    """A KLM_ID from whatever the user typed — an id, an MPN or a symbol name.

    Falls back to the lock file rather than only the catalog, because the part
    a user most wants to diff is often the orphan the catalog has never heard
    of, and "no such part" would be the least useful possible answer.
    """
    lock = read_lock(project.lock_file)
    if lock.by_id(reference) is not None:
        return reference
    by_symbol = lock.by_symbol(reference)
    if by_symbol is not None:
        return by_symbol.klm_id
    matches = {e.klm_id for e in lock.entries if e.mpn.lower() == reference.lower()}
    if len(matches) == 1:
        return matches.pop()
    if len(matches) > 1:
        raise LookupError(f"{reference!r} matches several vendored parts: {', '.join(matches)}")
    return _resolve_part(conn, reference).klm_id


def cmd_render(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
    finally:
        conn.close()

    kind = AssetKind.FOOTPRINT if args.kind == "footprint" else AssetKind.SYMBOL
    try:
        svg = render_part(AssetStore(paths.assets), part, kind)
    except PreviewError as exc:
        print(f"{_FAIL} {exc}", file=sys.stderr)
        return EXIT_CHECK_FAILED

    if args.output:
        Path(args.output).write_text(svg, encoding="utf-8")
        print(f"{_OK} {args.output}")
    else:
        print(svg)
    return EXIT_OK


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


# ---------------------------------------------------------------------------
# bom / fab
# ---------------------------------------------------------------------------


def _project_config(project: KiCadProject) -> dict[str, object]:
    """Read the project's own `klm.toml`, which holds its variants."""
    path = project.root / "klm.toml"
    if not path.is_file():
        return {}
    with open(path, "rb") as handle:
        return dict(tomllib.load(handle))


def _variant(project: KiCadProject, name: str | None) -> Variant | None:
    if not name:
        return None
    raw = _project_config(project).get("variants")
    variants = load_variants(dict(raw)) if isinstance(raw, dict) else {}
    if name not in variants:
        known = ", ".join(sorted(variants)) or "none defined"
        raise SystemExit(f"klm: no variant {name!r} in {project.root / 'klm.toml'} ({known})")
    return variants[name]


def cmd_bom(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    conn = connect(paths.db, create=False)
    try:
        report = extract_bom(conn, project, variant=_variant(project, args.variant))
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(_bom_json(report), indent=2, ensure_ascii=False))
        return EXIT_OK
    if args.format == "csv":
        print(
            render_csv(
                ("Comment", "Designator", "Footprint", "Quantity", "MPN", "Manufacturer"),
                [GENERIC.bom_row(line) for line in report.lines],
            ),
            end="",
        )
        return EXIT_OK

    for line in report.lines:
        print(
            f"  {line.quantity:>3}x  {line.value:<18} "
            f"{line.footprint_name:<28} {line.designators}"
        )
    if report.excluded:
        print(f"\n{_INFO} not populated:")
        for line in report.excluded:
            print(f"  {line.quantity:>3}x  {line.value:<18} {line.designators}")
    print(f"\n{len(report.lines)} line(s), {report.total_parts} part(s)")
    if report.unresolved:
        print(f"{_WARN} no catalog part for: {', '.join(report.unresolved[:10])}")
        return EXIT_CHECK_FAILED
    return EXIT_OK


def _bom_json(report: BomReport) -> dict[str, object]:
    return {
        "variant": report.variant,
        "lines": [
            {
                "value": line.value,
                "footprint": line.footprint_name,
                "quantity": line.quantity,
                "designators": line.designators,
                "klm_id": line.klm_id,
                "mpn": line.mpn,
                "lcsc": line.lcsc,
                "dnp": line.dnp,
            }
            for line in (*report.lines, *report.excluded)
        ],
        "unresolved": report.unresolved,
    }


def cmd_fab(args: argparse.Namespace) -> int:
    paths, project = _open_project(args)
    try:
        profile = profile_for(args.profile)
    except KeyError as exc:
        raise SystemExit(f"klm: {exc}") from exc

    conn = connect(paths.db, create=False)
    try:
        report = fab_package(
            conn,
            AssetStore(paths.assets),
            load_config(paths.config),
            project,
            output_dir=Path(args.output) if args.output else None,
            profile=profile,
            variant=_variant(project, args.variant),
            assembly=not args.no_assembly,
            check_only=args.check,
            allow_dirty=args.allow_dirty,
            timestamp=not args.no_timestamp,
            normalize_timestamps=args.normalize_timestamps,
        )
    finally:
        conn.close()

    if args.format == "github":
        for check in report.preflight.checks:
            if check.status != "pass":
                severity = "error" if check.status == "fail" else "warning"
                print(_annotate(severity, f"{check.name}: {check.detail or 'failed'}"))
    else:
        for check in report.preflight.checks:
            glyph = {"pass": _OK, "warn": _WARN, "fail": _FAIL}[check.status]
            detail = f"  — {check.detail}" if check.detail else ""
            print(f"{glyph} {check.name}{detail}")

    if report.preflight.blocked:
        print(f"\n{_FAIL} preflight failed; no package written")
        return EXIT_CHECK_FAILED
    if args.check:
        print(f"\n{_OK} preflight passed ({len(report.preflight.warnings)} warning(s))")
        return EXIT_OK

    bom = report.bom
    print(f"\n{_OK} wrote {report.output_dir}")
    if bom:
        print(f"    {len(bom.lines)} BOM line(s), {bom.total_parts} part(s) placed")
    if report.normalized:
        print(f"    {report.normalized} file(s) had their timestamps normalized")
    if report.unconfirmed:
        # ADR-0011: klm bundles no rotation data, so an unconfirmed part has
        # nothing behind it. Saying so is the whole mitigation.
        print(f"{_WARN} rotation unconfirmed for {len(report.unconfirmed)} reference(s):")
        print(f"      {', '.join(report.unconfirmed[:12])}")
        print("      → check the fab's DFM preview, then: klm fab feedback")
    return EXIT_OK


def cmd_fab_feedback(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)

    wrong: dict[str, float] = {}
    for item in args.wrong:
        reference, _, degrees = item.partition(":")
        try:
            wrong[reference.strip()] = float(degrees)
        except ValueError:
            raise SystemExit(f"klm: --wrong wants REF:DEGREES, got {item!r}") from None

    conn = connect(paths.db, create=False)
    try:
        report = fab_feedback(
            conn,
            Path(args.package),
            wrong=wrong,
            confirm_rest=args.confirm_rest,
            generalize=args.generalize,
        )
    finally:
        conn.close()

    for reference, mpn, degrees in report.corrected:
        print(f"{_OK} {reference} ({mpn}): corrected by {degrees:g}°")
    for pattern, degrees in report.generalized:
        print(f"{_INFO} generalized to {pattern} = {degrees:g}°")
    if report.confirmed:
        print(f"{_OK} confirmed {len(report.confirmed)} part(s) against this board")
    for reference in report.unknown:
        print(f"{_WARN} {reference} is not in this package")
    return EXIT_CHECK_FAILED if report.unknown else EXIT_OK


def cmd_fab_corrections(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        if args.operation == "list":
            return _print_corrections(conn)
        if args.pattern is None:
            raise SystemExit("klm: this needs a footprint pattern")
        if args.operation == "remove":
            removed = delete_pattern(conn, args.pattern)
            print(f"{_OK} removed {args.pattern}" if removed else f"{_WARN} no such pattern")
            return EXIT_OK if removed else EXIT_CHECK_FAILED
        if args.rotation is None:
            raise SystemExit("klm: set needs a rotation in degrees")
        set_pattern(
            conn,
            args.pattern,
            args.rotation,
            offset_x=args.offset_x,
            offset_y=args.offset_y,
            source=args.source,
        )
        print(f"{_OK} {args.pattern} = {args.rotation:g}° ({args.source})")
        return EXIT_OK
    finally:
        conn.close()


def _print_corrections(conn: sqlite3.Connection) -> int:
    parts = part_corrections(conn)
    patterns = list_patterns(conn)
    if parts:
        print("per part — the specific key, and the one a board confirms:")
        for _klm_id, mpn, correction in parts:
            mark = _OK if correction.confirmed else _WARN
            print(f"  {mark} {mpn:<28} {correction.rotation:>7.1f}°  {correction.source}")
    if patterns:
        print("\nby footprint pattern:")
        for pattern, correction in patterns:
            mark = _OK if correction.confirmed else _WARN
            print(f"  {mark} {pattern:<28} {correction.rotation:>7.1f}°  {correction.source}")
    if not parts and not patterns:
        # ADR-0011: this is the expected state on a fresh install, and saying so
        # beats an empty screen that reads like a bug.
        print(f"{_INFO} no corrections recorded yet.")
        print("    klm bundles none deliberately — the one community table is GPL-3.0 and")
        print("    keyed by footprint, which cannot express two parts on one land pattern")
        print("    needing different rotations. See docs/adr/0011.")
        print("    The table fills as boards come back: klm fab feedback --confirm-rest")
    return EXIT_OK


# ---------------------------------------------------------------------------
# verify / scaffold / docs / report
# ---------------------------------------------------------------------------

_GITHUB_LEVEL = {"error": "error", "warning": "warning", "info": "notice"}


def _annotate(severity: str, message: str, *, file: str = "", line: int = 0) -> str:
    """A GitHub Actions workflow command, so a finding lands on the right line.

    The whole point of the format: a reviewer sees the offending `lib_id` in the
    PR diff rather than having to open a log.
    """
    parts = []
    if file:
        parts.append(f"file={file}")
    if line:
        parts.append(f"line={line}")
    location = f" {','.join(parts)}" if parts else ""
    flat = message.replace("\n", " ").replace("%", "%25")
    return f"::{_GITHUB_LEVEL.get(severity, 'notice')}{location}::{flat}"


def cmd_verify(args: argparse.Namespace) -> int:
    """Deliberately does not open the catalog — see klm.services.verify."""
    project = find_project(args.project)
    if not args.clean_room:
        print(f"{_INFO} klm verify currently implements --clean-room only; running that.")
    report = verify_clean_room(project, require_3d=args.require_3d)

    if args.format == "json":
        print(to_json(report))
    elif args.format == "github":
        for finding in report.findings:
            print(
                _annotate(
                    finding.severity, finding.message, file=finding.file, line=finding.line
                )
            )
    else:
        for name, summary in report.checks.items():
            glyph = {"error": _FAIL, "warning": _WARN, "info": _INFO, "pass": _OK}[
                report.status(name)
            ]
            print(f"{glyph} {name:<24} {summary.lstrip('~')}")
            for finding in report.findings:
                if finding.check == name:
                    where = f"{finding.location}  " if finding.location else ""
                    print(f"    {where}{finding.message}")
        print()
        if report.failed:
            print(f"{_FAIL} FAILED — {report.count('error')} error(s)")
        else:
            print(f"{_OK} this repository is enough to open the project")

    return EXIT_CHECK_FAILED if report.failed else EXIT_OK


def cmd_scaffold(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser().resolve()
    variants = tuple(sorted(_project_variants(root)))
    try:
        plan = plan_scaffold(root, preset=args.preset, variants=variants)
    except KeyError as exc:
        raise SystemExit(f"klm: {exc}") from exc

    if args.check:
        for change in plan.needed:
            print(f"{_WARN} {change.path}: would {change.action}")
        if plan.drifted:
            print(f"\n{_FAIL} {len(plan.needed)} file(s) differ — run: klm scaffold --update")
            return EXIT_CHECK_FAILED
        print(f"{_OK} scaffolding matches klm {__version__}")
        return EXIT_OK

    written = apply_scaffold(plan)
    for change in written:
        print(f"{_OK} {change.action}d {change.path}")
    if not written:
        print(f"{_OK} nothing to do — scaffolding is current")
    else:
        print(f"\n{_INFO} workflows pin {KICAD_IMAGE} and klm {__version__}; edits outside")
        print("    the klm:managed markers survive --update")
    return EXIT_OK


def _project_variants(root: Path) -> set[str]:
    path = root / "klm.toml"
    if not path.is_file():
        return set()
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    variants = raw.get("variants")
    return set(variants) if isinstance(variants, dict) else set()


def cmd_docs(args: argparse.Namespace) -> int:
    project = find_project(args.project)
    wanted = (args.pdf, args.render, args.step)
    if args.all or not any(wanted):
        args.pdf = args.render = args.step = True

    report = build_docs(
        project,
        Path(args.output),
        pdf=args.pdf,
        render=args.render,
        step=args.step,
    )
    for path in report.written:
        print(f"{_OK} {path}")
    for name, reason in report.skipped:
        print(f"{_WARN} {name}: {reason}")
    # A missing artifact is a degradation, not a failure: losing the schematic
    # PDF because the 3D render wanted an X server would be a poor trade.
    return EXIT_OK if report.written else EXIT_CHECK_FAILED


def cmd_report(args: argparse.Namespace) -> int:
    project = find_project(args.project)
    paths = Paths.resolve(args.catalog)
    conn = connect(paths.db, create=False) if paths.exists() else None
    try:
        variant = _variant(project, args.variant)
        package = Path(args.package) if args.package else None
        if package is None:
            default = project.root / "fab" / project.name
            package = default if default.is_dir() else None
        if args.format == "json":
            print(json_report(conn, project, variant=variant, package=package))
        else:
            print(github_summary(conn, project, variant=variant, package=package), end="")
    finally:
        if conn is not None:
            conn.close()
    return EXIT_OK


# ---------------------------------------------------------------------------
# order / stock / labels
# ---------------------------------------------------------------------------


def _spares_policy(config: Config, paths: Paths) -> SparesPolicy:
    if not paths.config.is_file():
        return SparesPolicy()
    with open(paths.config, "rb") as handle:
        raw = tomllib.load(handle)
    return SparesPolicy.from_config(dict(raw.get("spares") or {}))


def _plan(args: argparse.Namespace, paths: Paths, conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    builds = parse_build_plan(args.build)
    return plan_demand(
        conn,
        builds,
        projects_root=args.projects,
        policy=_spares_policy(load_config(paths.config), paths),
        use_stock=not getattr(args, "no_stock", False),
    )


def cmd_order_plan(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    config = load_config(paths.config)
    conn = connect(paths.db, create=False)
    try:
        demand = _plan(args, paths, conn)
        result = split_order(demand.orderable, config, pins=pins(conn))

        for project, references in sorted(demand.unresolved.items()):
            print(f"{_WARN} {project}: {len(references)} reference(s) with no catalog part")

        for assignment in result.assignments:
            line = assignment.line
            price = f"{assignment.subtotal:8.2f}" if assignment.unit_price else "       ?"
            print(
                f"  {assignment.quantity:>5} x {line.mpn:<26} {assignment.supplier:<6}"
                f" {price}  [{assignment.reason}]"
            )
            if args.explain:
                for note in line.explain():
                    print(f"          {note}")
                for name, delta, note in assignment.alternatives:
                    extra = f"{delta:+.2f}"
                    print(f"          alt {name}: {extra}{'  ' + note if note else ''}")

        for line in result.unsourced:
            print(f"{_WARN} {line.mpn}: no enabled supplier stocks {line.order_qty}")

        print()
        for supplier in sorted(result.carts):
            for row in cart_summary(result, supplier):
                print(row)
        print(f"\n  grand total {result.total:.2f}   (estimate — see the assumptions above)")
        for improvement in result.improvements:
            print(f"{_INFO} {improvement}")

        if args.save:
            for supplier in sorted(result.carts):
                order = create_order(
                    conn,
                    supplier,
                    result.for_supplier(supplier),
                    currency=config.suppliers[supplier].currency
                    if supplier in config.suppliers
                    else None,
                )
                print(f"{_OK} saved draft order {order.id}")
    finally:
        conn.close()
    return EXIT_CHECK_FAILED if result.unsourced else EXIT_OK


def cmd_order_export(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        if not args.order:
            raise SystemExit("klm: --order ID is required; run `klm order plan --save` first")
        order = get_order(conn, args.order)
        if order is None:
            raise SystemExit(f"klm: no order {args.order!r}")
        assignments = [
            Assignment(
                line=DemandLine(klm_id=line.klm_id, part=get_part(conn, line.klm_id)),
                supplier=order.supplier,
                offer=Offer(supplier=order.supplier, supplier_pn=line.supplier_pn),
                quantity=line.qty_ordered,
                unit_price=line.unit_price,
            )
            for line in order.lines
        ]
        name, body = export_cart(conn, args.supplier, assignments)
        target = Path(args.output) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="")
        print(f"{_OK} {target}  ({len(order.lines)} line(s))")
        print(f"{_INFO} klm does not place orders — review it, then submit it yourself")
    finally:
        conn.close()
    return EXIT_OK


def cmd_order_list(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        orders = list_orders(conn, state=args.state)
        for order in orders:
            print(
                f"  {order.id:<28} {order.state:<20} {len(order.lines):>3} line(s)"
                f"  {order.estimated:8.2f} {order.currency or ''}"
            )
        if not orders:
            print(f"{_INFO} no orders yet")
    finally:
        conn.close()
    return EXIT_OK


def cmd_order_mark_placed(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        order = mark_placed(conn, args.order_id, total=args.total)
        print(f"{_OK} {order.id} is {order.state}")
    finally:
        conn.close()
    return EXIT_OK


def cmd_order_receive(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    partial: dict[str, int] | None = None
    if args.partial:
        partial = {}
        for item in args.partial:
            pn, _, count = item.partition(":")
            try:
                partial[pn.strip()] = int(count)
            except ValueError:
                raise SystemExit(f"klm: --partial wants PN:QTY, got {item!r}") from None

    conn = connect(paths.db, create=False)
    try:
        report = receive(conn, args.order_id, location=args.location, partial=partial)
        for klm_id, location, quantity in report.stocked:
            part = get_part(conn, klm_id)
            print(f"{_OK} +{quantity:<5} {(part.mpn if part else klm_id):<26} → {location}")
        for pn, note in report.discrepancies:
            print(f"{_WARN} {pn}: {note}")
        print(f"\n{_OK} {report.order.id} is {report.order.state}")
        print(f"{_INFO} next: klm labels print --order {report.order.id}")
    finally:
        conn.close()
    return EXIT_OK


def cmd_order_pin(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
        pin_supplier(conn, part.klm_id, args.supplier)
        if args.supplier:
            print(f"{_OK} {part.mpn} pinned to {args.supplier} — this overrides cost")
        else:
            print(f"{_OK} {part.mpn} unpinned")
    finally:
        conn.close()
    return EXIT_OK


def cmd_stock_list(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        if args.low:
            rows = low_stock(conn)
            for item, threshold in rows:
                print(f"{_WARN} {item.mpn:<26} {item.quantity:>6} < {threshold}")
            if not rows:
                print(f"{_OK} nothing below its reorder threshold")
            return EXIT_CHECK_FAILED if rows else EXIT_OK
        items = [i for i in list_stock(conn, location=args.location) if i.quantity]
        for item in items:
            counted = item.last_counted or "never counted"
            print(f"  {item.quantity:>6}  {item.mpn:<26} {item.location:<28} {counted}")
        if not items:
            print(f"{_INFO} nothing in stock")
    finally:
        conn.close()
    return EXIT_OK


def cmd_stock_adjust(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
        item = adjust(
            conn, part.klm_id, args.location, set_to=args.set_to, delta=args.delta
        )
        print(f"{_OK} {part.mpn} @ {item.location}: {item.quantity}")
    finally:
        conn.close()
    return EXIT_OK


def cmd_stock_where(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
        found = where(conn, part.klm_id)
        for item in found:
            print(f"  {item.quantity:>6}  {item.location}")
        if not found:
            print(f"{_INFO} {part.mpn} is not recorded anywhere")
    finally:
        conn.close()
    return EXIT_OK if found else EXIT_CHECK_FAILED


def cmd_stock_consume(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        args.no_stock = True
        demand = _plan(args, paths, conn)
        taken, short = consume(conn, {line.klm_id: line.gross for line in demand.lines})
        for klm_id, location, quantity in taken:
            part = get_part(conn, klm_id)
            print(f"{_OK} -{quantity:<5} {(part.mpn if part else klm_id):<26} from {location}")
        for klm_id, missing in sorted(short.items()):
            part = get_part(conn, klm_id)
            print(f"{_WARN} short {missing} of {part.mpn if part else klm_id}")
    finally:
        conn.close()
    return EXIT_CHECK_FAILED if short else EXIT_OK


def cmd_stock_threshold(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, args.part)
        set_threshold(conn, part.klm_id, args.count)
        print(f"{_OK} {part.mpn} reorders below {args.count}")
    finally:
        conn.close()
    return EXIT_OK


def cmd_labels_print(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        klm_ids: list[str] = []
        if args.order:
            order = get_order(conn, args.order)
            if order is None:
                raise SystemExit(f"klm: no order {args.order!r}")
            klm_ids += [line.klm_id for line in order.lines if line.qty_received]
        if args.location:
            klm_ids += [i.klm_id for i in list_stock(conn, location=args.location) if i.quantity]
        for reference in args.part:
            klm_ids.append(_resolve_part(conn, reference).klm_id)

        seen: dict[str, None] = {}
        for klm_id in klm_ids:
            seen.setdefault(klm_id, None)
        labels = labels_for_parts(conn, list(seen))
        if not labels:
            print(f"{_INFO} nothing to label — try --order, --location or --part")
            return EXIT_CHECK_FAILED

        target = Path(args.output)
        if args.format == "png":
            target.mkdir(parents=True, exist_ok=True)
            for label in labels:
                render_png(label, target / f"{label.short}.png", dpi=args.dpi)
            print(f"{_OK} {len(labels)} PNG(s) in {target}")
        else:
            write_pdf(labels, target)
            print(f"{_OK} {len(labels)} label(s) → {target}")
        # Q6: no barcode until a scanner exists to prove one this small reads.
        print(f"{_INFO} labels carry a short ID, not a Data Matrix — see docs/14 Q6")
    finally:
        conn.close()
    return EXIT_OK


def cmd_labels_scan(args: argparse.Namespace) -> int:
    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        matches = resolve_short_id(conn, args.short)
        for part in matches:
            print(f"{_OK} {part.klm_id}  {part.mpn}  {part.description}")
            for item in where(conn, part.klm_id):
                print(f"      {item.quantity:>6} @ {item.location}")
        if not matches:
            print(f"{_FAIL} no part has the short ID {args.short!r}")
        elif len(matches) > 1:
            print(f"{_WARN} {len(matches)} parts share this short ID — reported, not guessed")
    finally:
        conn.close()
    return EXIT_OK if len(matches) == 1 else EXIT_CHECK_FAILED


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------


def cmd_research_check(args: argparse.Namespace) -> int:
    """Read a requirement and say back what klm understood by it.

    Needs no catalog and no API key. The point is to make the hard/soft split
    visible before a research session spends money on it: a constraint that
    silently became a preference, or a section a typo dropped, is far cheaper
    to see here than in a list of candidates that all look plausible.
    """
    from klm.research.requirement import RequirementError, load_requirement

    try:
        requirement = load_requirement(Path(args.file))
    except RequirementError as exc:
        for problem in exc.problems:
            print(f"{_FAIL} {problem}")
        return EXIT_CHECK_FAILED

    if args.toml:
        print(requirement.to_toml(), end="")
        return EXIT_OK

    print(f"{_OK} {args.file} reads as a requirement")
    print()
    print(requirement.to_prompt())
    return EXIT_OK


def cmd_research_tools(args: argparse.Namespace) -> int:
    """List the tools a research session would have here, and why not others.

    Worth being able to ask before spending anything: with no TME credentials
    the agent has no `supplier_search`, and finding that out from a session
    that returned nothing useful is an expensive way to learn it.
    """
    from klm.research.tools import ResearchContext, build_toolset

    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    config = load_config(paths.config)

    # Credentials are resolved from the environment at the point of use, so an
    # adapter exists whether or not there is a key behind it. A tool that
    # cannot authenticate is worse than an absent one — the agent spends a turn
    # discovering it — so the ones without credentials are left out and named.
    built = build_adapters(config, paths.supplier_cache)
    adapters: dict[str, SupplierAdapter] = {}
    absent: list[str] = []
    for name, adapter in built.items():
        supplier = config.suppliers[name]
        if supplier.mode != "api":
            # LCSC in manual mode answers every lookup with "I don't know", by
            # design (ADR-0009). Offering that as a tool costs a turn to learn.
            absent.append(
                f"{name}: manual mode — its offers are entered by a human, so it has no tools"
            )
        elif not supplier.has_credentials():
            absent.append(f"{name}: no credentials in the environment, so its tools are absent")
        else:
            adapters[name] = adapter

    conn = connect(paths.db, create=False, read_only=True)
    try:
        toolset = build_toolset(
            ResearchContext(conn=conn, store=AssetStore(paths.assets), adapters=adapters)
        )
        if args.format == "json":
            print(json.dumps(toolset.definitions(), indent=2))
            return EXIT_OK

        for tool in toolset.tools:
            print(f"{_OK} {tool.name}")
            print(f"    {textwrap.shorten(tool.description, width=88)}")
        for reason in absent:
            print(f"{_WARN} {reason}")
        print(f"{_INFO} no tool writes to the catalog; proposals go to a review queue")
    finally:
        conn.close()
    return EXIT_OK


def cmd_research_run(args: argparse.Namespace) -> int:
    """Research one requirement, and say what it cost.

    Two connections, deliberately: the tools get a read-only one, and the
    event log gets a writable one. klm records what the agent did; the agent
    cannot record anything (docs/adr/0006).
    """
    from klm.llm.client import EXTRACTION_MODEL, AnthropicClient, LlmUnavailable
    from klm.research.agent import Limits, Step, research
    from klm.research.requirement import RequirementError, load_requirement
    from klm.research.tools import ResearchContext, build_toolset

    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)

    try:
        requirement = load_requirement(Path(args.file))
    except RequirementError as exc:
        for problem in exc.problems:
            print(f"{_FAIL} {problem}")
        return EXIT_CHECK_FAILED

    config = load_config(paths.config)
    adapters = {
        name: adapter
        for name, adapter in build_adapters(config, paths.supplier_cache).items()
        if config.suppliers[name].mode == "api" and config.suppliers[name].has_credentials()
    }

    try:
        client = AnthropicClient(effort=args.effort)
    except LlmUnavailable as exc:
        print(f"{_FAIL} {exc}")
        return EXIT_CHECK_FAILED

    reading = connect(paths.db, create=False, read_only=True)
    writing = connect(paths.db, create=False)
    try:
        toolset = build_toolset(
            ResearchContext(
                conn=reading,
                store=AssetStore(paths.assets),
                adapters=adapters,
                datasheet_cache=paths.datasheet_cache,
                # A small model for reading PDFs: narrow, mechanical, and done
                # a lot. Its spend counts against the same session ceiling.
                reader=AnthropicClient(model=EXTRACTION_MODEL, max_tokens=4000),
            )
        )
        print(f"{_INFO} {requirement.kind}: {len(toolset.tools)} tool(s), "
              f"ceiling ${args.max_spend:.2f}")

        def show(step: Step) -> None:
            detail = ", ".join(f"{k}={v!r}" for k, v in step.arguments.items())
            print(f"  {step.name}({detail})")

        transcript = research(
            requirement,
            toolset,
            client,
            limits=Limits(
                max_iterations=args.max_iterations,
                max_tokens=args.max_tokens,
                max_spend=args.max_spend,
            ),
            conn=writing,
            on_text=None if args.quiet else lambda text: print(text, end="", flush=True),
            on_step=show,
        )
    finally:
        reading.close()
        writing.close()

    print()
    if args.quiet and transcript.answer:
        print(transcript.answer)
    for line in transcript.summary():
        print(f"{_INFO} {line}")
    if not transcript.complete:
        print(f"{_WARN} this answer is partial — {transcript.stopped}")
        return EXIT_CHECK_FAILED
    print(f"{_INFO} nothing was written to the catalog; this is a proposal")
    return EXIT_OK


def cmd_research_review(args: argparse.Namespace) -> int:
    """The proposal queue: read it, approve into a draft, or reject with why."""
    from klm.services.proposals import (
        ProposalError,
        approve,
        get,
        list_proposals,
        reject,
    )

    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    conn = connect(paths.db, create=False)
    try:
        if args.reject is not None:
            if not (args.reason or "").strip():
                print(f"{_FAIL} --reject needs --reason; the log is what improves the prompt")
                return EXIT_CHECK_FAILED
            proposal = reject(conn, args.reject, args.reason)
            print(f"{_OK} rejected {proposal.mpn}: {proposal.reason}")
            return EXIT_OK

        if args.approve is not None:
            config = load_config(paths.config)
            proposal, report = approve(
                conn,
                AssetStore(paths.assets),
                args.approve,
                adapters=build_adapters(config, paths.supplier_cache),
            )
            print(f"{_OK} {proposal.mpn} → {report.part.klm_id} ({report.part.status})")
            for note in report.notes:
                print(f"  {note}")
            for kind, why in (report.assets.unavailable if report.assets else []):
                print(f"{_WARN} no {kind}: {why}")
            print(f"{_INFO} it is a draft — it still has to pass asset QA and lint")
            return EXIT_OK

        if args.proposal is not None:
            found = get(conn, args.proposal)
            if found is None:
                print(f"{_FAIL} no proposal {args.proposal}")
                return EXIT_CHECK_FAILED
            _show_proposal(found, as_json=args.format == "json")
            return EXIT_OK

        queue = list_proposals(conn, state=None if args.all else "pending")
        if args.format == "json":
            print(json.dumps([_json_proposal(p) for p in queue], indent=2))
            return EXIT_OK
        for proposal in queue:
            mark = {"pending": _INFO, "approved": _OK, "rejected": _FAIL}[proposal.state]
            print(f"{mark} [{proposal.id}] {proposal.summary()}")
        if not queue:
            print(f"{_INFO} nothing waiting for review")
        return EXIT_OK
    except ProposalError as exc:
        print(f"{_FAIL} {exc}")
        return EXIT_CHECK_FAILED
    finally:
        conn.close()


def _json_proposal(proposal: Any) -> dict[str, Any]:
    from dataclasses import asdict

    payload = asdict(proposal)
    payload["failing"] = [c.name for c in proposal.failing]
    return dict(payload)


def _show_proposal(proposal: Any, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(_json_proposal(proposal), indent=2))
        return

    print(f"[{proposal.id}] {proposal.mpn}  {proposal.manufacturer}  ({proposal.state})")
    if proposal.description:
        print(f"  {proposal.description}")
    if proposal.why:
        print(f"  why: {proposal.why}")
    for check in proposal.checks:
        mark = {"PASS": _OK, "FAIL": _FAIL}.get(check.status, _WARN)
        print(f"  {mark} {check.name}: needs {check.required}, has {check.actual}")
    for parameter in proposal.parameters:
        where = f"p.{parameter.page}" if parameter.page else "no page"
        print(f"      {parameter.name} = {parameter.value}  [{where}: {parameter.quote!r}]")
    for offer in proposal.offers:
        price = f"{offer.unit_price} {offer.currency}" if offer.unit_price else "no price"
        print(f"  buy: {offer.supplier}:{offer.supplier_pn}  {price}  stock {offer.stock}")
    for concern in proposal.concerns:
        print(f"  {_WARN} {concern}")
    for note in proposal.notes:
        print(f"  {_INFO} klm: {note}")
    if proposal.reason:
        print(f"  rejected: {proposal.reason}")
    if proposal.klm_id:
        print(f"  approved as {proposal.klm_id}")


def _datasheet_url(args: argparse.Namespace, paths: Paths) -> tuple[str | None, str | None]:
    """`(url, title)` for a URL, or for a part that carries one.

    A part with no datasheet URL is a checked condition, not a klm error —
    the answer is "this part has none", which the caller reports and exits 1.
    """
    target = args.target
    if target.lower().startswith(("http://", "https://")):
        return target, None
    conn = connect(paths.db, create=False)
    try:
        part = _resolve_part(conn, target)
        if not part.datasheet_url:
            print(f"{_FAIL} {part.mpn} has no datasheet URL — set one, or pass a URL")
            return None, None
        return part.datasheet_url, part.mpn
    finally:
        conn.close()


def cmd_datasheet_fetch(args: argparse.Namespace) -> int:
    from klm.services.datasheets import DatasheetError, fetch

    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    url, _ = _datasheet_url(args, paths)
    if url is None:
        return EXIT_CHECK_FAILED

    try:
        datasheet = fetch(url, paths.datasheet_cache, refresh=args.refresh)
    except DatasheetError as exc:
        print(f"{_FAIL} {exc}")
        return EXIT_CHECK_FAILED

    print(f"{_OK} {datasheet.sha}  {datasheet.size / 1000:.0f} kB")
    print(f"      {datasheet.path}")
    return EXIT_OK


def cmd_datasheet_extract(args: argparse.Namespace) -> int:
    """Read parameters out of a datasheet — with the page and the quote.

    The same code path the agent's `datasheet_extract` tool uses, so a human
    can check what the agent would be told before trusting a proposal built on
    it. A parameter the datasheet does not actually state is *reported as
    dropped*, never returned as a value.
    """
    from klm.llm.client import EXTRACTION_MODEL, AnthropicClient, LlmUnavailable
    from klm.services.datasheets import DatasheetError, extract, fetch

    paths = Paths.resolve(args.catalog)
    _require_catalog(paths)
    url, title = _datasheet_url(args, paths)
    if url is None:
        return EXIT_CHECK_FAILED

    try:
        client = AnthropicClient(model=EXTRACTION_MODEL, max_tokens=4000)
    except LlmUnavailable as exc:
        print(f"{_FAIL} {exc}")
        return EXIT_CHECK_FAILED

    try:
        datasheet = fetch(url, paths.datasheet_cache)
    except DatasheetError as exc:
        print(f"{_FAIL} {exc}")
        return EXIT_CHECK_FAILED

    result = extract(datasheet, args.parameter, client, title=title)

    if args.format == "json":
        print(json.dumps({
            "datasheet": datasheet.sha,
            "parameters": [
                {
                    "name": p.name,
                    "value": p.value,
                    "page": p.page,
                    "quote": p.citations[0].quote,
                }
                for p in result.parameters
            ],
            "dropped_uncited": [{"name": n, "value": v} for n, v in result.uncited],
            "not_stated": result.missing,
        }, indent=2))
        return EXIT_OK if result.parameters else EXIT_CHECK_FAILED

    for parameter in result.parameters:
        print(f"{_OK} {parameter}")
    for name, value in result.uncited:
        print(f"{_WARN} {name} = {value} — nothing cited for it, dropped")
    for name in result.missing:
        print(f"{_INFO} {name}: the datasheet does not state it")
    if result.note:
        print(f"{_WARN} {result.note}")
    return EXIT_OK if result.parameters else EXIT_CHECK_FAILED


# ---------------------------------------------------------------------------
# app / serve
# ---------------------------------------------------------------------------


def cmd_app(args: argparse.Namespace) -> int:
    """Open the window, or explain why it could not and serve instead."""
    from klm.api.desktop import WindowUnavailable, run_server, run_window

    paths = Paths.resolve(args.catalog)
    if not paths.exists():
        print(f"{_WARN} no catalog at {paths.home} — run `klm init` first")
    if args.serve:
        run_server(args.catalog, port=args.port)
        return EXIT_OK
    try:
        run_window(args.catalog, port=args.port)
    except WindowUnavailable as exc:
        # A missing webview is a degradation, not a failure: the same UI is one
        # command away, and saying so beats a traceback (docs/adr/0012).
        print(f"{_WARN} {exc}")
        print(f"{_INFO} falling back to the browser")
        run_server(args.catalog, port=args.port)
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    from klm.api.desktop import run_server

    run_server(args.catalog, port=args.port)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
