"""Tests for project discovery, vendoring and unvendoring.

The properties that matter here are the ones a bug would make invisible: that a
re-vendor with nothing changed produces identical bytes, that only the nodes klm
understands are touched, and that a reference klm cannot resolve stops the run
instead of being guessed at.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.projects import (
    BOARD,
    MOUNT_ID,
    RESISTOR_ID,
    make_project,
    schematic,
    seed_mount,
    seed_resistor,
)

from klm.kicad import board as pcb
from klm.kicad import schematic as sch
from klm.kicad.project import ProjectError, find_project
from klm.kicad.sexpr import dumps, loads
from klm.model import PartStatus
from klm.services.lockfile import (
    LockEntry,
    LockError,
    LockFile,
    read_lock,
    render_lock,
    write_lock,
)
from klm.services.vendor import (
    VendorError,
    plan_vendor,
    read_vendored,
    unvendor,
    vendor,
)

# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------


def test_find_project_locates_the_pieces(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj", board=True)
    project = find_project(root)
    assert project.name == "my-board"
    assert project.pro_file is not None
    assert [p.name for p in project.schematics] == ["my-board.kicad_sch"]
    assert project.board is not None and project.board.name == "my-board.kicad_pcb"
    assert not project.is_vendored


def test_find_project_refuses_a_directory_with_two_projects(tmp_path: Path) -> None:
    """Picking one would silently vendor the wrong schematic."""
    root = make_project(tmp_path / "proj")
    (root / "other.kicad_pro").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="more than one project"):
        find_project(root)


def test_find_project_skips_kicad_backup_directories(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    backups = root / "my-board-backups"
    backups.mkdir()
    (backups / "my-board.kicad_sch").write_text(schematic(), encoding="utf-8")
    assert len(find_project(root).schematics) == 1


def test_find_project_rejects_a_directory_that_is_not_one(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ProjectError, match="does not look like"):
        find_project(tmp_path / "empty")


# ---------------------------------------------------------------------------
# Schematic and board editing
# ---------------------------------------------------------------------------


def test_reading_a_schematic_finds_the_placed_symbol_not_the_cache() -> None:
    instances = sch.iter_symbol_instances(loads(schematic()))
    assert len(instances) == 1
    assert instances[0].reference == "R1"
    assert instances[0].library == "KLM"
    assert instances[0].klm_id == RESISTOR_ID


def test_klm_id_is_read_from_the_cache_when_the_instance_lacks_one() -> None:
    """A symbol placed before klm touched the project has only the cached copy."""
    source = schematic(klm_id=None).replace(
        '(property "Reference" "R" (at 0 0 0))',
        f'(property "Reference" "R" (at 0 0 0))\n\t\t\t'
        f'(property "KLM_ID" "{RESISTOR_ID}" (at 0 0 0))',
        1,
    )
    assert sch.iter_symbol_instances(loads(source))[0].klm_id == RESISTOR_ID


def test_rewriting_lib_ids_touches_the_cache_too() -> None:
    """A rewritten instance whose cache still says KLM: resolves to nothing."""
    document = loads(schematic())
    changed = sch.rewrite_lib_ids(document, {"KLM:RC0402FR-074K7L": "my-board:RC0402FR-074K7L"})
    assert changed == 2
    text = dumps(document)
    assert "KLM:RC0402FR-074K7L" not in text
    # The parent carries the nickname; its units carry the bare name. Prefixing
    # a unit with the parent's full new name gives `lib:Part_Part_1_1`, which
    # KiCad loads and renders as nothing.
    assert "(symbol \"my-board:RC0402FR-074K7L\"" in text
    assert "(symbol \"RC0402FR-074K7L_1_1\"" in text


def test_rewriting_a_schematic_changes_nothing_else() -> None:
    """The lossless guarantee, stated as the property vendoring depends on."""
    source = schematic()
    document = loads(source)
    sch.rewrite_lib_ids(document, {"nothing:matches": "x"})
    assert dumps(document) == source


def test_board_footprints_are_found_and_rewritten() -> None:
    document = loads(BOARD)
    placed = pcb.iter_footprints(document)
    assert [f.reference for f in placed] == ["R1", "H1"]
    assert pcb.rewrite_footprint_ids(document, {"KLM:MountingHole_3.2mm": "my-board:MH"}) == 1
    assert "my-board:MH" in dumps(document)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_plan_resolves_a_symbol_by_its_klm_id(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))

    plan = plan_vendor(conn, store, project)
    assert plan.ok
    assert [p.klm_id for p in plan.parts] == [RESISTOR_ID]
    assert plan.references[RESISTOR_ID] == ["R1"]
    assert plan.symbol_map == {"KLM:RC0402FR-074K7L": "my-board:RC0402FR-074K7L"}


def test_plan_falls_back_to_the_symbol_name_when_there_is_no_klm_id(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(paths.home.parent / "proj", sheet=schematic(klm_id=None))

    plan = plan_vendor(conn, store, find_project(root))
    assert plan.ok
    assert [p.klm_id for p in plan.parts] == [RESISTOR_ID]


def test_an_unresolvable_symbol_is_reported_never_guessed(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(
        paths.home.parent / "proj",
        sheet=schematic(lib_id="KLM:SOMETHING-ELSE", klm_id=None),
    )

    plan = plan_vendor(conn, store, find_project(root))
    assert not plan.ok
    assert plan.unresolved[0].reason == "no approved catalog part carries this symbol name"


def test_a_symbol_from_another_library_is_reported_but_does_not_block(env) -> None:
    """Every real schematic is full of `power:GND`; aborting on those is useless.

    A library klm does not manage was never a klm part, so it is left linked and
    reported. `--strict` is for anyone who wants the harder guarantee before
    `klm verify --clean-room` arrives.
    """
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(
        paths.home.parent / "proj", sheet=schematic(lib_id="Device:R", klm_id=None)
    )

    plan = plan_vendor(conn, store, find_project(root))
    assert not plan.unresolved
    assert "not managed by klm" in plan.external[0].reason
    assert not plan.blocking(strict=False)
    assert plan.blocking(strict=True)


def test_a_named_library_is_resolved_against_the_catalog(env) -> None:
    """The adoption path: a project that predates klm uses its own nickname."""
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(
        paths.home.parent / "proj",
        sheet=schematic(lib_id="Passive:RC0402FR-074K7L", klm_id=None),
    )

    plan = plan_vendor(conn, store, find_project(root), from_libraries=["Passive"])
    assert plan.ok and not plan.external
    assert [p.klm_id for p in plan.parts] == [RESISTOR_ID]


def test_a_draft_part_is_not_vendorable(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn, status=PartStatus.DRAFT)
    root = make_project(paths.home.parent / "proj", sheet=schematic(klm_id=None))

    plan = plan_vendor(conn, store, find_project(root))
    assert not plan.ok


# ---------------------------------------------------------------------------
# Vendoring
# ---------------------------------------------------------------------------


def test_vendor_makes_the_project_self_contained(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))

    report = vendor(conn, store, project, include_3d=True)
    assert report.written

    assert project.symbol_library("my-board").exists()
    assert (project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod").exists()
    assert (project.models3d / "R_0402_1005Metric.step").exists()
    assert project.is_vendored

    schematic = (project.root / "my-board.kicad_sch").read_text(encoding="utf-8")
    assert "KLM:" not in schematic
    assert "my-board:RC0402FR-074K7L" in schematic
    assert "my-board:R_0402_1005Metric" in schematic


def test_vendored_model_paths_are_project_relative(env) -> None:
    """An absolute or global-variable path is what makes a library unshareable."""
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project, include_3d=True)

    footprint = (
        project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod"
    ).read_text(encoding="utf-8")
    assert "${KIPRJMOD}/libraries/packages3d/R_0402_1005Metric.step" in footprint
    assert "KLM_3DMODELS" not in footprint


def test_vendoring_without_3d_drops_the_model_references(env) -> None:
    """A reference to a file the repository does not ship is worse than none."""
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project, include_3d=False)

    footprint = (
        project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod"
    ).read_text(encoding="utf-8")
    assert "(model" not in footprint
    assert not project.models3d.exists()


def test_re_vendoring_without_3d_removes_the_models(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project, include_3d=True)
    assert (project.models3d / "R_0402_1005Metric.step").exists()

    vendor(conn, store, project, include_3d=False)
    assert not project.models3d.exists()


def test_vendoring_twice_produces_identical_bytes(env) -> None:
    """The determinism requirement, stated the way git states it (docs/06 §5)."""
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj", board=True))
    seed_mount(store, conn)

    vendor(conn, store, project, include_3d=True)
    before = {
        path.relative_to(project.root): path.read_bytes()
        for path in sorted(project.root.rglob("*"))
        if path.is_file() and not path.name.endswith(".klm-bak")
    }

    report = vendor(conn, store, project, include_3d=True)
    after = {
        path.relative_to(project.root): path.read_bytes()
        for path in sorted(project.root.rglob("*"))
        if path.is_file() and not path.name.endswith(".klm-bak")
    }
    assert after == before
    assert not report.changed


def test_the_lock_keeps_its_timestamp_when_nothing_moved(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))

    vendor(conn, store, project)
    first = read_lock(project.lock_file).vendored_at
    vendor(conn, store, project)
    assert read_lock(project.lock_file).vendored_at == first


def test_no_timestamp_omits_it_entirely(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))

    vendor(conn, store, project, timestamp=False)
    assert read_lock(project.lock_file).vendored_at is None
    assert "vendored_at" not in project.lock_file.read_text(encoding="utf-8")


def test_the_lock_records_both_hashes(env) -> None:
    """Recording only one is what makes a sync tool unable to tell what moved."""
    paths, conn, store = env
    part = seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project, include_3d=True)

    entry = read_lock(project.lock_file).by_id(RESISTOR_ID)
    assert entry is not None
    assert entry.global_symbol_hash == part.symbol_hash
    assert entry.vendored_symbol_hash is not None
    # The vendored symbol is renamed and re-fielded, so it is deliberately not
    # the same bytes as the catalog's copy.
    assert entry.vendored_symbol_hash != entry.global_symbol_hash
    assert entry.references == ("R1",)


def test_the_vendored_hash_matches_what_is_read_back(env) -> None:
    """`sync status` compares these two; producing them differently breaks it."""
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project, include_3d=True)

    lock = read_lock(project.lock_file)
    library = read_vendored(project, "my-board")
    entry = lock.by_id(RESISTOR_ID)
    assert entry is not None
    assert library.symbol_hash(entry.symbol_name) == entry.vendored_symbol_hash
    assert library.footprint_hash(entry.footprint_name) == entry.vendored_footprint_hash
    assert library.model_hash(entry.model_name) == entry.vendored_model3d_hash


def test_a_board_only_footprint_is_vendored_without_a_symbol(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    seed_mount(store, conn)
    project = find_project(make_project(paths.home.parent / "proj", board=True))

    vendor(conn, store, project)
    entry = read_lock(project.lock_file).by_id(MOUNT_ID)
    assert entry is not None
    assert entry.symbol_name is None
    assert entry.footprint_name == "MountingHole_3.2mm"
    assert (project.footprint_library("my-board") / "MountingHole_3.2mm.kicad_mod").exists()

    board = (project.root / "my-board.kicad_pcb").read_text(encoding="utf-8")
    assert "KLM:" not in board


def test_vendor_refuses_rather_than_half_vendoring(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(
        paths.home.parent / "proj", sheet=schematic(lib_id="KLM:MISSING", klm_id=None)
    )
    project = find_project(root)

    with pytest.raises(VendorError, match="could not be vendored"):
        vendor(conn, store, project)
    assert not project.libraries.exists()
    assert not project.lock_file.exists()


def test_strict_refuses_a_symbol_from_an_unmanaged_library(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(
        paths.home.parent / "proj", sheet=schematic(lib_id="Device:R", klm_id=None)
    )

    with pytest.raises(VendorError, match="could not be vendored"):
        vendor(conn, store, find_project(root), strict=True)


def test_allow_unresolved_vendors_what_it_can(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    source = schematic() + ""
    root = make_project(paths.home.parent / "proj", sheet=source)
    # Add a second, foreign symbol beside the resolvable one.
    text = (root / "my-board.kicad_sch").read_text(encoding="utf-8")
    text = text.replace(
        "\n)\n",
        '\n\t(symbol\n\t\t(lib_id "Device:C")\n\t\t(uuid "cccc")\n'
        '\t\t(property "Reference" "C1" (at 0 0 0))\n\t)\n)\n',
    )
    (root / "my-board.kicad_sch").write_text(text, encoding="utf-8")
    project = find_project(root)

    report = vendor(conn, store, project, allow_unresolved=True)
    assert report.written
    assert len(report.plan.external) == 1
    assert project.symbol_library("my-board").exists()


def test_dry_run_writes_nothing(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))

    report = vendor(conn, store, project, dry_run=True)
    assert report.changed and not report.written
    assert not project.libraries.exists()
    assert not project.lock_file.exists()
    assert not (project.root / ".klm-vendor").exists()


def test_vendor_backs_up_theschematic(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    original = (project.root / "my-board.kicad_sch").read_text(encoding="utf-8")

    vendor(conn, store, project)
    backup = project.root / "my-board.kicad_sch.klm-bak"
    assert backup.read_text(encoding="utf-8") == original


def test_vendor_writes_project_library_tables(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project)

    table = project.sym_lib_table.read_text(encoding="utf-8")
    assert "${KIPRJMOD}/libraries/my-board.kicad_sym" in table
    assert "${KIPRJMOD}/libraries/my-board.pretty" in project.fp_lib_table.read_text(
        encoding="utf-8"
    )


def test_vendor_preserves_a_library_table_row_it_does_not_own(env) -> None:
    """The project's library table is the user's file, not klm's."""
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(paths.home.parent / "proj")
    (root / "sym-lib-table").write_text(
        '(sym_lib_table\n  (lib (name "Mine")(type "KiCad")(uri "x")(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    project = find_project(root)

    vendor(conn, store, project)
    assert '(name "Mine")' in project.sym_lib_table.read_text(encoding="utf-8")


def test_renaming_the_library_is_honoured(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))

    vendor(conn, store, project, library_name="shared-libs")
    assert project.symbol_library("shared-libs").exists()
    assert "shared-libs:RC0402FR-074K7L" in (project.root / "my-board.kicad_sch").read_text(
        encoding="utf-8"
    )


