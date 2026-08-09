"""``klm vendor`` and ``klm unvendor`` — global libraries into a project and back.

The problem, in one sentence: a working style built on global libraries and a
publishing style built on project-local ones are directly opposed, and doing
both by hand means every part exists twice and the copies diverge (docs/06 §1).

Vendoring resolves it by making the project-local copy *derived* — written from
the catalog, recorded in ``klm.lock.json``, and reversible. Three rules govern
the writing, all of them because klm is editing files that represent hours of
work it cannot regenerate:

* **Resolve, never guess.** A symbol in a klm-managed library whose part klm
  cannot identify is reported and aborts the run. ``--allow-unresolved`` is an
  explicit decision to leave the project partly linked. A symbol from a library
  klm does not manage is a different thing and is reported separately — see
  :attr:`VendorPlan.external`.
* **Touch only what is understood.** Vendoring changes ``lib_id`` nodes, the
  ``Footprint`` field and ``(model ...)`` paths. Every other node in a schematic
  or board is re-serialised byte-identically.
* **Stage, then move.** Everything is written beside the project and moved into
  place at the end, so an interrupted vendor leaves the project untouched.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from klm import ids
from klm.kicad import board as pcb
from klm.kicad import footprints as fp
from klm.kicad import schematic as sch
from klm.kicad import symbols as sym
from klm.kicad.libtable import (
    LibEntry,
    TableKind,
    load_table,
    read_entries,
    remove_entry,
    upsert_entry,
    write_table,
)
from klm.kicad.project import LIBRARIES_DIR, MODELS_DIR, KiCadProject
from klm.kicad.sexpr import Document, SExp, dumps, dumps_canonical, loads
from klm.model import Part, PartStatus
from klm.services.catalog import get_part, list_parts
from klm.services.generate import LIBRARY_NAME as GLOBAL_LIBRARY
from klm.services.library import (
    Layout,
    LibraryBuild,
    NameMap,
    assign_names,
    build_library,
    write_library,
)
from klm.services.lockfile import (
    LockEntry,
    LockError,
    LockFile,
    read_lock,
    render_lock,
    write_lock,
)
from klm.store.assets import AssetKind, AssetStore, hash_bytes

__all__ = [
    "PROJECT_MODEL_SUBDIR",
    "PROJECT_VAR",
    "UnvendorReport",
    "Unvendored",
    "VendorError",
    "VendorPlan",
    "VendorReport",
    "VendoredLibrary",
    "footprint_for_store",
    "plan_vendor",
    "project_layout",
    "read_vendored",
    "unvendor",
    "vendor",
]

#: KiCad expands this to the directory holding the project file. Everything a
#: vendored project references hangs off it, which is what makes a fresh clone
#: open with no configuration at all.
PROJECT_VAR = "KIPRJMOD"
PROJECT_MODEL_SUBDIR = f"{LIBRARIES_DIR}/{MODELS_DIR}"

_DESCR = "vendored by klm"
_STAGING = ".klm-vendor"
_BACKUP_SUFFIX = ".klm-bak"


class VendorError(Exception):
    """Raised when vendoring cannot proceed without guessing."""


def project_layout(project: KiCadProject, library_name: str, *, include_3d: bool) -> Layout:
    """Where a vendored library lands, and what its paths refer to."""
    return Layout(
        symbol_file=project.symbol_library(library_name),
        footprint_dir=project.footprint_library(library_name),
        model_dir=project.models3d,
        footprint_lib=library_name,
        include_models=include_3d,
        model_env_var=PROJECT_VAR,
        model_subdir=PROJECT_MODEL_SUBDIR,
    )


def _staged_layout(staging: Path, project: KiCadProject, name: str, *, include_3d: bool) -> Layout:
    """The same layout, rooted in the staging directory.

    Only the destinations move; ``model_env_var`` and ``model_subdir`` describe
    file *contents*, so staged files are already correct for their final home.
    """
    libraries = staging / LIBRARIES_DIR
    return Layout(
        symbol_file=libraries / f"{name}.kicad_sym",
        footprint_dir=libraries / f"{name}.pretty",
        model_dir=libraries / MODELS_DIR,
        footprint_lib=name,
        include_models=include_3d,
        model_env_var=PROJECT_VAR,
        model_subdir=PROJECT_MODEL_SUBDIR,
    )


# ---------------------------------------------------------------------------
# Reading a project's vendored library back
# ---------------------------------------------------------------------------


@dataclass
class VendoredLibrary:
    """A project's library as it currently sits on disk.

    Read rather than assumed, because the whole point of ``sync status`` is to
    notice when it stopped matching what was written.
    """

    symbols: dict[str, SExp] = field(default_factory=dict)
    footprints: dict[str, bytes] = field(default_factory=dict)
    models: dict[str, bytes] = field(default_factory=dict)

    def symbol_hash(self, name: str | None) -> str | None:
        if not name or name not in self.symbols:
            return None
        return hash_bytes(dumps_canonical(self.symbols[name]).encode("utf-8"), AssetKind.SYMBOL)

    def footprint_hash(self, name: str | None) -> str | None:
        if not name or name not in self.footprints:
            return None
        return hash_bytes(self.footprints[name], AssetKind.FOOTPRINT)

    def model_hash(self, name: str | None) -> str | None:
        if not name or name not in self.models:
            return None
        return hash_bytes(self.models[name], AssetKind.MODEL3D)


def read_vendored(project: KiCadProject, library_name: str) -> VendoredLibrary:
    """Load the project's library files. Absent files simply yield nothing."""
    library = VendoredLibrary()

    symbol_file = project.symbol_library(library_name)
    if symbol_file.exists():
        with open(symbol_file, encoding="utf-8", newline="") as handle:
            document = loads(handle.read())
        for symbol in sym.extract_symbols(document):
            name = sym.symbol_name(symbol)
            if name is not None:
                library.symbols[name] = symbol

    footprint_dir = project.footprint_library(library_name)
    if footprint_dir.is_dir():
        for path in sorted(footprint_dir.glob("*.kicad_mod")):
            library.footprints[path.stem] = path.read_bytes()

    if project.models3d.is_dir():
        for path in sorted(project.models3d.glob("*.step")):
            library.models[path.stem] = path.read_bytes()

    return library


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unvendored:
    """A reference that did not become part of the vendored library."""

    where: str
    reference: str
    lib_id: str
    reason: str


