"""Tests for the BOM, the correction table, the fab profiles and the pipeline.

KiCad is not installed on the machine that runs these, which is the normal case
and the reason `KiCadCli` takes an injectable runner. A fab pipeline whose tests
skip themselves when the tool is absent is a fab pipeline nobody tests.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from tests.projects import RESISTOR_ID, make_project, schematic, seed_resistor

from klm.cad.kicadcli import CliResult, KiCadCli, KiCadCliError, KiCadCliUnavailable
from klm.config import Config
from klm.fab.profiles import GENERIC, JLCPCB, Placement, profile_for, render_csv
from klm.kicad.project import find_project
from klm.model import Part, PartStatus
from klm.services import corrections
from klm.services.bom import Variant, extract_bom, reference_sort_key
from klm.services.catalog import save_part
from klm.services.fab import fab_feedback, fab_package, parse_pos_csv, preflight
from klm.store.assets import AssetStore

# ---------------------------------------------------------------------------
# Fixtures: a two-part board
# ---------------------------------------------------------------------------

CAP_ID = "01JB4K7QW8ZR3XN5M2VYT9DCFC"

BOARD_WITH_OUTLINE = """(kicad_pcb
	(version 20231120)
	(generator "pcbnew")
	(gr_line (start 0 0) (end 40 0) (layer "Edge.Cuts"))
	(gr_line (start 40 0) (end 40 30) (layer "Edge.Cuts"))
	(gr_line (start 40 30) (end 0 30) (layer "Edge.Cuts"))
	(gr_line (start 0 30) (end 0 0) (layer "Edge.Cuts"))
	(footprint "KLM:R_0402_1005Metric"
		(layer "F.Cu")
		(at 10 10)
		(property "Reference" "R1" (at 0 0 0))
		(fp_line (start -1 -0.5) (end 1 -0.5) (layer "F.CrtYd"))
	)
)
"""

BOARD_NO_COURTYARD = BOARD_WITH_OUTLINE.replace(
    '\t\t(fp_line (start -1 -0.5) (end 1 -0.5) (layer "F.CrtYd"))\n', ""
)
BOARD_OPEN_OUTLINE = BOARD_WITH_OUTLINE.replace(
    '\t(gr_line (start 0 30) (end 0 0) (layer "Edge.Cuts"))\n', ""
)

POS_CSV = """Ref,Val,Package,PosX,PosY,Rot,Side
"R1","4.7k","R_0402_1005Metric",10.000000,-10.000000,90.000000,top
"C1","100nF","C_0402_1005Metric",20.000000,-10.000000,0.000000,bottom
"""


def two_part_schematic() -> str:
    """One resistor with a KLM_ID, one capacitor marked DNP."""
    base = schematic()
    capacitor = f"""	(symbol
		(lib_id "KLM:CC0402KRX7R9BB104")
		(at 120 100 0)
		(unit 1)
		(in_bom yes)
		(dnp yes)
		(uuid "c0c0")
		(property "Reference" "C1" (at 0 0 0))
		(property "Value" "100nF" (at 0 0 0))
		(property "Footprint" "KLM:C_0402_1005Metric" (at 0 0 0))
		(property "LCSC" "C1525" (at 0 0 0))
		(property "KLM_ID" "{CAP_ID}" (at 0 0 0))
	)
