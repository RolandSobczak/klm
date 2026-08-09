"""Tests for ``klm sync`` — status, pull, push, promote and adopt.

The three-hash comparison is the thing worth testing hardest: every state has to
be reachable, and each has to be distinguishable from the others. A sync tool
that reports `conflict` as `global-ahead` silently discards local work, and no
amount of careful pulling downstream recovers it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.projects import RESISTOR_ID, SYMBOL_ASSET, make_project, seed_resistor

from klm.kicad import symbols as sym
from klm.kicad.project import KiCadProject, find_project
from klm.kicad.sexpr import dumps_canonical, loads
from klm.model import PartStatus
from klm.services.catalog import get_part, save_part
from klm.services.lockfile import read_lock
from klm.services.sync import SyncState, adopt, promote, pull, push, sync_status
from klm.services.vendor import VendorError, vendor
from klm.store.assets import AssetKind, AssetStore

EDITED_SYMBOL = SYMBOL_ASSET.replace('(name "~") (number "1")', '(name "A") (number "1")')


def vendored(env, **kwargs) -> tuple[KiCadProject, AssetStore, sqlite3.Connection]:
    """A project already vendored from a one-part catalog."""
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project, **kwargs)
    return project, store, conn


def state_of(conn: sqlite3.Connection, project: KiCadProject, klm_id: str = RESISTOR_ID):
    row = next(r for r in sync_status(conn, project).rows if r.klm_id == klm_id)
    return row.state


def move_catalog_forward(conn: sqlite3.Connection, store: AssetStore) -> None:
    """Improve the catalog's copy, as `klm lint --fix` or a re-import would."""
    part = get_part(conn, RESISTOR_ID)
    assert part is not None
    part.symbol_hash = store.add_bytes(EDITED_SYMBOL.encode(), AssetKind.SYMBOL)
    part.updated_at = None
    save_part(conn, part)


def edit_the_project_copy(project: KiCadProject) -> None:
    """Edit a footprint the way KiCad's editor would."""
    target = project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod"
    target.write_text(target.read_text(encoding="utf-8").replace("0.56", "0.60"), encoding="utf-8")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_a_freshly_vendored_project_is_clean(env) -> None:
    project, _store, conn = vendored(env, include_3d=True)
    status = sync_status(conn, project)
    assert status.clean
    assert status.attention == 0


def test_a_catalog_change_reads_as_global_ahead(env) -> None:
    project, store, conn = vendored(env)
    move_catalog_forward(conn, store)
    assert state_of(conn, project) is SyncState.GLOBAL_AHEAD


def test_a_local_edit_reads_as_project_ahead(env) -> None:
    project, _store, conn = vendored(env)
    edit_the_project_copy(project)
    assert state_of(conn, project) is SyncState.PROJECT_AHEAD


def test_both_changing_reads_as_conflict_not_as_either_one(env) -> None:
    project, store, conn = vendored(env)
    move_catalog_forward(conn, store)
    edit_the_project_copy(project)
    assert state_of(conn, project) is SyncState.CONFLICT


def test_the_detail_names_which_asset_moved(env) -> None:
    project, store, conn = vendored(env)
    move_catalog_forward(conn, store)
    row = next(r for r in sync_status(conn, project).rows if r.klm_id == RESISTOR_ID)
    assert row.detail == "symbol changed in the catalog"