@dataclass
class VendorPlan:
    project: KiCadProject
    library_name: str
    include_3d: bool
    parts: list[Part] = field(default_factory=list)
    names: NameMap = field(default_factory=NameMap)
    symbol_map: dict[str, str] = field(default_factory=dict)
    """Old ``lib_id`` → vendored ``lib_id``, for schematic symbols."""
    footprint_map: dict[str, str] = field(default_factory=dict)
    """Old footprint reference → vendored one, for ``Footprint`` fields and the board."""
    references: dict[str, list[str]] = field(default_factory=dict)
    footprint_only: set[str] = field(default_factory=set)
    unresolved: list[Unvendored] = field(default_factory=list)
    """References in a klm-owned library that klm cannot identify. These abort.

    A symbol whose ``lib_id`` says ``KLM:`` is one of the user's parts by
    construction, so not knowing *which* is a genuine problem: vendoring around
    it produces a project that is silently missing a part it thinks it has.
    """
    external: list[Unvendored] = field(default_factory=list)
    """References to libraries klm does not manage, left linked as they are.

    ``power:GND``, ``Device:R``, ``Connector_Generic:Conn_01x02`` — every real
    schematic is full of these, and they were never klm parts. Aborting on them
    would make the command unusable and push everyone straight to
    ``--allow-unresolved``, which would defeat the check that matters. They are
    reported instead, and ``--strict`` promotes them to errors for anyone who
    wants the harder guarantee before ``klm verify --clean-room`` exists.
    """
    previous: LockFile | None = None

    @property
    def ok(self) -> bool:
        return not self.unresolved

    def blocking(self, *, strict: bool) -> list[Unvendored]:
        return [*self.unresolved, *(self.external if strict else ())]