"""
    return base.replace("\n)\n", "\n" + capacitor + ")\n")


def seed_capacitor(store: AssetStore, conn: sqlite3.Connection) -> Part:
    return save_part(
        conn,
        Part(
            klm_id=CAP_ID,
            mpn="CC0402KRX7R9BB104",
            manufacturer="Yageo",
            category="Passive/Capacitor",
            package="0402",
            status=PartStatus.APPROVED,
        ),
    )


def board_project(env, *, board: str = BOARD_WITH_OUTLINE, sheet: str | None = None):
    paths, conn, store = env
    seed_resistor(store, conn)
    seed_capacitor(store, conn)
    root = make_project(paths.home.parent / "proj", sheet=sheet or two_part_schematic())
    (root / "my-board.kicad_pcb").write_text(board, encoding="utf-8")
    return find_project(root), store, conn


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------


def test_references_sort_the_way_a_person_reads_them() -> None:
    """R10 after R9. A BOM reading R1, R10, R2 looks broken to whoever checks it."""
    refs = ["R10", "R2", "R1", "C1"]
    assert sorted(refs, key=reference_sort_key) == ["C1", "R1", "R2", "R10"]


def test_bom_groups_and_honours_dnp(env) -> None:
    project, _store, conn = board_project(env)
    report = extract_bom(conn, project)

    assert [line.designators for line in report.lines] == ["R1"]
    assert [line.designators for line in report.excluded] == ["C1"]
    assert report.excluded[0].dnp, "KiCad's own flag, honoured rather than inferred"


def test_a_dnp_part_is_reported_not_dropped(env) -> None:
    """A part that silently disappears is how a board arrives unpopulated."""
    project, _store, conn = board_project(env)
    report = extract_bom(conn, project)
    assert report.excluded and report.excluded[0].value == "100nF"


def test_power_symbols_carry_no_bom_line(env) -> None:
    paths, conn, store = env
    seed_resistor(store, conn)
    sheet = schematic().replace(
        "\n)\n",
        '\n\t(symbol\n\t\t(lib_id "power:GND")\n\t\t(uuid "gg")\n'
        '\t\t(property "Reference" "#PWR01" (at 0 0 0))\n'
        '\t\t(property "Value" "GND" (at 0 0 0))\n\t)\n)\n',
    )
    project = find_project(make_project(paths.home.parent / "proj", sheet=sheet))
    assert all("#PWR01" not in line.designators for line in extract_bom(conn, project).lines)


def test_hierarchy_multiplies_the_count(env) -> None:
    """One symbol node, two instance paths, two parts on the board."""
    paths, conn, store = env
    seed_resistor(store, conn)
    sheet = schematic().replace(
        '\t\t(property "Footprint" "KLM:R_0402_1005Metric" (at 0 0 0))',
        '\t\t(property "Footprint" "KLM:R_0402_1005Metric" (at 0 0 0))\n'
        '\t\t(instances (project "p" (path "/a" (reference "R1") (unit 1))'
        ' (path "/b" (reference "R101") (unit 1))))',
    )
    project = find_project(make_project(paths.home.parent / "proj", sheet=sheet))
    report = extract_bom(conn, project)
    assert report.lines[0].quantity == 2
    assert report.lines[0].designators == "R1,R101"


def test_a_variant_depopulates_and_overrides(env) -> None:
    project, _store, conn = board_project(env)
    variant = Variant(name="basic", dnp=frozenset({"R1"}))
    report = extract_bom(conn, project, variant=variant)
    assert not report.lines
    assert {line.designators for line in report.excluded} == {"R1", "C1"}


def test_a_variant_override_changes_the_value(env) -> None:
    project, _store, conn = board_project(env)
    variant = Variant(name="full", overrides={"R1": "10k"})
    report = extract_bom(conn, project, variant=variant)
    assert report.lines[0].value == "10k"


def test_a_bom_needs_no_catalog(env) -> None:
    """The clean-room check has no catalog by definition, and still wants a BOM."""
    project, _store, _conn = board_project(env)
    report = extract_bom(None, project)
    assert report.lines and report.lines[0].klm_id is None


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_the_table_starts_empty(env) -> None:
    """ADR-0011: klm bundles no rotation data, and that is the decision."""
    _paths, conn, _store = env
    assert corrections.list_patterns(conn) == []
    assert corrections.part_corrections(conn) == []
    assert corrections.resolve(conn, klm_id=None, footprint="SOT-23-3").is_identity


def test_a_part_correction_beats_a_pattern(env) -> None:
    """Two parts on one land pattern can need different rotations (ADR-0011)."""
    _paths, conn, store = env
    seed_resistor(store, conn)
    corrections.set_pattern(conn, "^R_0402", 180.0)
    corrections.learn_part(conn, RESISTOR_ID, 90.0)

    specific = corrections.resolve(conn, klm_id=RESISTOR_ID, footprint="R_0402_1005Metric")
    generic = corrections.resolve(conn, klm_id=None, footprint="R_0402_1005Metric")
    assert specific.rotation == 90.0
    assert generic.rotation == 180.0


def test_a_symbol_field_overrides_the_pattern(env) -> None:
    _paths, conn, _store = env
    corrections.set_pattern(conn, "^SOT-23", 180.0)
    resolved = corrections.resolve(conn, klm_id=None, footprint="SOT-23-3", field_value="90")
    assert resolved.rotation == 90.0
    assert resolved.source == corrections.CorrectionSource.FIELD


def test_a_malformed_field_is_ignored_not_guessed_at(env) -> None:
    _paths, conn, _store = env
    assert corrections.resolve(conn, klm_id=None, footprint="X", field_value="sideways").is_identity


def test_a_field_can_carry_offsets_too(env) -> None:
    _paths, conn, _store = env
    resolved = corrections.resolve(conn, klm_id=None, footprint="X", field_value="180,0.1,-0.2")
    assert (resolved.rotation, resolved.offset_x, resolved.offset_y) == (180.0, 0.1, -0.2)


def test_a_learned_pattern_outranks_a_bundled_one(env) -> None:
    _paths, conn, _store = env
    corrections.set_pattern(conn, "^SOT-23", 90.0, source=corrections.CorrectionSource.BUNDLED)
    corrections.set_pattern(conn, "^SOT-23-6", 270.0, source=corrections.CorrectionSource.LEARNED)
    assert corrections.resolve(conn, klm_id=None, footprint="SOT-23-6").rotation == 270.0


def test_a_longer_pattern_wins_a_tie(env) -> None:
    _paths, conn, _store = env
    corrections.set_pattern(conn, "^SOIC-8", 90.0)
    corrections.set_pattern(conn, "^SOIC-8_3.9x4.9mm", 270.0)
    assert corrections.resolve(conn, klm_id=None, footprint="SOIC-8_3.9x4.9mm").rotation == 270.0


def test_a_broken_user_regex_does_not_break_the_export(env) -> None:
    _paths, conn, _store = env
    conn.execute(
        "INSERT INTO rotation_correction (pattern, rotation, source) VALUES ('*bad(', 90, 'user')"
    )
    assert corrections.resolve(conn, klm_id=None, footprint="R_0402").is_identity


def test_learning_twice_accumulates(env) -> None:
    """90° corrected, still 180° out, needs 270 — not the user's arithmetic to do."""
    _paths, conn, store = env
    seed_resistor(store, conn)
    corrections.learn_part(conn, RESISTOR_ID, 90.0)
    corrections.learn_part(conn, RESISTOR_ID, 180.0)
    assert corrections.resolve(conn, klm_id=RESISTOR_ID, footprint="x").rotation == 270.0


