"""``klm sync`` — keeping a vendored project and the catalog in step.

Everything here rests on one comparison. For each vendored part there are three
hashes: the catalog's hash *recorded at vendoring time*, the catalog's hash
*now*, and the project copy's hash *now*. Which of the two comparisons differs
says what happened, and the four answers need four different responses:

===========================  ==========================  ================
recorded vs current global   recorded vs current copy     state
===========================  ==========================  ================
same                         same                         ``clean``
differs                      same                         ``global-ahead``
same                         differs                      ``project-ahead``
differs                      differs                      ``conflict``
===========================  ==========================  ================

Collapsing those into one "changed" flag is how sync tools lose work: a pull
that silently overwrote a local footprint fix, or a push that silently reverted
a catalog improvement. klm reports and asks (docs/06 §3).

Nothing in this module merges. ``pull`` takes the catalog's version whole,
``push`` takes the project's version whole, and a conflict needs a strategy said
out loud. Automatically merging two versions of a footprint is not reliably
possible, and pretending otherwise would be worse than refusing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from difflib import unified_diff
from enum import StrEnum

from klm import ids
from klm.assets.qa import QaReport
from klm.kicad import symbols as sym
from klm.kicad.project import KiCadProject
from klm.kicad.sexpr import SExp, SExprError, dumps_canonical, loads
from klm.model import Part, PartStatus
from klm.services.assets import AssetOrigin, register_asset, run_qa
from klm.services.catalog import get_part, list_parts, save_part
from klm.services.importer import import_symbol
from klm.services.library import (
    GENERATOR,
    LibraryBuild,
    NameMap,
    build_library,
    write_if_changed,
)
from klm.services.lockfile import LockEntry, LockFile, read_lock, write_lock
from klm.services.vendor import (
    VendoredLibrary,
    VendorError,
    footprint_for_store,
    project_layout,
    read_vendored,
)
from klm.store.assets import AssetError, AssetKind, AssetStore, hash_bytes

__all__ = [
    "AdoptReport",
    "AssetDiff",
    "PartDiff",
    "PromoteReport",
    "SyncReport",
    "SyncRow",
    "SyncState",
    "SyncStatus",
    "adopt",
    "diff_part",
    "promote",
    "pull",
    "push",
    "sync_status",
]


class SyncState(StrEnum):
    CLEAN = "clean"
    GLOBAL_AHEAD = "global-ahead"
    """The catalog improved; the project can pull."""
    PROJECT_AHEAD = "project-ahead"
    """Someone edited the project copy; `sync push` moves it into the catalog."""
    CONFLICT = "conflict"
    """Both changed. Needs a decision, never an automatic merge."""
    MISSING = "missing"
    """The lock names a part the vendored library no longer contains."""
    ORPHAN = "orphan"
    """The project has a part the catalog does not — typically a collaborator's."""
    DEPRECATED_UPSTREAM = "deprecated-upstream"
    """In step, but the catalog has since retired the part. A warning, not an error."""


#: The order states are reported in: worst first, so a long list still leads
#: with what needs a decision.
STATE_ORDER = (
    SyncState.CONFLICT,
    SyncState.MISSING,
    SyncState.ORPHAN,
    SyncState.PROJECT_AHEAD,
    SyncState.GLOBAL_AHEAD,
    SyncState.DEPRECATED_UPSTREAM,
    SyncState.CLEAN,
)


@dataclass(frozen=True)
class SyncRow:
    klm_id: str
    mpn: str
    symbol_name: str | None
    state: SyncState
    detail: str = ""

    @property
    def needs_attention(self) -> bool:
        return self.state not in (SyncState.CLEAN, SyncState.DEPRECATED_UPSTREAM)