def plan_vendor(
    conn: sqlite3.Connection,
    store: AssetStore,
    project: KiCadProject,
    *,
    library_name: str | None = None,
    include_3d: bool = False,
    from_libraries: Sequence[str] = (),
) -> VendorPlan:
    """Work out what vendoring would produce, without touching anything.

    ``from_libraries`` names additional library nicknames whose symbols should
    be resolved against the catalog by name. It exists for the adoption case: a
    project that predates klm references its own library, and requiring every
    symbol to be re-linked to ``KLM:`` first would make vendoring unusable on an
    existing corpus. It is a flag rather than a default because resolving *any*
    nickname by name would let ``Device:R`` silently claim a catalog part called
    ``R`` — which is precisely the guessing this module refuses to do.
    """
    previous = _existing_lock(project)
    name = library_name or (previous.library_name if previous else project.name)
    name = sym.sanitize_name(name)

    approved = list_parts(conn, status=PartStatus.APPROVED)
    global_names = assign_names(approved, store)
    by_id = {part.klm_id: part for part in approved}
    symbol_owner = {n: pid for pid, n in global_names.symbols.items()}
    footprint_owner: dict[str, str] = {}
    for part_id, footprint in sorted(global_names.footprints.items()):
        footprint_owner.setdefault(footprint, part_id)

    owned = {GLOBAL_LIBRARY, name, *from_libraries}
    if previous is not None:
        owned.add(previous.library_name)

    plan = VendorPlan(
        project=project, library_name=name, include_3d=include_3d, previous=previous
    )
    resolved: dict[str, Part] = {}
    symbol_refs: dict[str, str] = {}
    footprint_refs: set[str] = set()

    for sheet in project.schematics:
        document = _load(sheet)
        for instance in sch.iter_symbol_instances(document, sheet=sheet.name):
            part = _resolve_symbol(
                conn, instance, owned, previous, symbol_owner, by_id, plan
            )
            if part is None:
                continue
            resolved[part.klm_id] = part
            symbol_refs[instance.lib_id] = part.klm_id
            plan.references.setdefault(part.klm_id, [])
            if instance.reference:
                plan.references[part.klm_id].append(instance.reference)
            if instance.footprint:
                footprint_refs.add(instance.footprint)

    if project.board is not None:
        document = _load(project.board)
        for placed in pcb.iter_footprints(document):
            part = _resolve_footprint(
                placed, owned, previous, footprint_owner, by_id, project.board.name, plan
            )
            if part is None:
                continue
            if part.klm_id not in resolved:
                # A footprint on the board with no symbol on any sheet — a
                # mounting hole or a fiducial. It is vendored without a symbol.
                resolved[part.klm_id] = part
                plan.footprint_only.add(part.klm_id)
            footprint_refs.add(placed.lib_id)

    plan.parts = sorted(resolved.values(), key=lambda p: p.klm_id)
    plan.names = _vendored_names(plan.parts, store, previous, plan.footprint_only)

    for lib_id, klm_id in sorted(symbol_refs.items()):
        target = plan.names.symbols.get(klm_id)
        if target:
            plan.symbol_map[lib_id] = sch.join_lib_id(name, target)

    for reference in sorted(footprint_refs):
        nickname, bare = sch.split_lib_id(reference)
        if nickname and nickname not in owned:
            continue
        owner = _footprint_owner(bare, previous, footprint_owner)
        target = plan.names.footprints.get(owner or "")
        if target:
            plan.footprint_map[reference] = sch.join_lib_id(name, target)

    for part in plan.parts:
        plan.references.setdefault(part.klm_id, [])
        plan.references[part.klm_id] = sorted(set(plan.references[part.klm_id]))

    return plan


def _existing_lock(project: KiCadProject) -> LockFile | None:
    if not project.lock_file.exists():
        return None
    try:
        return read_lock(project.lock_file)
    except LockError as exc:
        raise VendorError(f"{exc}; run `klm sync adopt` to rebuild it") from exc


def _vendored_names(
    parts: list[Part], store: AssetStore, previous: LockFile | None, footprint_only: set[str]
) -> NameMap:
    """Names for this build, honouring any the lock already committed to."""
    keep_symbols: dict[str, str] = {}
    keep_footprints: dict[str, str] = {}
    if previous is not None:
        for entry in previous.entries:
            if entry.symbol_name:
                keep_symbols[entry.klm_id] = entry.symbol_name
            if entry.footprint_name:
                keep_footprints[entry.klm_id] = entry.footprint_name
    names = assign_names(
        parts, store, keep_symbols=keep_symbols, keep_footprints=keep_footprints
    )
    return names.without_symbols(footprint_only)