def test_confirming_records_that_a_board_proved_it(env) -> None:
    _paths, conn, store = env
    seed_resistor(store, conn)
    corrections.confirm_part(conn, RESISTOR_ID)
    resolved = corrections.resolve(conn, klm_id=RESISTOR_ID, footprint="x")
    assert resolved.confirmed and resolved.is_identity


def test_setting_a_bad_pattern_fails_loudly_at_the_time(env) -> None:
    _paths, conn, _store = env
    with pytest.raises(re.error):
        corrections.set_pattern(conn, "*bad(", 90.0)


# ---------------------------------------------------------------------------
# Profiles and formatting
# ---------------------------------------------------------------------------


def test_placement_csv_is_parsed_by_header_name() -> None:
    placements = parse_pos_csv(POS_CSV)
    assert [p.reference for p in placements] == ["R1", "C1"]
    assert placements[0].rotation == 90.0
    assert placements[1].side == "bottom"


def test_a_csv_value_containing_a_comma_does_not_shift_the_columns() -> None:
    text = render_csv(("A", "B"), [{"A": "1%, 100ppm", "B": "x"}])
    assert '"1%, 100ppm",x' in text
    assert text.endswith("\r\n")


def test_the_jlcpcb_profile_mirrors_bottom_side_angles() -> None:
    """KiCad mirrors bottom components and JLCPCB does not (ADR-0011)."""
    placement = Placement("C1", "100nF", "C_0402", 1.0, 2.0, 90.0, "bottom")
    assert JLCPCB.cpl_row(placement)["Rotation"] == "90.0000"
    assert JLCPCB.cpl_row(placement)["Layer"] == "Bottom"
    top = Placement("R1", "4k7", "R_0402", 1.0, 2.0, 90.0, "top")
    assert JLCPCB.cpl_row(top)["Rotation"] == "90.0000"