@dataclass
class SyncStatus:
    project: KiCadProject
    library_name: str
    rows: list[SyncRow] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return all(row.state is SyncState.CLEAN for row in self.rows)

    @property
    def attention(self) -> int:
        return sum(1 for row in self.rows if row.needs_attention)

    def by_state(self) -> dict[SyncState, list[SyncRow]]:
        grouped: dict[SyncState, list[SyncRow]] = {}
        for row in sorted(self.rows, key=lambda r: (r.mpn, r.klm_id)):
            grouped.setdefault(row.state, []).append(row)
        return {state: grouped[state] for state in STATE_ORDER if state in grouped}

    def select(self, *states: SyncState) -> list[SyncRow]:
        return [row for row in self.rows if row.state in states]


def sync_status(
    conn: sqlite3.Connection, project: KiCadProject
) -> SyncStatus:
    """Compare the lock, the catalog and the project copy. Changes nothing."""
    lock = read_lock(project.lock_file)
    library = read_vendored(project, lock.library_name)
    status = SyncStatus(project=project, library_name=lock.library_name)

    for entry in lock.sorted_entries():
        status.rows.append(_row(conn, entry, library))

    known = {entry.symbol_name for entry in lock.entries if entry.symbol_name}
    for name in sorted(set(library.symbols) - known):
        # A symbol in the project library that the lock never put there: a
        # collaborator added it, and `klm promote` is how it gets a catalog row.
        properties = sym.properties(library.symbols[name])
        status.rows.append(
            SyncRow(
                klm_id=properties.get("KLM_ID", ""),
                mpn=properties.get("MPN", "") or properties.get("Value", "") or name,
                symbol_name=name,
                state=SyncState.ORPHAN,
                detail="in the project library but not in the lock file",
            )
        )

    return status


def _row(conn: sqlite3.Connection, entry: LockEntry, library: VendoredLibrary) -> SyncRow:
    part = get_part(conn, entry.klm_id)
    if part is None:
        return SyncRow(
            klm_id=entry.klm_id,
            mpn=entry.mpn,
            symbol_name=entry.symbol_name,
            state=SyncState.ORPHAN,
            detail="no catalog part carries this KLM_ID",
        )

    if entry.symbol_name and entry.symbol_name not in library.symbols:
        return SyncRow(
            klm_id=entry.klm_id,
            mpn=entry.mpn,
            symbol_name=entry.symbol_name,
            state=SyncState.MISSING,
            detail=f"symbol {entry.symbol_name!r} is not in the vendored library",
        )
    if entry.footprint_name and entry.footprint_name not in library.footprints:
        return SyncRow(
            klm_id=entry.klm_id,
            mpn=entry.mpn,
            symbol_name=entry.symbol_name,
            state=SyncState.MISSING,
            detail=f"footprint {entry.footprint_name!r} is not in the vendored library",
        )

    global_moved = _describe(
        [
            ("symbol", entry.global_symbol_hash, part.symbol_hash, entry.symbol_name),
            ("footprint", entry.global_footprint_hash, part.footprint_hash, entry.footprint_name),
            ("3D model", entry.global_model3d_hash, part.model3d_hash, entry.model_name),
        ]
    )
    project_moved = _describe(
        [
            (
                "symbol",
                entry.vendored_symbol_hash,
                library.symbol_hash(entry.symbol_name),
                entry.symbol_name,
            ),
            (
                "footprint",
                entry.vendored_footprint_hash,
                library.footprint_hash(entry.footprint_name),
                entry.footprint_name,
            ),
            (
                "3D model",
                entry.vendored_model3d_hash,
                library.model_hash(entry.model_name),
                entry.model_name,
            ),
        ]
    )

    if global_moved and project_moved:
        state, detail = SyncState.CONFLICT, f"catalog: {global_moved}; project: {project_moved}"
    elif global_moved:
        state, detail = SyncState.GLOBAL_AHEAD, f"{global_moved} changed in the catalog"
    elif project_moved:
        state, detail = SyncState.PROJECT_AHEAD, f"{project_moved} edited locally"
    elif part.status is PartStatus.DEPRECATED:
        state, detail = SyncState.DEPRECATED_UPSTREAM, "the catalog has retired this part"
    else:
        state, detail = SyncState.CLEAN, ""

    return SyncRow(
        klm_id=entry.klm_id,
        mpn=entry.mpn or part.mpn,
        symbol_name=entry.symbol_name,
        state=state,
        detail=detail,
    )