def _resolve_symbol(
    conn: sqlite3.Connection,
    instance: sch.SymbolInstance,
    owned: set[str],
    previous: LockFile | None,
    symbol_owner: dict[str, str],
    by_id: dict[str, Part],
    plan: VendorPlan,
) -> Part | None:
    """Identify the catalog part behind a placed symbol.

    In descending order of certainty: the ``KLM_ID`` the symbol carries, the
    name the lock recorded, then the name the global library would have given
    it. Nothing below that — a name that matches nothing is reported.
    """
    if instance.library and instance.library not in owned:
        plan.external.append(
            Unvendored(
                where=instance.sheet,
                reference=instance.reference,
                lib_id=instance.lib_id,
                reason=f"library {instance.library!r} is not managed by klm",
            )
        )
        return None

    if instance.klm_id and ids.is_valid(instance.klm_id):
        part = get_part(conn, instance.klm_id)
        if part is not None:
            return part

    candidates = []
    if previous is not None:
        entry = previous.by_symbol(instance.name)
        if entry is not None:
            candidates.append(entry.klm_id)
    owner = symbol_owner.get(instance.name)
    if owner:
        candidates.append(owner)

    for klm_id in candidates:
        part = by_id.get(klm_id)
        if part is not None:
            return part

    plan.unresolved.append(
        Unvendored(
            where=instance.sheet,
            reference=instance.reference,
            lib_id=instance.lib_id,
            reason="no approved catalog part carries this symbol name",
        )
    )
    return None


def _footprint_owner(
    bare: str, previous: LockFile | None, footprint_owner: dict[str, str]
) -> str | None:
    if previous is not None:
        entry = previous.by_footprint(bare)
        if entry is not None:
            return entry.klm_id
    return footprint_owner.get(bare)


def _resolve_footprint(
    placed: pcb.FootprintInstance,
    owned: set[str],
    previous: LockFile | None,
    footprint_owner: dict[str, str],
    by_id: dict[str, Part],
    where: str,
    plan: VendorPlan,
) -> Part | None:
    if placed.library and placed.library not in owned:
        plan.external.append(
            Unvendored(
                where=where,
                reference=placed.reference,
                lib_id=placed.lib_id,
                reason=f"library {placed.library!r} is not managed by klm",
            )
        )
        return None

    klm_id = _footprint_owner(placed.name, previous, footprint_owner)
    part = by_id.get(klm_id or "")
    if part is not None:
        return part

    plan.unresolved.append(
        Unvendored(
            where=where,
            reference=placed.reference,
            lib_id=placed.lib_id,
            reason="no approved catalog part carries this footprint",
        )
    )
    return None


def _load(path: Path) -> Document:
    with open(path, encoding="utf-8", newline="") as handle:
        return loads(handle.read())


# ---------------------------------------------------------------------------
# Vendoring
# ---------------------------------------------------------------------------


@dataclass
class VendorReport:
    plan: VendorPlan
    build: LibraryBuild
    lock: LockFile
    rewritten: dict[str, int] = field(default_factory=dict)
    """Design file name → number of references rewritten."""
    changed: bool = False
    written: bool = False

    @property
    def symbols(self) -> int:
        return len(self.build.symbols())

    @property
    def footprints(self) -> int:
        return len(self.build.footprints())

    @property
    def models(self) -> int:
        return len(self.build.models())