def test_a_second_vendor_keeps_the_names_the_lock_committed_to(env) -> None:
    """Adding a part must not rename the parts already in the project."""
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project)

    # Same MPN from a second manufacturer: the names now collide, and the one
    # the lock already committed to must be the one that keeps the plain name.
    seed_resistor(store, conn, klm_id="01AAAAAAAAAAAAAAAAAAAAAAAA", manufacturer="Panasonic")
    plan = plan_vendor(conn, store, project)
    assert plan.names.symbols[RESISTOR_ID] == "RC0402FR-074K7L"


# ---------------------------------------------------------------------------
# Unvendoring
# ---------------------------------------------------------------------------


def test_unvendor_is_the_exact_inverse(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj", board=True))
    seed_mount(store, conn)
    before = (project.root / "my-board.kicad_sch").read_text(encoding="utf-8")

    vendor(conn, store, project, include_3d=True)
    unvendor(conn, store, project)

    assert (project.root / "my-board.kicad_sch").read_text(encoding="utf-8") == before
    assert not project.lock_file.exists()
    assert not project.libraries.exists()
    assert not project.sym_lib_table.exists()


def test_unvendor_refuses_when_the_project_copy_has_drifted(env) -> None:
    """Local work is unpublished; deleting it is not recoverable."""
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project)

    target = project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod"
    target.write_text(target.read_text(encoding="utf-8").replace("0.56", "0.60"), encoding="utf-8")

    with pytest.raises(VendorError, match="differs from the catalog"):
        unvendor(conn, store, project)
    assert project.lock_file.exists()