def _describe(
    comparisons: list[tuple[str, str | None, str | None, str | None]],
) -> str:
    """Name the assets whose hash moved.

    A comparison against an asset the entry never had is skipped rather than
    read as a change: a part vendored without a 3D model has not lost one.
    """
    moved = [
        label
        for label, recorded, current, name in comparisons
        if name and recorded is not None and recorded != current
    ]
    return ", ".join(moved)


# ---------------------------------------------------------------------------
# pull / push
# ---------------------------------------------------------------------------


@dataclass
class SyncReport:
    """What a pull or push did, per part."""

    applied: list[SyncRow] = field(default_factory=list)
    skipped: list[tuple[SyncRow, str]] = field(default_factory=list)
    qa: dict[str, dict[AssetKind, QaReport]] = field(default_factory=dict)
    changed: bool = False


def _store_asset(
    conn: sqlite3.Connection, store: AssetStore, data: bytes, kind: AssetKind, filename: str
) -> str:
    """Put project bytes into the catalog's asset store and record where from.

    ``source`` says `project` rather than `hand-drawn`, because the difference
    matters later: this asset was reviewed as part of a board that was built,
    which is a stronger provenance than a symbol someone drew and never used.
    """
    content_hash = store.add_bytes(data, kind)
    register_asset(conn, content_hash, kind, filename=filename, source=AssetOrigin.PROJECT)
    return content_hash


def _lock_names(lock: LockFile) -> NameMap:
    """Names taken from the lock, so nothing gets renamed by being updated."""
    return NameMap(
        symbols={e.klm_id: e.symbol_name for e in lock.entries if e.symbol_name},
        footprints={e.klm_id: e.footprint_name for e in lock.entries if e.footprint_name},
        models={
            e.global_model3d_hash: e.model_name
            for e in lock.entries
            if e.global_model3d_hash and e.model_name
        },
    )


def pull(
    conn: sqlite3.Connection,
    store: AssetStore,
    project: KiCadProject,
    *,
    only: set[str] | None = None,
    strategy: str | None = None,
    dry_run: bool = False,
) -> SyncReport:
    """Bring catalog changes into the vendored project.

    Refuses a conflict unless ``strategy`` says which side wins, because the
    other side's work is unpublished and taking it silently is not recoverable.
    """
    lock = read_lock(project.lock_file)
    status = sync_status(conn, project)
    report = SyncReport()

    wanted = [SyncState.GLOBAL_AHEAD]
    if strategy == "prefer-global":
        wanted.append(SyncState.CONFLICT)
    targets = [row for row in status.select(*wanted) if only is None or row.klm_id in only]

    for row in status.select(SyncState.CONFLICT):
        if row not in targets:
            report.skipped.append((row, "conflict — pass --strategy prefer-global to overwrite"))

    if not targets:
        return report

    parts = [p for p in (get_part(conn, row.klm_id) for row in targets) if p is not None]
    layout = project_layout(project, lock.library_name, include_3d=lock.include_3d)
    build = build_library(parts, store, layout, _lock_names(lock))

    if dry_run:
        report.applied = targets
        report.changed = True
        return report

    library = read_vendored(project, lock.library_name)
    report.changed = _splice(build, library, project, lock, store)
    _update_entries(lock, build, store, parts)
    write_lock(lock, project.lock_file)
    report.applied = targets
    return report