def vendor(
    conn: sqlite3.Connection,
    store: AssetStore,
    project: KiCadProject,
    *,
    library_name: str | None = None,
    include_3d: bool = False,
    from_libraries: Sequence[str] = (),
    allow_unresolved: bool = False,
    strict: bool = False,
    dry_run: bool = False,
    timestamp: bool = True,
) -> VendorReport:
    """Make a linked project self-contained.

    Raises :class:`VendorError` rather than producing a half-vendored project,
    which is the failure mode that looks fine on the machine that made it and
    breaks for everyone else (docs/adr/0007).
    """
    plan = plan_vendor(
        conn,
        store,
        project,
        library_name=library_name,
        include_3d=include_3d,
        from_libraries=from_libraries,
    )
    blocking = plan.blocking(strict=strict)
    if blocking and not allow_unresolved:
        raise VendorError(
            f"{len(blocking)} reference(s) could not be vendored; "
            "add the parts to the catalog, name their library with --from-library, "
            "or pass --allow-unresolved to leave them linked"
        )

    staging = project.root / _STAGING
    _remove(staging)
    layout = _staged_layout(staging, project, plan.library_name, include_3d=include_3d)
    build = build_library(plan.parts, store, layout, plan.names)
    write_library(build, store, prune=True)

    lock = _build_lock(plan, build, store, timestamp=timestamp)
    report = VendorReport(plan=plan, build=build, lock=lock)

    rewrites = _plan_rewrites(project, plan)
    report.rewritten = {path.name: count for path, (count, _) in rewrites.items()}
    report.changed = (
        any(count for count, _ in rewrites.values())
        or _library_differs(staging, project, plan.library_name)
        or write_lock_would_change(lock, project.lock_file)
    )

    if dry_run:
        _remove(staging)
        return report

    try:
        _commit_libraries(project, staging, plan.library_name)
        for path, (_, text) in rewrites.items():
            _replace(path, text)
        _write_tables(project, plan.library_name)
        write_lock(lock, project.lock_file)
        _record_mode(conn, project, "vendored")
    finally:
        _remove(staging)

    report.written = True
    return report


def write_lock_would_change(lock: LockFile, path: Path) -> bool:
    if not path.exists():
        return True
    return path.read_text(encoding="utf-8") != render_lock(lock)


def _library_differs(staging: Path, project: KiCadProject, library_name: str) -> bool:
    """Whether the staged library would land as different bytes."""
    staged = staging / LIBRARIES_DIR
    pairs = [
        (staged / f"{library_name}.kicad_sym", project.symbol_library(library_name)),
    ]
    for source, target in pairs:
        if source.exists() != target.exists():
            return True
        if source.exists() and source.read_bytes() != target.read_bytes():
            return True
    return _tree_differs(
        staged / f"{library_name}.pretty", project.footprint_library(library_name)
    ) or _tree_differs(staged / MODELS_DIR, project.models3d)


def _tree_differs(source: Path, target: Path) -> bool:
    left = {p.name: p.read_bytes() for p in source.glob("*")} if source.is_dir() else {}
    right = {p.name: p.read_bytes() for p in target.glob("*")} if target.is_dir() else {}
    return left != right


def _plan_rewrites(project: KiCadProject, plan: VendorPlan) -> dict[Path, tuple[int, str]]:
    """Rewrite every design file in memory, returning the counts and the text."""
    out: dict[Path, tuple[int, str]] = {}
    for sheet in project.schematics:
        document = _load(sheet)
        count = sch.rewrite_lib_ids(document, plan.symbol_map)
        count += sch.rewrite_footprint_fields(document, plan.footprint_map)
        out[sheet] = (count, dumps(document))
    if project.board is not None:
        document = _load(project.board)
        count = pcb.rewrite_footprint_ids(document, plan.footprint_map)
        out[project.board] = (count, dumps(document))
    return out


def _build_lock(
    plan: VendorPlan, build: LibraryBuild, store: AssetStore, *, timestamp: bool
) -> LockFile:
    """Record both hashes per part — the catalog's and the copy just written."""
    by_id = {part.klm_id: part for part in plan.parts}
    entries: list[LockEntry] = []
    for built in build.parts:
        part = by_id[built.klm_id]
        symbol_bytes = built.symbol_bytes()
        footprint_bytes = built.footprint_bytes()
        entries.append(
            LockEntry(
                klm_id=part.klm_id,
                mpn=part.mpn,
                symbol_name=built.symbol_name,
                footprint_name=built.footprint_name,
                model_name=built.model_name if plan.include_3d else None,
                global_symbol_hash=part.symbol_hash if built.symbol is not None else None,
                global_footprint_hash=part.footprint_hash if built.footprint is not None else None,
                global_model3d_hash=part.model3d_hash if plan.include_3d else None,
                vendored_symbol_hash=(
                    hash_bytes(symbol_bytes, AssetKind.SYMBOL) if symbol_bytes else None
                ),
                vendored_footprint_hash=(
                    hash_bytes(footprint_bytes, AssetKind.FOOTPRINT) if footprint_bytes else None
                ),
                vendored_model3d_hash=_model_hash(store, built.model_hash, plan.include_3d),
                references=tuple(plan.references.get(part.klm_id, ())),
            )
        )

    lock = LockFile(
        library_name=plan.library_name,
        include_3d=plan.include_3d,
        entries=entries,
    )
    if timestamp:
        # Re-vendoring an unchanged project must produce a zero-byte diff, so
        # the old stamp is kept when nothing else moved. `--no-timestamp` drops
        # it entirely, for workflows that want no wall-clock in the tree at all.
        previous = plan.previous
        lock.vendored_at = previous.vendored_at if previous and _same(previous, lock) else _now()
    return lock