def test_bottom_mirroring_actually_changes_an_asymmetric_angle() -> None:
    placement = Placement("C1", "100nF", "C_0402", 0.0, 0.0, 45.0, "bottom")
    assert JLCPCB.cpl_row(placement)["Rotation"] == "135.0000"
    assert GENERIC.cpl_row(placement)["Rotation"] == "45.0000"


def test_profile_lookup_names_what_it_knows() -> None:
    assert profile_for("JLCPCB").name == "jlcpcb"
    with pytest.raises(KeyError, match="known: generic, jlcpcb"):
        profile_for("nonesuch")


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def fake_cli(*, erc: int = 0, drc: int = 0, message: str = "3 violations") -> KiCadCli:
    """A kicad-cli that answers without KiCad being installed."""

    def runner(args, timeout):  # type: ignore[no-untyped-def]
        joined = " ".join(str(a) for a in args)
        if " erc" in joined:
            return CliResult(args, erc, "", message if erc else "")
        if " drc" in joined:
            return CliResult(args, drc, "", message if drc else "")
        if "--version" in joined:
            return CliResult(args, 0, "9.0.1\n")
        # Every export writes something, so the pipeline has files to package.
        for index, token in enumerate(args):
            if token == "--output":
                target = Path(str(args[index + 1]))
                if target.suffix:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(POS_CSV if target.suffix == ".csv" else "x")
                else:
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "board-F_Cu.gbr").write_text("G04 gerber*")
        return CliResult(args, 0)

    return KiCadCli(Path("/fake/kicad-cli"), runner=runner)


def run_preflight(env, project, conn, store, **kwargs):
    return preflight(
        conn,
        store,
        Config(),
        project,
        bom=extract_bom(conn, project),
        cli=kwargs.pop("cli", fake_cli()),
        work_dir=project.root / "work",
        **kwargs,
    )


def test_preflight_passes_on_a_sound_board(env) -> None:
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = run_preflight(env, project, conn, store)
    assert not report.blocked, [c.name for c in report.failures]


def test_drc_violations_block(env) -> None:
    project, store, conn = board_project(env)
    report = run_preflight(env, project, conn, store, cli=fake_cli(drc=5))
    assert report.blocked
    assert any(c.name == "DRC" for c in report.failures)


def test_erc_violations_block(env) -> None:
    project, store, conn = board_project(env)
    report = run_preflight(env, project, conn, store, cli=fake_cli(erc=2))
    assert any(c.name == "ERC" for c in report.failures)


def test_a_missing_courtyard_blocks(env) -> None:
    project, store, conn = board_project(env, board=BOARD_NO_COURTYARD)
    report = run_preflight(env, project, conn, store)
    assert any("courtyard" in c.name for c in report.failures)


def test_an_open_board_outline_blocks(env) -> None:
    """The gerbers plot, the upload succeeds, and the fab asks — after ordering."""
    project, store, conn = board_project(env, board=BOARD_OPEN_OUTLINE)
    report = run_preflight(env, project, conn, store)
    assert any("outline" in c.name for c in report.failures)


def test_a_part_with_no_lcsc_number_blocks_assembly(env) -> None:
    project, store, conn = board_project(env)
    report = run_preflight(env, project, conn, store, assembly=True)
    assert any("LCSC" in c.name for c in report.failures)


def test_no_lcsc_number_is_fine_for_a_bare_board(env) -> None:
    project, store, conn = board_project(env)
    report = run_preflight(env, project, conn, store, assembly=False)
    assert not any("LCSC" in c.name for c in report.checks)


def test_a_missing_kicad_cli_is_a_blocking_check_not_a_traceback(env) -> None:
    project, store, conn = board_project(env)

    def absent(args, timeout):  # type: ignore[no-untyped-def]
        raise KiCadCliUnavailable("kicad-cli was not found")

    cli = KiCadCli(Path("/fake"), runner=absent)
    report = run_preflight(env, project, conn, store, cli=cli)
    assert report.blocked
    assert "not found" in " ".join(c.detail for c in report.failures)


# ---------------------------------------------------------------------------
# The package
# ---------------------------------------------------------------------------