def _splice(
    build: LibraryBuild,
    library: VendoredLibrary,
    project: KiCadProject,
    lock: LockFile,
    store: AssetStore,
) -> bool:
    """Replace just the rebuilt parts, leaving everything else in place.

    A vendored library legitimately holds symbols klm did not put there — an
    orphan a collaborator added, not yet promoted. Rewriting the whole file
    from the catalog would delete them.
    """
    symbols = dict(library.symbols)
    for built in build.parts:
        if built.symbol_name and built.symbol is not None:
            symbols[built.symbol_name] = built.symbol

    ordered = [symbols[name] for name in sorted(symbols)]
    changed = write_if_changed(
        project.symbol_library(lock.library_name),
        dumps_canonical(
            sym.make_library(ordered, generator=GENERATOR, version=build.format_version)
        ),
    )

    footprint_dir = project.footprint_library(lock.library_name)
    footprint_dir.mkdir(parents=True, exist_ok=True)
    for name, node in sorted(build.footprints().items()):
        changed |= write_if_changed(footprint_dir / f"{name}.kicad_mod", dumps_canonical(node))

    if lock.include_3d:
        project.models3d.mkdir(parents=True, exist_ok=True)
        for name, content_hash in sorted(build.models().items()):
            data = store.read(content_hash, AssetKind.MODEL3D)
            target = project.models3d / f"{name}.step"
            if not target.exists() or target.read_bytes() != data:
                target.write_bytes(data)
                changed = True

    return changed


def _update_entries(
    lock: LockFile, build: LibraryBuild, store: AssetStore, parts: list[Part]
) -> None:
    by_id = {part.klm_id: part for part in parts}
    for built in build.parts:
        entry = lock.by_id(built.klm_id)
        part = by_id.get(built.klm_id)
        if entry is None or part is None:  # pragma: no cover - targets come from the lock
            continue
        symbol_bytes = built.symbol_bytes()
        footprint_bytes = built.footprint_bytes()
        entry.mpn = part.mpn
        entry.global_symbol_hash = part.symbol_hash if built.symbol is not None else None
        entry.global_footprint_hash = part.footprint_hash if built.footprint is not None else None
        if symbol_bytes:
            entry.vendored_symbol_hash = hash_bytes(symbol_bytes, AssetKind.SYMBOL)
        if footprint_bytes:
            entry.vendored_footprint_hash = hash_bytes(footprint_bytes, AssetKind.FOOTPRINT)
        if lock.include_3d and built.model_hash:
            entry.global_model3d_hash = part.model3d_hash
            entry.vendored_model3d_hash = hash_bytes(
                store.read(built.model_hash, AssetKind.MODEL3D), AssetKind.MODEL3D
            )


def push(
    conn: sqlite3.Connection,
    store: AssetStore,
    project: KiCadProject,
    *,
    only: set[str] | None = None,
    strategy: str | None = None,
    dry_run: bool = False,
) -> SyncReport:
    """Move a locally edited asset back into the catalog.

    The part stays whatever status it already had — pushing a fix to an approved
    part's footprint is not a reason to re-approve it, but it *is* a reason to
    re-run the QA gate, which this does.
    """
    lock = read_lock(project.lock_file)
    status = sync_status(conn, project)
    library = read_vendored(project, lock.library_name)
    report = SyncReport()

    wanted = [SyncState.PROJECT_AHEAD]
    if strategy == "prefer-project":
        wanted.append(SyncState.CONFLICT)
    targets = [row for row in status.select(*wanted) if only is None or row.klm_id in only]

    for row in status.select(SyncState.CONFLICT):
        if row not in targets:
            report.skipped.append((row, "conflict — pass --strategy prefer-project to overwrite"))

    for row in targets:
        entry = lock.by_id(row.klm_id)
        part = get_part(conn, row.klm_id)
        if entry is None or part is None:  # pragma: no cover - targets come from the lock
            continue
        if dry_run:
            report.applied.append(row)
            continue
        _push_one(conn, store, part, entry, library)
        report.qa[row.klm_id] = run_qa(conn, store, get_part(conn, row.klm_id) or part)
        report.applied.append(row)
        report.changed = True

    if report.changed:
        write_lock(lock, project.lock_file)
    return report