def _same(previous: LockFile, current: LockFile) -> bool:
    before, after = previous.to_json(), current.to_json()
    before.pop("vendored_at", None)
    after.pop("vendored_at", None)
    before.pop("klm_version", None)
    after.pop("klm_version", None)
    return before == after


def _model_hash(store: AssetStore, content_hash: str | None, include_3d: bool) -> str | None:
    """The hash of the STEP file as written into the project.

    Models are copied byte-for-byte, so this equals the catalog hash — but it is
    computed rather than assumed, because ``sync status`` compares it against a
    file re-hashed from disk and the two have to be produced the same way.
    """
    if not include_3d or not content_hash:
        return None
    try:
        return hash_bytes(store.read(content_hash, AssetKind.MODEL3D), AssetKind.MODEL3D)
    except Exception:  # pragma: no cover - a missing model is already reported
        return None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Moving into place
# ---------------------------------------------------------------------------


def _commit_libraries(project: KiCadProject, staging: Path, library_name: str) -> None:
    staged = staging / LIBRARIES_DIR
    if not staged.is_dir():  # pragma: no cover - build always creates it
        return
    target = project.libraries
    target.mkdir(parents=True, exist_ok=True)

    symbol_file = staged / f"{library_name}.kicad_sym"
    if symbol_file.exists():
        _replace_bytes(target / symbol_file.name, symbol_file.read_bytes())

    _swap_dir(staged / f"{library_name}.pretty", target / f"{library_name}.pretty")
    _swap_dir(staged / MODELS_DIR, target / MODELS_DIR)