def with_lcsc(project) -> None:
    """Give the resistor an LCSC number so assembly preflight passes."""
    sheet = project.root / "my-board.kicad_sch"
    sheet.write_text(
        sheet.read_text(encoding="utf-8").replace(
            '\t\t(property "Value" "4.7k" (at 100 104 0))',
            '\t\t(property "Value" "4.7k" (at 100 104 0))\n'
            '\t\t(property "LCSC" "C25900" (at 0 0 0))',
        ),
        encoding="utf-8",
    )


def test_check_only_writes_nothing(env) -> None:
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = fab_package(
        conn, store, Config(), project, cli=fake_cli(), check_only=True, allow_dirty=True
    )
    assert not report.written
    assert not (project.root / "fab").exists()


def test_a_blocked_preflight_writes_no_package(env) -> None:
    """A package that exists is a package someone will upload."""
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = fab_package(conn, store, Config(), project, cli=fake_cli(drc=3), allow_dirty=True)
    assert not report.written
    assert not (project.root / "fab").exists()


def test_the_package_has_the_files_a_fab_needs(env) -> None:
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = fab_package(conn, store, Config(), project, cli=fake_cli(), allow_dirty=True)

    assert report.written
    written = {p.name for p in report.output_dir.iterdir()}
    assert {"gerbers.zip", "drill", "bom.csv", "cpl.csv", "manifest.json", "README.txt"} <= written


def test_the_cpl_carries_only_populated_parts(env) -> None:
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = fab_package(conn, store, Config(), project, cli=fake_cli(), allow_dirty=True)

    cpl = (report.output_dir / "cpl.csv").read_text(encoding="utf-8")
    assert "R1" in cpl
    assert "C1" not in cpl, "C1 is DNP and must not be placed"