def _push_one(
    conn: sqlite3.Connection,
    store: AssetStore,
    part: Part,
    entry: LockEntry,
    library: VendoredLibrary,
) -> None:
    if entry.symbol_name and entry.symbol_name in library.symbols:
        data = dumps_canonical(library.symbols[entry.symbol_name]).encode("utf-8")
        part.symbol_hash = _store_asset(
            conn, store, data, AssetKind.SYMBOL, f"{entry.symbol_name}.kicad_sym"
        )
        entry.global_symbol_hash = part.symbol_hash
        entry.vendored_symbol_hash = hash_bytes(data, AssetKind.SYMBOL)

    if entry.footprint_name and entry.footprint_name in library.footprints:
        raw = library.footprints[entry.footprint_name]
        data = footprint_for_store(raw, f"{entry.model_name or entry.footprint_name}.step")
        part.footprint_hash = _store_asset(
            conn, store, data, AssetKind.FOOTPRINT, f"{entry.footprint_name}.kicad_mod"
        )
        entry.global_footprint_hash = part.footprint_hash
        entry.vendored_footprint_hash = hash_bytes(raw, AssetKind.FOOTPRINT)

    part.updated_at = None
    save_part(conn, part)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetDiff:
    """One asset's drift, on one side, as text a human can read.

    ``side`` is ``catalog`` or ``project``, and the pairing is always *recorded
    versus current on that same side*. Diffing the catalog asset against the
    vendored one would be the obvious thing to show and would be noise: the
    vendored symbol was renamed and re-fielded on the way in and its footprint
    points inside the project, so the two differ permanently and by design.
    Only each side against its own recorded state means anything.
    """

    kind: AssetKind
    side: str
    name: str
    changed: bool
    before: str = ""
    after: str = ""
    unified: str = ""
    note: str = ""
    """Why there is no text, when there is none. Never left to be inferred."""


@dataclass
class PartDiff:
    row: SyncRow
    diffs: list[AssetDiff] = field(default_factory=list)

    @property
    def changed(self) -> list[AssetDiff]:
        return [d for d in self.diffs if d.changed]


def diff_part(
    conn: sqlite3.Connection, store: AssetStore, project: KiCadProject, klm_id: str
) -> PartDiff:
    """Show what moved under one vendored part, on each side separately.

    Reads only. This is what `sync status` reports, spelled out to the line.
    """
    lock = read_lock(project.lock_file)
    entry = lock.by_id(klm_id)
    if entry is None:
        raise VendorError(f"{klm_id} is not in {project.lock_file.name}")

    status = sync_status(conn, project)
    row = next(
        (r for r in status.rows if r.klm_id == klm_id),
        SyncRow(klm_id=klm_id, mpn=entry.mpn, symbol_name=entry.symbol_name,
                state=SyncState.CLEAN),
    )
    result = PartDiff(row=row)

    part = get_part(conn, klm_id)
    if part is None:
        result.diffs.append(
            AssetDiff(
                kind=AssetKind.SYMBOL, side="catalog", name=entry.symbol_name or "",
                changed=True, note="no catalog part carries this KLM_ID — `klm promote` adds one",
            )
        )
        return result

    for kind, name, recorded, current in (
        (AssetKind.SYMBOL, entry.symbol_name, entry.global_symbol_hash, part.symbol_hash),
        (
            AssetKind.FOOTPRINT,
            entry.footprint_name,
            entry.global_footprint_hash,
            part.footprint_hash,
        ),
        (AssetKind.MODEL3D, entry.model_name, entry.global_model3d_hash, part.model3d_hash),
    ):
        if not name or recorded is None:
            continue
        result.diffs.append(_catalog_diff(store, kind, name, recorded, current))

    result.diffs.extend(_project_diffs(store, project, lock, entry, part))
    return result


def _catalog_diff(
    store: AssetStore, kind: AssetKind, name: str, recorded: str, current: str | None
) -> AssetDiff:
    changed = recorded != current
    if kind is AssetKind.MODEL3D:
        return AssetDiff(
            kind=kind, side="catalog", name=name, changed=changed,
            note="STEP is binary — compared by content hash, not shown",
        )
    if current is None:
        return AssetDiff(
            kind=kind, side="catalog", name=name, changed=True,
            note=f"the catalog part no longer has a {kind.value}",
        )
    before, after = _asset_text(store, recorded, kind), _asset_text(store, current, kind)
    return _text_diff(kind, "catalog", name, before, after, changed)