def test_a_deleted_vendored_file_reads_as_missing(env) -> None:
    project, _store, conn = vendored(env)
    (project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod").unlink()
    assert state_of(conn, project) is SyncState.MISSING


def test_a_symbol_the_lock_never_placed_reads_as_orphan(env) -> None:
    """A collaborator adding a part is the case this exists for."""
    project, _store, conn = vendored(env)
    _add_collaborator_symbol(project)

    orphans = sync_status(conn, project).select(SyncState.ORPHAN)
    assert [row.mpn for row in orphans] == ["TPS62840"]


def test_a_part_retired_upstream_is_a_warning_not_an_error(env) -> None:
    project, _store, conn = vendored(env)
    part = get_part(conn, RESISTOR_ID)
    assert part is not None
    part.status = PartStatus.DEPRECATED
    part.updated_at = None
    save_part(conn, part)

    status = sync_status(conn, project)
    assert state_of(conn, project) is SyncState.DEPRECATED_UPSTREAM
    assert status.attention == 0


def _add_collaborator_symbol(project: KiCadProject, name: str = "TPS62840") -> None:
    path = project.symbol_library("my-board")
    document = loads(path.read_text(encoding="utf-8"))
    extra = sym.extract_symbols(loads(SYMBOL_ASSET))[0]
    sym.rename_symbol(extra, name)
    sym.set_property(extra, "MPN", name)
    sym.set_property(extra, "Value", name, hidden=False)
    sym.set_property(extra, "Footprint", "my-board:R_0402_1005Metric")
    document.root.children.append(extra)
    path.write_text(dumps_canonical(document.root), encoding="utf-8")


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def test_pull_brings_the_catalog_change_in(env) -> None:
    project, store, conn = vendored(env)
    move_catalog_forward(conn, store)

    report = pull(conn, store, project)
    assert [row.klm_id for row in report.applied] == [RESISTOR_ID]
    assert report.changed
    assert sync_status(conn, project).clean
    assert '(name "A")' in project.symbol_library("my-board").read_text(encoding="utf-8")


def test_pull_refuses_a_conflict_unless_told_which_side_wins(env) -> None:
    project, store, conn = vendored(env)
    move_catalog_forward(conn, store)
    edit_the_project_copy(project)

    report = pull(conn, store, project)
    assert not report.applied
    assert "prefer-global" in report.skipped[0][1]

    forced = pull(conn, store, project, strategy="prefer-global")
    assert [row.klm_id for row in forced.applied] == [RESISTOR_ID]
    assert sync_status(conn, project).clean


def test_pull_leaves_an_orphan_symbol_alone(env) -> None:
    """Rewriting the library wholesale would delete a collaborator's work."""
    project, store, conn = vendored(env)
    _add_collaborator_symbol(project)
    move_catalog_forward(conn, store)

    pull(conn, store, project)
    text = project.symbol_library("my-board").read_text(encoding="utf-8")
    assert '"TPS62840"' in text


def test_pull_with_nothing_to_do_changes_nothing(env) -> None:
    project, store, conn = vendored(env)
    report = pull(conn, store, project)
    assert not report.applied and not report.changed


def test_pull_dry_run_writes_nothing(env) -> None:
    project, store, conn = vendored(env)
    move_catalog_forward(conn, store)
    before = project.symbol_library("my-board").read_bytes()

    report = pull(conn, store, project, dry_run=True)
    assert report.applied
    assert project.symbol_library("my-board").read_bytes() == before


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def test_push_moves_the_local_fix_into_the_catalog(env) -> None:
    project, store, conn = vendored(env)
    before = get_part(conn, RESISTOR_ID)
    assert before is not None
    edit_the_project_copy(project)

    report = push(conn, store, project)
    assert [row.klm_id for row in report.applied] == [RESISTOR_ID]

    after = get_part(conn, RESISTOR_ID)
    assert after is not None and after.footprint_hash != before.footprint_hash
    assert sync_status(conn, project).clean


def test_a_pushed_footprint_gets_its_global_model_path_back(env) -> None:
    """Storing ${KIPRJMOD} in the catalog would break every other project."""
    project, store, conn = vendored(env, include_3d=True)
    edit_the_project_copy(project)
    push(conn, store, project)

    part = get_part(conn, RESISTOR_ID)
    assert part is not None and part.footprint_hash is not None
    stored = store.read_text(part.footprint_hash, AssetKind.FOOTPRINT)
    assert "${KLM_3DMODELS}/R_0402_1005Metric.step" in stored
    assert "KIPRJMOD" not in stored


def test_push_re_runs_the_qa_gate(env) -> None:
    project, store, conn = vendored(env)
    edit_the_project_copy(project)
    report = push(conn, store, project)
    assert AssetKind.FOOTPRINT in report.qa[RESISTOR_ID]


def test_push_does_not_re_approve_or_demote_the_part(env) -> None:
    project, store, conn = vendored(env)
    edit_the_project_copy(project)
    push(conn, store, project)
    part = get_part(conn, RESISTOR_ID)
    assert part is not None and part.status is PartStatus.APPROVED


def test_push_refuses_a_conflict_unless_told(env) -> None:
    project, store, conn = vendored(env)
    move_catalog_forward(conn, store)
    edit_the_project_copy(project)

    assert not push(conn, store, project).applied
    assert push(conn, store, project, strategy="prefer-project").applied


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


def test_promote_creates_a_draft_from_a_collaborators_part(env) -> None:
    project, store, conn = vendored(env)
    _add_collaborator_symbol(project)

    report = promote(conn, store, project, "TPS62840")
    assert report.created
    part = get_part(conn, report.klm_id)
    assert part is not None
    assert part.mpn == "TPS62840"
    assert part.status is PartStatus.DRAFT, "an automatic arrival has not been looked at"


def test_promote_stamps_the_new_identity_into_the_project(env) -> None:
    """Without this the symbol reads as an orphan again and never converges."""
    project, store, conn = vendored(env)
    _add_collaborator_symbol(project)

    report = promote(conn, store, project, "TPS62840")
    status = sync_status(conn, project)
    assert not status.select(SyncState.ORPHAN)
    assert report.klm_id in project.symbol_library("my-board").read_text(encoding="utf-8")


def test_promote_adopts_the_projects_footprint(env) -> None:
    project, store, conn = vendored(env)
    _add_collaborator_symbol(project)

    report = promote(conn, store, project, "TPS62840")
    part = get_part(conn, report.klm_id)
    assert part is not None and part.footprint_hash is not None


def test_promoting_something_that_is_not_an_orphan_is_refused(env) -> None:
    project, store, conn = vendored(env)
    with pytest.raises(VendorError, match="no orphan part matches"):
        promote(conn, store, project, "RC0402FR-074K7L")


def test_promote_files_the_part_under_a_category_when_given_one(env) -> None:
    project, store, conn = vendored(env)
    _add_collaborator_symbol(project)
    report = promote(conn, store, project, "TPS62840", category="IC/Power/Regulator")
    part = get_part(conn, report.klm_id)
    assert part is not None and part.category == "IC/Power/Regulator"


# ---------------------------------------------------------------------------
# adopt
# ---------------------------------------------------------------------------


def test_adopt_rebuilds_a_lost_lock_file(env) -> None:
    project, store, conn = vendored(env, include_3d=True)
    original = read_lock(project.lock_file)
    project.lock_file.unlink()

    report = adopt(conn, store, project, library_name="my-board")
    assert report.matched == [("RC0402FR-074K7L", RESISTOR_ID)]

    rebuilt = read_lock(project.lock_file)
    before = original.by_id(RESISTOR_ID)
    after = rebuilt.by_id(RESISTOR_ID)
    assert before is not None and after is not None
    assert after.vendored_symbol_hash == before.vendored_symbol_hash
    assert after.vendored_footprint_hash == before.vendored_footprint_hash


def test_an_adopted_project_reads_as_clean(env) -> None:
    project, store, conn = vendored(env, include_3d=True)
    project.lock_file.unlink()
    adopt(conn, store, project, library_name="my-board")
    assert sync_status(conn, project).clean


def test_adopt_guesses_the_library_when_there_is_only_one(env) -> None:
    project, store, conn = vendored(env)
    project.lock_file.unlink()
    assert adopt(conn, store, project).library_name == "my-board"


def test_adopt_reports_what_it_could_not_match(env) -> None:
    project, store, conn = vendored(env)
    _add_collaborator_symbol(project)
    project.lock_file.unlink()

    report = adopt(conn, store, project, library_name="my-board")
    assert report.unmatched == ["TPS62840"]


def test_adopt_on_a_project_with_no_library_says_so(env) -> None:
    paths, conn, store = env
    project = find_project(make_project(paths.home.parent / "proj"))
    with pytest.raises(VendorError, match="no symbol library"):
        adopt(conn, store, project)


# ---------------------------------------------------------------------------
# The whole collaboration loop
# ---------------------------------------------------------------------------


def test_the_collaboration_round_trip(env, tmp_path: Path) -> None:
    """vendor → clone → collaborator adds a part → promote → clean (docs/06 §6)."""
    project, store, conn = vendored(env)

    # The collaborator has no klm: they open the project, add a symbol, commit.
    _add_collaborator_symbol(project)
    assert sync_status(conn, project).select(SyncState.ORPHAN)

    promote(conn, store, project, "TPS62840", category="IC/Power/Regulator")
    promoted = next(
        row for row in sync_status(conn, project).rows if row.mpn == "TPS62840"
    )
    assert promoted.state is SyncState.CLEAN

    # And the catalog now carries it, as a draft awaiting review.
    part = get_part(conn, promoted.klm_id)
    assert part is not None and part.status is PartStatus.DRAFT
