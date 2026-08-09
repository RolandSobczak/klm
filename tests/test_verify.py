"""Tests for clean-room verification, scaffolding and timestamp normalisation.

The point of the verify tests is the *negative* cases. A checker that passes a
good project proves little — the one that matters is whether it catches the
half-vendored project that opens perfectly on the machine that made it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.projects import make_project, seed_resistor

from klm.assets.kicad_libs import KicadLibraries
from klm.fab.timestamps import EPOCH, normalize_directory, normalize_text
from klm.kicad.project import find_project
from klm.services.scaffold import BEGIN, END, KICAD_IMAGE, apply_scaffold, plan_scaffold
from klm.services.vendor import vendor
from klm.services.verify import verify_clean_room


def vendored(env):
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    vendor(conn, store, project)
    return find_project(project.root)


def errors(report, check: str | None = None) -> list[str]:
    return [
        f.message
        for f in report.findings
        if f.severity == "error" and (check is None or f.check == check)
    ]


# ---------------------------------------------------------------------------
# The guarantee itself
# ---------------------------------------------------------------------------


def test_verify_takes_a_project_and_nothing_else() -> None:
    """The signature *is* the guarantee (ADR-0007).

    A checker that could reach the catalog would pass on the one machine where
    passing means nothing. This is a real assertion, not a comment: adding a
    connection parameter would break it.
    """
    import inspect

    parameters = inspect.signature(verify_clean_room).parameters
    assert list(parameters) == ["project", "libraries", "require_3d"]


def test_a_vendored_project_verifies_clean(env) -> None:
    project = vendored(env)
    report = verify_clean_room(project, libraries=KicadLibraries())
    assert not report.failed, errors(report)


def test_a_linked_project_fails_on_the_global_prefix(env) -> None:
    """This is the whole feature: `KLM:` in a repository is partial vendoring."""
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))

    report = verify_clean_room(project, libraries=KicadLibraries())
    assert report.failed
    assert any("only partly vendored" in message for message in errors(report))


def test_one_forgotten_sheet_is_caught(env) -> None:
    """The failure ADR-0007 was written about: vendored except for one sheet."""
    project = vendored(env)
    extra = project.root / "power.kicad_sch"
    extra.write_text(
        '(kicad_sch (version 20231120)\n'
        '  (symbol (lib_id "KLM:RC0402FR-074K7L") (uuid "z")\n'
        '    (property "Reference" "R99" (at 0 0 0))))\n',
        encoding="utf-8",
    )

    report = verify_clean_room(find_project(project.root), libraries=KicadLibraries())
    assert any("R99" in message for message in errors(report))


def test_a_symbol_missing_from_the_vendored_library_is_caught(env) -> None:
    project = vendored(env)
    library = project.symbol_library("my-board")
    library.write_text(
        library.read_text(encoding="utf-8").replace("RC0402FR-074K7L", "SOMETHING_ELSE"),
        encoding="utf-8",
    )
    report = verify_clean_room(find_project(project.root), libraries=KicadLibraries())
    assert any("is not in" in message for message in errors(report, "symbol resolution"))


def test_a_stock_kicad_symbol_resolves_without_being_vendored(env, tmp_path: Path) -> None:
    """`power:GND` ships with KiCad, so a repository need not carry it.

    Answered by looking rather than by a list of library names — which is what
    ADR-0010 deferred to this phase (docs/14 Q11).
    """
    project = vendored(env)
    stock_dir = tmp_path / "stock"
    stock_dir.mkdir()
    (stock_dir / "power.kicad_sym").write_text(
        '(kicad_symbol_lib (version 20231120) (generator "x")\n'
        '  (symbol "GND" (property "Reference" "#PWR" (at 0 0 0))))\n',
        encoding="utf-8",
    )
    sheet = project.root / "my-board.kicad_sch"
    sheet.write_text(
        sheet.read_text(encoding="utf-8").replace(
            "\n)\n",
            '\n\t(symbol\n\t\t(lib_id "power:GND")\n\t\t(uuid "g")\n'
            '\t\t(property "Reference" "#PWR01" (at 0 0 0))\n\t)\n)\n',
        ),
        encoding="utf-8",
    )

    stock = KicadLibraries(symbol_dirs=(stock_dir,))
    assert not verify_clean_room(find_project(project.root), libraries=stock).failed
    # …and without a stock KiCad it is correctly reported as unresolvable.
    assert verify_clean_room(find_project(project.root), libraries=KicadLibraries()).failed


def test_an_absolute_library_uri_fails(env) -> None:
    project = vendored(env)
    project.sym_lib_table.write_text(
        '(sym_lib_table\n  (lib (name "my-board")(type "KiCad")'
        '(uri "/home/someone/libs/my-board.kicad_sym")(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    report = verify_clean_room(find_project(project.root), libraries=KicadLibraries())
    assert any("absolute URI" in message for message in errors(report, "library tables"))


def test_an_undeclared_environment_variable_fails(env) -> None:
    """`${MY_LIBS}` is a variable a collaborator has never heard of."""
    project = vendored(env)
    project.sym_lib_table.write_text(
        '(sym_lib_table\n  (lib (name "my-board")(type "KiCad")'
        '(uri "${MY_LIBS}/my-board.kicad_sym")(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    report = verify_clean_room(find_project(project.root), libraries=KicadLibraries())
    assert any("MY_LIBS" in message for message in errors(report, "library tables"))


def test_an_absolute_path_anywhere_is_reported_with_its_line(env) -> None:
    """`/home/rsobczak/…` is the classic leak, and invisible locally."""
    project = vendored(env)
    footprint = project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod"
    footprint.write_text(
        footprint.read_text(encoding="utf-8").rstrip()[:-1]
        + '  (model "/home/someone/models/R_0402.step")\n)\n',
        encoding="utf-8",
    )

    report = verify_clean_room(find_project(project.root), libraries=KicadLibraries())
    absolute = [f for f in report.findings if f.check == "absolute paths"]
    assert absolute and absolute[0].line > 0
    assert absolute[0].file.endswith("R_0402_1005Metric.kicad_mod")


def test_a_broken_model_reference_fails_but_no_models_is_fine(env) -> None:
    """Absent 3D is the documented default; a reference to nothing is not."""
    project = vendored(env)
    report = verify_clean_room(project, libraries=KicadLibraries())
    assert not errors(report, "3D models")

    footprint = project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod"
    footprint.write_text(
        footprint.read_text(encoding="utf-8").rstrip()[:-1]
        + '  (model "${KIPRJMOD}/libraries/packages3d/gone.step")\n)\n',
        encoding="utf-8",
    )
    report = verify_clean_room(find_project(project.root), libraries=KicadLibraries())
    assert errors(report, "3D models")


def test_a_hand_edited_vendored_library_is_caught_by_the_lock(env) -> None:
    """The state where a repository and the catalog have quietly disagreed."""
    project = vendored(env)
    footprint = project.footprint_library("my-board") / "R_0402_1005Metric.kicad_mod"
    footprint.write_text(
        footprint.read_text(encoding="utf-8").replace("0.56", "0.60"), encoding="utf-8"
    )
    report = verify_clean_room(find_project(project.root), libraries=KicadLibraries())
    assert errors(report, "lock file")


def test_a_project_with_no_tables_is_reported_not_crashed(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    project = find_project(make_project(paths.home.parent / "proj"))
    report = verify_clean_room(project, libraries=KicadLibraries())
    assert "none" in report.checks["library tables"]


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def test_scaffold_writes_the_furniture(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    plan = plan_scaffold(root, preset="publish")
    apply_scaffold(plan)

    for name in (
        ".github/workflows/verify.yml",
        ".github/workflows/artifacts.yml",
        ".github/workflows/release.yml",
        ".gitignore",
        ".gitattributes",
        "klm.toml",
        "README.md",
    ):
        assert (root / name).is_file(), name


def test_the_workflow_pins_the_image_and_runs_as_root(tmp_path: Path) -> None:
    """The KiCad image ends with `USER kicad`, so checkout fails without this.

    Found by reading the official Dockerfile rather than by a first broken CI
    run (docs/14 Q11).
    """
    root = make_project(tmp_path / "proj")
    apply_scaffold(plan_scaffold(root, preset="publish"))
    workflow = (root / ".github/workflows/verify.yml").read_text(encoding="utf-8")
    assert KICAD_IMAGE in workflow
    assert "--user root" in workflow
    assert ":latest" not in workflow


def test_the_private_preset_omits_publishing(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    apply_scaffold(plan_scaffold(root, preset="private"))
    assert (root / ".github/workflows/verify.yml").is_file()
    assert not (root / ".github/workflows/release.yml").exists()


def test_check_reports_drift_and_update_fixes_it(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    apply_scaffold(plan_scaffold(root, preset="private"))
    assert not plan_scaffold(root, preset="private").drifted

    target = root / ".gitignore"
    inner = target.read_text(encoding="utf-8").replace("fp-info-cache", "something-else")
    target.write_text(inner, encoding="utf-8")

    plan = plan_scaffold(root, preset="private")
    assert plan.drifted
    apply_scaffold(plan)
    assert "fp-info-cache" in target.read_text(encoding="utf-8")


def test_edits_outside_the_markers_survive_an_update(tmp_path: Path) -> None:
    """Regeneration that eats your edits is regeneration nobody runs."""
    root = make_project(tmp_path / "proj")
    apply_scaffold(plan_scaffold(root, preset="private"))

    target = root / ".gitignore"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# mine\n/scratch/\n", encoding="utf-8"
    )
    apply_scaffold(plan_scaffold(root, preset="private"))

    after = target.read_text(encoding="utf-8")
    assert "/scratch/" in after
    assert BEGIN in after and END in after


def test_a_hand_written_file_is_appended_to_not_replaced(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    (root / ".gitignore").write_text("# written before klm existed\n*.log\n", encoding="utf-8")
    apply_scaffold(plan_scaffold(root, preset="private"))

    after = (root / ".gitignore").read_text(encoding="utf-8")
    assert "*.log" in after
    assert "fp-info-cache" in after


def test_the_readme_is_the_users_document(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    apply_scaffold(plan_scaffold(root, preset="publish"))
    (root / "README.md").write_text("# my own words\n", encoding="utf-8")

    apply_scaffold(plan_scaffold(root, preset="publish"))
    assert (root / "README.md").read_text(encoding="utf-8") == "# my own words\n"


def test_variants_flow_into_the_artifacts_matrix(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    apply_scaffold(plan_scaffold(root, preset="publish", variants=("basic", "full")))
    workflow = (root / ".github/workflows/artifacts.yml").read_text(encoding="utf-8")
    assert "variant: [basic, full]" in workflow


def test_an_unknown_preset_names_the_ones_that_exist(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="publish"):
        plan_scaffold(tmp_path, preset="nonesuch")


# ---------------------------------------------------------------------------
# Timestamp normalisation
# ---------------------------------------------------------------------------


def test_the_gerber_creation_date_is_replaced_not_blanked() -> None:
    """A well-formed value cannot upset a parser that accepted the original."""
    text = "%TF.CreationDate,2026-08-09T12:00:00+02:00*%\n"
    normalized = normalize_text(text)
    assert EPOCH in normalized
    assert "2026" not in normalized
    assert normalized.startswith("%TF.CreationDate,") and normalized.rstrip().endswith("*%")


def test_the_human_readable_gerber_comment_is_normalized_too() -> None:
    text = "G04 Created by KiCad (9.0.9) date 2026-08-09 12:00:00*\n"
    assert "2026-08-09 12:00:00" not in normalize_text(text)


def test_the_excellon_header_is_normalized() -> None:
    text = "; DRILL file {KiCad 9.0.9} date 2026-08-09 12:00:00\n"
    assert "2026" not in normalize_text(text)


def test_normalising_makes_two_exports_identical(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    stamps = ((first, "2026-08-09T12:00:00+00:00"), (second, "2026-09-01T09:30:00+00:00"))
    for directory, stamp in stamps:
        directory.mkdir()
        (directory / "board-F_Cu.gbr").write_text(
            f"%TF.CreationDate,{stamp}*%\nG04 rest*\n", encoding="utf-8"
        )
    assert normalize_directory(first) == 1
    assert normalize_directory(second) == 1
    assert (first / "board-F_Cu.gbr").read_bytes() == (second / "board-F_Cu.gbr").read_bytes()


def test_a_file_with_no_timestamp_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "board.gbr"
    target.write_text("G04 nothing dated here*\n", encoding="utf-8")
    before = target.read_bytes()
    assert normalize_directory(tmp_path) == 0
    assert target.read_bytes() == before