def _project_diffs(
    store: AssetStore,
    project: KiCadProject,
    lock: LockFile,
    entry: LockEntry,
    part: Part,
) -> list[AssetDiff]:
    """What the project's own copy looks like against the copy klm wrote.

    The bytes klm wrote are not kept anywhere — only their hash is — so they are
    *rebuilt* through the same builder that produced them, from the catalog
    assets the lock recorded. The rebuild is then checked against the recorded
    hash, and presented as the "before" only if it matches. A reconstruction
    that does not reproduce the hash is a guess, and a guess shown as a diff is
    worse than no diff, because it invites someone to resolve a conflict that
    isn't there.
    """
    library = read_vendored(project, lock.library_name)
    snapshot = replace(
        part,
        symbol_hash=entry.global_symbol_hash,
        footprint_hash=entry.global_footprint_hash,
        model3d_hash=entry.global_model3d_hash,
    )
    layout = project_layout(project, lock.library_name, include_3d=lock.include_3d)
    rebuilt = build_library([snapshot], store, layout, _lock_names(lock)).by_id(part.klm_id)

    out: list[AssetDiff] = []
    for kind, name, recorded, current, before_bytes, after_text in (
        (
            AssetKind.SYMBOL,
            entry.symbol_name,
            entry.vendored_symbol_hash,
            library.symbol_hash(entry.symbol_name),
            rebuilt.symbol_bytes() if rebuilt else None,
            _symbol_text(library, entry.symbol_name),
        ),
        (
            AssetKind.FOOTPRINT,
            entry.footprint_name,
            entry.vendored_footprint_hash,
            library.footprint_hash(entry.footprint_name),
            rebuilt.footprint_bytes() if rebuilt else None,
            _footprint_text(library, entry.footprint_name),
        ),
    ):
        if not name or recorded is None:
            continue
        changed = recorded != current
        if after_text is None:
            out.append(
                AssetDiff(
                    kind=kind, side="project", name=name, changed=True,
                    note=f"{name!r} is no longer in the vendored library",
                )
            )
            continue
        if before_bytes is None or hash_bytes(before_bytes, kind) != recorded:
            out.append(
                AssetDiff(
                    kind=kind, side="project", name=name, changed=changed, after=after_text,
                    note="the copy klm wrote could not be reconstructed from the lock — "
                    "showing the current file only",
                )
            )
            continue
        out.append(
            _text_diff(kind, "project", name, before_bytes.decode("utf-8"), after_text, changed)
        )

    if entry.model_name and entry.vendored_model3d_hash is not None:
        out.append(
            AssetDiff(
                kind=AssetKind.MODEL3D, side="project", name=entry.model_name,
                changed=entry.vendored_model3d_hash != library.model_hash(entry.model_name),
                note="STEP is binary — compared by content hash, not shown",
            )
        )
    return out


def _text_diff(
    kind: AssetKind, side: str, name: str, before: str, after: str, changed: bool
) -> AssetDiff:
    return AssetDiff(
        kind=kind,
        side=side,
        name=name,
        changed=changed,
        before=before,
        after=after,
        unified="".join(
            unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"{name} (as vendored)" if side == "project" else f"{name} (recorded)",
                tofile=f"{name} (now)",
            )
        ),
    )


def _asset_text(store: AssetStore, content_hash: str, kind: AssetKind) -> str:
    """The asset's canonical text, or a line saying why there is none.

    Canonical rather than stored bytes so the diff shows what the *hash* saw:
    a reformat that changed no content leaves the hash alone, and a diff full of
    whitespace beside a "nothing changed" verdict is the kind of contradiction
    that makes people stop trusting the tool.
    """
    try:
        raw = store.read_text(content_hash, kind)
    except AssetError:
        return f"# {content_hash} is no longer in the asset store\n"
    try:
        return dumps_canonical(loads(raw))
    except SExprError:
        return raw


def _symbol_text(library: VendoredLibrary, name: str | None) -> str | None:
    if not name or name not in library.symbols:
        return None
    return dumps_canonical(library.symbols[name])