def test_corrections_are_applied_and_recorded_in_the_manifest(env) -> None:
    project, store, conn = board_project(env)
    with_lcsc(project)
    corrections.learn_part(conn, RESISTOR_ID, 180.0)

    report = fab_package(conn, store, Config(), project, cli=fake_cli(), allow_dirty=True)
    manifest = json.loads((report.output_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = next(p for p in manifest["placements"] if p["reference"] == "R1")
    assert entry["rotation"] == 270.0, "90 from the board plus the learned 180"
    assert entry["klm_id"] == RESISTOR_ID
    assert entry["confirmed"] is True


def test_an_unconfirmed_rotation_is_reported_loudly(env) -> None:
    """ADR-0011's stated cost: the first board of a package has nothing behind it."""
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = fab_package(conn, store, Config(), project, cli=fake_cli(), allow_dirty=True)

    assert "R1" in report.unconfirmed
    readme = (report.output_dir / "README.txt").read_text(encoding="utf-8")
    assert "NOT confirmed" in readme
    assert "R1" in readme


def test_the_readme_lists_what_is_not_populated(env) -> None:
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = fab_package(conn, store, Config(), project, cli=fake_cli(), allow_dirty=True)
    assert "C1" in (report.output_dir / "README.txt").read_text(encoding="utf-8")


def test_the_package_is_byte_stable_across_runs(env) -> None:
    project, store, conn = board_project(env)
    with_lcsc(project)
    kwargs = dict(cli=fake_cli(), allow_dirty=True, timestamp=False)

    first = fab_package(conn, store, Config(), project, **kwargs)
    before = {p.name: p.read_bytes() for p in sorted(first.output_dir.rglob("*")) if p.is_file()}
    second = fab_package(conn, store, Config(), project, **kwargs)
    after = {p.name: p.read_bytes() for p in sorted(second.output_dir.rglob("*")) if p.is_file()}
    assert after == before


def test_a_variant_gets_its_own_directory(env) -> None:
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = fab_package(
        conn,
        store,
        Config(),
        project,
        cli=fake_cli(),
        allow_dirty=True,
        variant=Variant(name="basic"),
    )
    assert report.output_dir.name == "my-board-basic"


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def built_package(env) -> tuple[Path, sqlite3.Connection]:
    project, store, conn = board_project(env)
    with_lcsc(project)
    report = fab_package(conn, store, Config(), project, cli=fake_cli(), allow_dirty=True)
    return report.output_dir, conn


def test_feedback_records_what_the_board_showed(env) -> None:
    package, conn = built_package(env)
    report = fab_feedback(conn, package, wrong={"R1": 180.0})

    assert [r[0] for r in report.corrected] == ["R1"]
    assert corrections.resolve(conn, klm_id=RESISTOR_ID, footprint="x").rotation == 180.0


def test_confirming_the_rest_is_the_valuable_half(env) -> None:
    """Turning "untested" into "came back right" is knowledge nothing else records."""
    package, conn = built_package(env)
    report = fab_feedback(conn, package, wrong={}, confirm_rest=True)

    assert [r[0] for r in report.confirmed] == ["R1"]
    assert corrections.resolve(conn, klm_id=RESISTOR_ID, footprint="x").confirmed


def test_feedback_generalizes_only_when_asked(env) -> None:
    """One board is one data point about one reel."""
    package, conn = built_package(env)
    fab_feedback(conn, package, wrong={"R1": 90.0})
    assert corrections.list_patterns(conn) == []

    fab_feedback(conn, package, wrong={"R1": 90.0}, generalize=True)
    assert [p for p, _ in corrections.list_patterns(conn)]


def test_feedback_names_a_reference_the_package_does_not_have(env) -> None:
    package, conn = built_package(env)
    report = fab_feedback(conn, package, wrong={"U99": 90.0})
    assert report.unknown == ["U99"]


def test_feedback_on_something_that_is_not_a_package_says_so(env, tmp_path: Path) -> None:
    _paths, conn, _store = env
    with pytest.raises(FileNotFoundError, match=r"manifest\.json"):
        fab_feedback(conn, tmp_path, wrong={})


def test_the_cli_wrapper_turns_a_failure_into_a_message() -> None:
    def failing(args, timeout):  # type: ignore[no-untyped-def]
        return CliResult(args, 1, "", "could not open board\n")

    cli = KiCadCli(Path("/fake"), runner=failing)
    with pytest.raises(KiCadCliError, match="could not open board"):
        cli.export_gerbers(Path("b.kicad_pcb"), Path("out"))


# ---------------------------------------------------------------------------
# The board outline, against what KiCad actually writes
# ---------------------------------------------------------------------------

BOARD_ROUNDED = """(kicad_pcb
	(version 20231120)
	(gr_line (start 5 0) (end 35 0) (layer "Edge.Cuts"))
	(gr_arc (start 35.000317 0.006338) (mid 38.5 1.5) (end 40 5) (layer "Edge.Cuts"))
	(gr_line (start 40 5) (end 40 30) (layer "Edge.Cuts"))
	(gr_line (start 40 30) (end 0 30) (layer "Edge.Cuts"))
	(gr_line (start 0 30) (end 0 5) (layer "Edge.Cuts"))
	(gr_arc (start 0 5) (mid 1.5 1.5) (end 5 0) (layer "Edge.Cuts"))
	(footprint "KLM:R_0402_1005Metric"
		(layer "F.Cu")
		(at 10 10)
		(property "Reference" "R1" (at 0 0 0))
		(fp_line (start -1 -0.5) (end 1 -0.5) (layer "F.CrtYd"))
	)
)
"""


def test_a_rounded_corner_does_not_read_as_an_open_outline(env) -> None:
    """KiCad's arc endpoints sit microns off the lines they meet, on real boards.

    Two failures hide here, and both were found against the author's own corpus
    rather than a fixture: taking an arc's *midpoint* for an endpoint, and
    rounding rather than clustering — 6 µm either side of a rounding boundary
    lands in different buckets however fine the grid.
    """
    project, store, conn = board_project(env, board=BOARD_ROUNDED)
    with_lcsc(project)
    report = run_preflight(env, project, conn, store)
    assert not any("outline" in c.name for c in report.failures)


def test_an_arc_reports_its_ends_not_its_midpoint() -> None:
    from klm.kicad import footprints as fp
    from klm.kicad.sexpr import loads

    arc = loads("(gr_arc (start 0 0) (mid 1 1) (end 2 0))").root
    assert fp.endpoints(arc) == ((0.0, 0.0), (2.0, 0.0))


def test_a_board_with_no_edge_cuts_at_all_is_reported(env) -> None:
    project, store, conn = board_project(
        env, board=BOARD_WITH_OUTLINE.replace("Edge.Cuts", "Dwgs.User")
    )
    report = run_preflight(env, project, conn, store)
    assert any("nothing on Edge.Cuts" in c.detail for c in report.failures)