def _swap_dir(source: Path, target: Path) -> None:
    """Replace ``target`` with ``source``, or remove it if there is no source."""
    if not source.is_dir():
        _remove(target)
        return
    _remove(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _replace(path: Path, text: str) -> None:
    """Overwrite a design file, backing it up first."""
    _backup(path)
    tmp = path.with_name(path.name + ".klm-tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _replace_bytes(path: Path, data: bytes) -> None:
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".klm-tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + _BACKUP_SUFFIX))


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _write_tables(project: KiCadProject, library_name: str) -> None:
    """Add klm's rows to the project library tables, leaving any others alone."""
    entries = (
        (
            project.sym_lib_table,
            TableKind.SYMBOL,
            LibEntry(
                library_name,
                f"${{{PROJECT_VAR}}}/{LIBRARIES_DIR}/{library_name}.kicad_sym",
                descr=_DESCR,
            ),
        ),
        (
            project.fp_lib_table,
            TableKind.FOOTPRINT,
            LibEntry(
                library_name,
                f"${{{PROJECT_VAR}}}/{LIBRARIES_DIR}/{library_name}.pretty",
                descr=_DESCR,
            ),
        ),
    )
    for path, kind, entry in entries:
        document = load_table(path, kind)
        if upsert_entry(document, entry):
            _backup(path)
            write_table(document, path)


def _record_mode(conn: sqlite3.Connection, project: KiCadProject, mode: str) -> None:
    """Note the project in the catalog, so the desktop app can list it later."""
    conn.execute(
        """
        INSERT INTO project (path, name, mode, last_seen) VALUES (?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET name = excluded.name, mode = excluded.mode,
                                        last_seen = excluded.last_seen
        """,
        (str(project.root), project.name, mode, _now()),
    )


# ---------------------------------------------------------------------------
# Unvendoring
# ---------------------------------------------------------------------------


@dataclass
class UnvendorReport:
    project: KiCadProject
    library_name: str
    rewritten: dict[str, int] = field(default_factory=dict)
    removed: list[Path] = field(default_factory=list)
    written: bool = False


def unvendor(
    conn: sqlite3.Connection,
    store: AssetStore,
    project: KiCadProject,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> UnvendorReport:
    """Point a project back at the global libraries and delete its copies.

    Refuses when the project copy has drifted from the catalog, because that
    drift is unpublished work and deleting it is not recoverable. ``--force``
    is the way to say it was deliberate.
    """
    lock = _existing_lock(project)
    if lock is None:
        raise VendorError(f"{project.root} is not vendored (no {project.lock_file.name})")

    if not force:
        drifted = _drifted(project, lock)
        if drifted:
            names = ", ".join(sorted(drifted))
            raise VendorError(
                f"the project copy of {names} differs from the catalog; "
                "run `klm sync status`, then `klm sync push` or `klm unvendor --force`"
            )

    approved = list_parts(conn, status=PartStatus.APPROVED)
    global_names = assign_names(approved, store)

    symbol_map: dict[str, str] = {}
    footprint_map: dict[str, str] = {}
    for entry in lock.entries:
        if entry.symbol_name and entry.klm_id in global_names.symbols:
            symbol_map[sch.join_lib_id(lock.library_name, entry.symbol_name)] = sch.join_lib_id(
                GLOBAL_LIBRARY, global_names.symbols[entry.klm_id]
            )
        if entry.footprint_name and entry.klm_id in global_names.footprints:
            footprint_map[
                sch.join_lib_id(lock.library_name, entry.footprint_name)
            ] = sch.join_lib_id(GLOBAL_LIBRARY, global_names.footprints[entry.klm_id])

    report = UnvendorReport(project=project, library_name=lock.library_name)
    rewrites: dict[Path, str] = {}
    for sheet in project.schematics:
        document = _load(sheet)
        count = sch.rewrite_lib_ids(document, symbol_map)
        count += sch.rewrite_footprint_fields(document, footprint_map)
        report.rewritten[sheet.name] = count
        rewrites[sheet] = dumps(document)
    if project.board is not None:
        document = _load(project.board)
        report.rewritten[project.board.name] = pcb.rewrite_footprint_ids(document, footprint_map)
        rewrites[project.board] = dumps(document)

    report.removed = [
        project.symbol_library(lock.library_name),
        project.footprint_library(lock.library_name),
        project.models3d,
        project.lock_file,
    ]

    if dry_run:
        return report

    for path, text in rewrites.items():
        _replace(path, text)
    for path in report.removed:
        _remove(path)
    if project.libraries.is_dir() and not any(project.libraries.iterdir()):
        project.libraries.rmdir()
    _drop_tables(project, lock.library_name)
    _record_mode(conn, project, "linked")

    report.written = True
    return report


def _drifted(project: KiCadProject, lock: LockFile) -> set[str]:
    """Parts whose vendored copy no longer hashes to what the lock recorded."""
    library = read_vendored(project, lock.library_name)
    out: set[str] = set()
    for entry in lock.entries:
        symbol_now = library.symbol_hash(entry.symbol_name)
        footprint_now = library.footprint_hash(entry.footprint_name)
        if entry.symbol_name and symbol_now != entry.vendored_symbol_hash:
            out.add(entry.mpn or entry.klm_id)
        if entry.footprint_name and footprint_now != entry.vendored_footprint_hash:
            out.add(entry.mpn or entry.klm_id)
    return out


def _drop_tables(project: KiCadProject, library_name: str) -> None:
    """Remove klm's rows, and the table itself only if nothing else is in it."""
    for path, kind in (
        (project.sym_lib_table, TableKind.SYMBOL),
        (project.fp_lib_table, TableKind.FOOTPRINT),
    ):
        if not path.exists():
            continue
        document = load_table(path, kind)
        if not remove_entry(document, library_name):
            continue
        _backup(path)
        if read_entries(document):
            write_table(document, path)
        else:
            path.unlink()


def footprint_for_store(data: bytes, filename: str) -> bytes:
    """A vendored footprint, made fit for the catalog again.

    The project copy points at ``${KIPRJMOD}/libraries/packages3d/…``. Storing
    that as a catalog asset would hand every other project a path that resolves
    only inside this one, so it goes back to the global variable on the way in.
    """
    document = loads(data.decode("utf-8"))
    fp.rewrite_model_paths(document.root, filename=filename)
    return dumps_canonical(document.root).encode("utf-8")