def _footprint_text(library: VendoredLibrary, name: str | None) -> str | None:
    if not name or name not in library.footprints:
        return None
    try:
        return dumps_canonical(loads(library.footprints[name].decode("utf-8")))
    except (SExprError, UnicodeDecodeError):  # pragma: no cover - a corrupt file
        return library.footprints[name].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


@dataclass
class PromoteReport:
    klm_id: str
    mpn: str
    symbol_name: str
    created: bool
    qa: dict[AssetKind, QaReport] = field(default_factory=dict)


def promote(
    conn: sqlite3.Connection,
    store: AssetStore,
    project: KiCadProject,
    reference: str,
    *,
    category: str | None = None,
    dry_run: bool = False,
) -> PromoteReport:
    """Create a catalog part from one the project has and the catalog does not.

    This is how a collaborator's work flows back rather than being lost. It runs
    the same validation as any other new part and lands as a ``draft``: a part
    that arrived automatically has not been looked at by a human.
    """
    lock = read_lock(project.lock_file)
    library = read_vendored(project, lock.library_name)
    status = sync_status(conn, project)

    row = _find_orphan(status, reference)
    if row is None:
        raise VendorError(f"no orphan part matches {reference!r}; see `klm sync status`")
    name = row.symbol_name
    if not name or name not in library.symbols:
        raise VendorError(f"{reference!r} has no symbol in the vendored library to promote")

    symbol = library.symbols[name]
    if dry_run:
        return PromoteReport(klm_id=row.klm_id, mpn=row.mpn, symbol_name=name, created=True)

    imported = import_symbol(
        conn, store, symbol, name, status=PartStatus.DRAFT, category=category
    )
    part = get_part(conn, imported.klm_id)
    if part is None:  # pragma: no cover - import guarantees the row
        raise VendorError(f"promoting {name!r} did not produce a part")

    entry = _promote_entry(conn, store, lock, part, row, name, library)
    _stamp_klm_id(project, lock, library, name, part.klm_id)
    entry.vendored_symbol_hash = read_vendored(project, lock.library_name).symbol_hash(name)
    write_lock(lock, project.lock_file)

    return PromoteReport(
        klm_id=part.klm_id,
        mpn=part.mpn,
        symbol_name=name,
        created=imported.created,
        qa=run_qa(conn, store, get_part(conn, part.klm_id) or part),
    )


def _find_orphan(status: SyncStatus, reference: str) -> SyncRow | None:
    orphans = status.select(SyncState.ORPHAN)
    for row in orphans:
        if reference in (row.klm_id, row.mpn, row.symbol_name):
            return row
    return None


def _promote_entry(
    conn: sqlite3.Connection,
    store: AssetStore,
    lock: LockFile,
    part: Part,
    row: SyncRow,
    name: str,
    library: VendoredLibrary,
) -> LockEntry:
    """Adopt the project's footprint into the catalog and record the part."""
    footprint_name = _footprint_of(library, name)
    if footprint_name and footprint_name in library.footprints:
        raw = library.footprints[footprint_name]
        part.footprint_hash = _store_asset(
            conn,
            store,
            footprint_for_store(raw, f"{footprint_name}.step"),
            AssetKind.FOOTPRINT,
            f"{footprint_name}.kicad_mod",
        )
        save_part(conn, part)

    entry = lock.by_id(row.klm_id) if row.klm_id else None
    if entry is None:
        entry = LockEntry(klm_id=part.klm_id)
        lock.entries.append(entry)
    entry.klm_id = part.klm_id
    entry.mpn = part.mpn
    entry.symbol_name = name
    entry.footprint_name = footprint_name
    entry.global_symbol_hash = part.symbol_hash
    entry.global_footprint_hash = part.footprint_hash
    entry.vendored_footprint_hash = library.footprint_hash(footprint_name)
    return entry


def _footprint_of(library: VendoredLibrary, symbol_name: str) -> str | None:
    """The footprint a vendored symbol points at, if the project carries it."""
    properties = sym.properties(library.symbols[symbol_name])
    reference = properties.get("Footprint", "")
    _, _, bare = reference.partition(":")
    candidate = bare or reference
    return candidate if candidate in library.footprints else None