def test_unvendor_force_discards_the_drift(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project)
    target = project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod"
    target.write_text(target.read_text(encoding="utf-8").replace("0.56", "0.60"), encoding="utf-8")

    unvendor(conn, store, project, force=True)
    assert not project.libraries.exists()


def test_unvendor_keeps_a_library_table_that_still_has_rows(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(paths.home.parent / "proj")
    (root / "sym-lib-table").write_text(
        '(sym_lib_table\n  (lib (name "Mine")(type "KiCad")(uri "x")(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    project = find_project(root)

    vendor(conn, store, project)
    unvendor(conn, store, project)
    assert project.sym_lib_table.exists()
    assert "my-board" not in project.sym_lib_table.read_text(encoding="utf-8")


def test_unvendor_on_a_linked_project_says_so(env) -> None:
    paths, conn, store = env
    project = find_project(make_project(paths.home.parent / "proj"))
    with pytest.raises(VendorError, match="not vendored"):
        unvendor(conn, store, project)


# ---------------------------------------------------------------------------
# The lock file itself
# ---------------------------------------------------------------------------


def test_lock_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "klm.lock.json"
    lock = LockFile(
        library_name="x",
        include_3d=True,
        entries=[LockEntry(klm_id="01", mpn="A", symbol_name="A", references=("R1",))],
    )
    write_lock(lock, path)

    restored = read_lock(path)
    assert restored.library_name == "x"
    assert restored.include_3d
    assert restored.entries[0].references == ("R1",)


def test_a_lock_from_the_future_is_refused_not_misread(tmp_path: Path) -> None:
    path = tmp_path / "klm.lock.json"
    path.write_text('{"format_version": 99, "parts": []}', encoding="utf-8")
    with pytest.raises(LockError, match="not supported"):
        read_lock(path)


def test_the_lock_is_sorted_so_merge_conflicts_are_per_part() -> None:
    lock = LockFile(
        library_name="x", entries=[LockEntry(klm_id="02"), LockEntry(klm_id="01")]
    )
    rendered = render_lock(lock)
    assert rendered.index('"01"') < rendered.index('"02"')
    assert rendered.endswith("\n")


def test_vendoring_stamps_the_identity_onto_the_schematic(env) -> None:
    """A project that predates klm carries no KLM_ID on its placed symbols.

    KiCad copies a library symbol's fields onto an instance when it is placed,
    so a board built with klm gets this free. An adopted one does not — and
    without it the BOM, ordering and cost all key on a field that is not there
    and see an empty board.
    """
    paths, conn, store = env
    seed_resistor(store, conn)
    root = make_project(paths.home.parent / "proj", sheet=schematic(klm_id=None))
    project = find_project(root)
    assert "KLM_ID" not in (project.root / "my-board.kicad_sch").read_text(encoding="utf-8")

    vendor(conn, store, project)

    text = (project.root / "my-board.kicad_sch").read_text(encoding="utf-8")
    assert RESISTOR_ID in text
    instances = sch.iter_symbol_instances(loads(text))
    assert [i.klm_id for i in instances] == [RESISTOR_ID]
    # Written the way KiCad writes one, not jammed against its neighbours.
    assert '(property "KLM_ID" ' in text


def test_stamping_is_idempotent(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(
        make_project(paths.home.parent / "proj", sheet=schematic(klm_id=None))
    )
    vendor(conn, store, project)
    first = (project.root / "my-board.kicad_sch").read_bytes()

    vendor(conn, store, project)
    assert (project.root / "my-board.kicad_sch").read_bytes() == first