def _stamp_klm_id(
    project: KiCadProject,
    lock: LockFile,
    library: VendoredLibrary,
    name: str,
    klm_id: str,
) -> None:
    """Write the new identity into the project copy.

    Without this the symbol has no `KLM_ID`, so it reads as an orphan again on
    the next `sync status` and nothing ever converges.
    """
    symbols = dict(library.symbols)
    sym.set_property(symbols[name], "KLM_ID", klm_id)
    ordered = [symbols[key] for key in sorted(symbols)]
    write_if_changed(
        project.symbol_library(lock.library_name),
        dumps_canonical(sym.make_library(ordered, generator=GENERATOR, version="20231120")),
    )


# ---------------------------------------------------------------------------
# adopt
# ---------------------------------------------------------------------------


@dataclass
class AdoptReport:
    library_name: str
    matched: list[tuple[str, str]] = field(default_factory=list)
    """``(symbol name, klm_id)`` for symbols that found a catalog part."""
    unmatched: list[str] = field(default_factory=list)
    written: bool = False


def adopt(
    conn: sqlite3.Connection,
    store: AssetStore,
    project: KiCadProject,
    *,
    library_name: str | None = None,
    dry_run: bool = False,
) -> AdoptReport:
    """Rebuild a lost or unmergeable lock file from what is on disk.

    Matching is by the `KLM_ID` in each vendored symbol, falling back to the MPN.
    A symbol that matches nothing is left out: it will show up as an orphan,
    which is exactly what it is.
    """
    name = library_name or _guess_library(project)
    library = read_vendored(project, name)
    if not library.symbols and not library.footprints:
        raise VendorError(f"no vendored library named {name!r} under {project.libraries}")

    approved = {part.mpn: part for part in list_parts(conn)}
    report = AdoptReport(library_name=name)
    lock = LockFile(library_name=name, include_3d=bool(library.models))

    for symbol_name in sorted(library.symbols):
        symbol = library.symbols[symbol_name]
        part = _match(conn, symbol, approved)
        if part is None:
            report.unmatched.append(symbol_name)
            continue
        footprint_name = _footprint_of(library, symbol_name)
        model_name = footprint_name if footprint_name in library.models else None
        lock.entries.append(
            LockEntry(
                klm_id=part.klm_id,
                mpn=part.mpn,
                symbol_name=symbol_name,
                footprint_name=footprint_name,
                model_name=model_name,
                global_symbol_hash=part.symbol_hash,
                global_footprint_hash=part.footprint_hash,
                global_model3d_hash=part.model3d_hash if model_name else None,
                vendored_symbol_hash=library.symbol_hash(symbol_name),
                vendored_footprint_hash=library.footprint_hash(footprint_name),
                vendored_model3d_hash=library.model_hash(model_name),
            )
        )
        report.matched.append((symbol_name, part.klm_id))

    if not dry_run:
        write_lock(lock, project.lock_file)
        report.written = True
    return report


def _guess_library(project: KiCadProject) -> str:
    if project.lock_file.exists():
        return read_lock(project.lock_file).library_name
    candidates = sorted(project.libraries.glob("*.kicad_sym")) if project.libraries.is_dir() else []
    if len(candidates) == 1:
        return candidates[0].stem
    if not candidates:
        raise VendorError(f"no symbol library found under {project.libraries}")
    names = ", ".join(p.stem for p in candidates)
    raise VendorError(f"{project.libraries} holds several libraries ({names}); name one")


def _match(conn: sqlite3.Connection, symbol: SExp, by_mpn: dict[str, Part]) -> Part | None:
    properties = sym.properties(symbol)
    klm_id = properties.get("KLM_ID", "").strip()
    if klm_id and ids.is_valid(klm_id):
        part = get_part(conn, klm_id)
        if part is not None:
            return part
    mpn = properties.get("MPN", "").strip() or properties.get("Value", "").strip()
    return by_mpn.get(mpn)
