"""The asset pipeline: packages, templates, land patterns, the QA gate.

The fixture KiCad library under `tests/fixtures/kicad` is deliberately tiny —
two symbols, one footprint, one STEP model. It exists so the "prefer KiCad's
own libraries" branch is exercised on a machine where KiCad is not installed,
which is every CI runner and, at the time of writing, the development machine.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from klm.assets.kicad_libs import KicadLibraries, find_libraries
from klm.assets.landpattern import chip_footprint, chip_footprint_name
from klm.assets.packages import CHIP_PACKAGES, find_package, normalize_package
from klm.assets.qa import QaStatus, check_footprint, check_model3d, check_symbol
from klm.assets.templates import (
    PinSpec,
    connector_symbol,
    ic_symbol,
    passive_symbol,
    power_pin_type,
)
from klm.cad.freecad import ConversionResult, FreeCadUnavailable, convert_mesh, script_path
from klm.kicad.footprints import iter_pads
from klm.kicad.sexpr import dumps_canonical, loads
from klm.kicad.symbols import iter_pins, properties
from klm.model import Part, PartStatus
from klm.services.assets import (
    AssetOrigin,
    acquire_assets,
    register_asset,
    reuse_candidates,
    run_qa,
)
from klm.services.catalog import save_part
from klm.store.assets import AssetKind, AssetStore
from klm.store.db import connect, migrate
from klm.store.paths import Paths

FIXTURES = Path(__file__).parent / "fixtures"
KICAD = FIXTURES / "kicad"

STEP_BYTES = (KICAD / "3dmodels" / "Resistor_SMD.3dshapes" / "R_0402_1005Metric.step").read_bytes()


@pytest.fixture
def libraries() -> KicadLibraries:
    """The fixture libraries, plus whatever the machine happens to have."""
    return find_libraries(extra=(KICAD / "symbols", KICAD / "footprints"))


@pytest.fixture
def no_libraries() -> KicadLibraries:
    """A machine with no KiCad installed, so the template path is forced."""
    return KicadLibraries()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, AssetStore]]:
    paths = Paths(tmp_path / "home")
    paths.create()
    conn = connect(paths.db)
    migrate(conn)
    yield conn, AssetStore(paths.assets)
    conn.close()


def a_part(**overrides: object) -> Part:
    settings: dict[str, object] = {
        "klm_id": "01JB4K7QW8ZR3XN5M2VYT9DCFA",
        "mpn": "RC0402FR-074K7L",
        "manufacturer": "Yageo",
        "description": "4.7k 1% 0402",
        "category": "Passive/Resistor",
        "package": "0402",
        "status": PartStatus.DRAFT,
        **overrides,
    }
    return Part(**settings)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("0402", "0402"),
        ("1005 Metric", "0402"),
        ("1005Metric", "0402"),
        ("sot_23", "SOT-23"),
        ("SOT23-5", "SOT-23-5"),
        ("lqfp-48", "LQFP-48"),
    ],
)
def test_package_spellings_fold_to_one_name(spelling: str, expected: str) -> None:
    assert normalize_package(spelling) == expected


def test_a_bare_0603_reads_as_the_imperial_size() -> None:
    """`0603` is both an imperial size and the metric name of an 0201.

    A library writing bare digits means the imperial one essentially always,
    and guessing the other way would silently swap a resistor for one a third
    of the size.
    """
    package = find_package("0603")
    assert package is not None
    assert package.chip is not None
    assert package.chip.length == 1.60


def test_an_unknown_package_is_a_normal_answer() -> None:
    assert find_package("SOME-CUSTOM-THING") is None
    assert find_package(None) is None


def test_one_land_pattern_is_filed_under_the_designators_library() -> None:
    package = find_package("0402")
    assert package is not None
    assert package.kicad_id("R") == "Resistor_SMD:R_0402_1005Metric"
    assert package.kicad_id("C") == "Capacitor_SMD:C_0402_1005Metric"


def test_only_chips_are_generatable() -> None:
    assert find_package("0402").generatable  # type: ignore[union-attr]
    assert not find_package("LQFP-48").generatable  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Symbol templates
# ---------------------------------------------------------------------------


def test_a_passive_template_is_two_pins_on_the_grid() -> None:
    pins = iter_pins(passive_symbol("R_4k7", "R", "4.7k"))

    assert [p.number for p in pins] == ["1", "2"]
    assert all(p.on_grid() for p in pins)


def test_a_template_carries_the_four_kicad_fields_and_no_klm_ones() -> None:
    """klm's own fields are injected at generation time, from the catalog.

    Storing a copy in the asset would create two answers to one question, and
    the stale one would win whenever generation was skipped.
    """
    fields = properties(passive_symbol("R_4k7", "R", "4.7k"))

    assert sorted(fields) == ["Datasheet", "Footprint", "Reference", "Value"]
    assert "KLM_ID" not in fields


def test_generation_is_deterministic() -> None:
    left = dumps_canonical(passive_symbol("R_4k7", "R", "4.7k"))
    right = dumps_canonical(passive_symbol("R_4k7", "R", "4.7k"))

    assert left == right


def test_a_generated_symbol_round_trips_through_the_parser() -> None:
    text = dumps_canonical(passive_symbol("R_4k7", "R", "4.7k"))

    assert dumps_canonical(loads(text).root) == text


def test_there_is_no_template_for_a_designator_klm_cannot_draw() -> None:
    with pytest.raises(ValueError, match="no passive template"):
        passive_symbol("Q1", "Q")


def test_an_ic_template_splits_pins_between_the_two_edges() -> None:
    specs = [PinSpec(str(n), f"P{n}", "bidirectional") for n in range(1, 9)]
    pins = iter_pins(ic_symbol("U1", specs, "STM32"))

    assert len(pins) == 8
    assert all(p.on_grid() for p in pins)
    assert len({p.x for p in pins}) == 2


def test_a_connector_template_numbers_pins_top_to_bottom() -> None:
    pins = iter_pins(connector_symbol("J1", 4))

    assert [p.number for p in pins] == ["1", "2", "3", "4"]
    assert pins[0].y > pins[-1].y


@pytest.mark.parametrize(
    ("name", "expected"),
    [("VDD", "power_in"), ("GND", "power_in"), ("VOUT", "power_out"), ("SDA", "passive")],
)
def test_only_unambiguous_rails_are_typed_automatically(name: str, expected: str) -> None:
    """`SDA` is bidirectional most of the time, which is not good enough."""
    assert power_pin_type(name) == expected


def test_a_pin_type_kicad_does_not_know_is_refused() -> None:
    with pytest.raises(ValueError, match="not a KiCad pin type"):
        PinSpec("1", "VDD", "power")


# ---------------------------------------------------------------------------
# Land patterns
# ---------------------------------------------------------------------------


def test_a_generated_0402_matches_kicads_geometry_closely() -> None:
    """Within about 0.05 mm of `Resistor_SMD:R_0402_1005Metric`.

    Not identical — klm omits IPC's tolerance-driven rounding — but close
    enough to solder, which is the claim the docstring makes.
    """
    pads = iter_pads(chip_footprint(find_package("0402"), "R"))  # type: ignore[arg-type]

    assert [p.number for p in pads] == ["1", "2"]
    assert abs(abs(pads[0].x) - 0.51) < 0.05
    assert abs(pads[0].width - 0.54) < 0.05
    assert abs(pads[0].height - 0.64) < 0.05


@pytest.mark.parametrize("name", sorted(CHIP_PACKAGES))
def test_every_chip_package_generates_a_footprint_that_passes_qa(name: str) -> None:
    package = find_package(name)
    assert package is not None
    report = check_footprint(chip_footprint(package, "R"), package=package)

    assert report.passed, [str(r) for r in report.errors]


def test_the_generated_name_matches_kicads_so_a_swap_is_a_substitution() -> None:
    assert chip_footprint_name(find_package("0603"), "C") == "C_0603_1608Metric"  # type: ignore[arg-type]


def test_generating_a_non_chip_is_refused_rather_than_approximated() -> None:
    with pytest.raises(ValueError, match="not a chip package"):
        chip_footprint(find_package("LQFP-48"), "U")  # type: ignore[arg-type]


def test_land_patterns_are_deterministic() -> None:
    package = find_package("0805")
    assert package is not None
    assert dumps_canonical(chip_footprint(package, "C")) == dumps_canonical(
        chip_footprint(package, "C")
    )


# ---------------------------------------------------------------------------
# The QA gate
# ---------------------------------------------------------------------------


def test_a_generated_pair_passes_every_symbol_check() -> None:
    package = find_package("0402")
    symbol = passive_symbol("R_4k7", "R", "4.7k")
    report = check_symbol(symbol, package=package, category="Passive/Resistor")

    assert report.passed
    assert report.status is QaStatus.PASS


def test_a_wrong_pin_count_fails() -> None:
    symbol = passive_symbol("R_4k7", "R", "4.7k")
    report = check_symbol(symbol, package=find_package("SOT-23-5"))

    assert not report.passed
    assert any("pin count" in r.check for r in report.errors)


def test_duplicate_pin_numbers_fail() -> None:
    symbol = ic_symbol("U1", [PinSpec("1", "A"), PinSpec("1", "B"), PinSpec("2", "C")])
    report = check_symbol(symbol)

    assert any("duplicate pin numbers" in r.check for r in report.errors)


def test_an_unknown_package_is_skipped_not_quietly_passed() -> None:
    """A green report meaning "I didn't look" is worse than no report."""
    report = check_symbol(passive_symbol("R", "R"), package=None)
    result = next(r for r in report.results if "pin count" in r.check)

    assert result.status is QaStatus.UNCHECKED
    assert "unknown" in result.detail


def test_a_report_of_nothing_but_skips_is_unchecked_not_pass() -> None:
    report = check_model3d(STEP_BYTES[:20] + b"\x00")

    assert report.status is QaStatus.FAIL


def test_a_two_terminal_part_is_not_nagged_about_pin_types() -> None:
    """All-`passive` is *correct* for a resistor, not a missing type."""
    report = check_symbol(passive_symbol("R", "R"), package=find_package("0402"))
    result = next(r for r in report.results if "electrical types" in r.check)

    assert result.status is QaStatus.PASS


def test_an_ic_left_all_passive_is_warned_about() -> None:
    specs = [PinSpec(str(n)) for n in range(1, 6)]
    report = check_symbol(ic_symbol("U1", specs), package=find_package("SOT-23-5"))

    assert any("electrical types" in r.check for r in report.warnings)


def test_a_designator_that_disagrees_with_the_category_is_warned_about() -> None:
    report = check_symbol(passive_symbol("C1", "C"), category="Passive/Resistor")

    assert any("designator" in r.check for r in report.warnings)


def test_a_missing_courtyard_fails() -> None:
    footprint = loads(
        '(footprint "X" (layer "F.Cu") '
        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))'
    ).root
    report = check_footprint(footprint)

    assert any("courtyard" in r.check for r in report.errors)


def test_an_open_courtyard_fails() -> None:
    """Three sides of a rectangle is a gap, and DRC cannot use it."""
    footprint = loads(
        '(footprint "X" (layer "F.Cu")'
        ' (fp_line (start -1 -1) (end 1 -1) (layer "F.CrtYd"))'
        ' (fp_line (start 1 -1) (end 1 1) (layer "F.CrtYd"))'
        ' (fp_line (start 1 1) (end -1 1) (layer "F.CrtYd"))'
        ' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))'
    ).root
    report = check_footprint(footprint)

    assert any("courtyard" in r.check for r in report.errors)


def test_pads_that_disagree_with_the_symbols_pins_fail() -> None:
    symbol = ic_symbol("U1", [PinSpec(str(n)) for n in range(1, 6)])
    report = check_footprint(chip_footprint(find_package("0402"), "R"), symbol=symbol)  # type: ignore[arg-type]

    assert any("pad numbers" in r.check for r in report.errors)


def test_silkscreen_on_a_pad_is_a_warning_not_a_failure() -> None:
    footprint = loads(
        '(footprint "X" (layer "F.Cu")'
        ' (fp_rect (start -2 -2) (end 2 2) (layer "F.CrtYd"))'
        ' (fp_line (start 0 0) (end 0.2 0) (layer "F.SilkS"))'
        ' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))'
    ).root
    report = check_footprint(footprint)

    assert report.passed
    assert any("silkscreen" in r.check for r in report.warnings)


def test_a_valid_step_file_passes() -> None:
    report = check_model3d(STEP_BYTES)

    assert report.passed
    assert any("closed solid" in r.check and r.status is QaStatus.PASS for r in report.results)


def test_something_that_is_not_step_fails_immediately() -> None:
    report = check_model3d(b"solid teapot\nfacet normal 0 0 1\n")

    assert not report.passed
    assert len(report.results) == 1


def test_a_truncated_step_file_fails() -> None:
    report = check_model3d(STEP_BYTES[: len(STEP_BYTES) // 2])

    assert any("complete" in r.check for r in report.errors)


def test_an_open_shell_warns_that_the_mesh_was_not_watertight() -> None:
    data = STEP_BYTES.replace(b"MANIFOLD_SOLID_BREP", b"SHELL_BASED_SURFACE_MODEL")
    report = check_model3d(data)

    assert report.passed
    assert any("closed solid" in r.check for r in report.warnings)


def test_an_oversized_model_is_warned_about() -> None:
    padded = STEP_BYTES[:-30] + b"/*" + b" " * 2048 + b"*/\nEND-ISO-10303-21;\n"
    report = check_model3d(padded, max_bytes=1024)

    assert any("file size" in r.check for r in report.warnings)


def test_the_qa_report_serialises_for_storage() -> None:
    import json

    payload = json.loads(check_symbol(passive_symbol("R", "R")).to_json())

    assert payload["kind"] == "symbol"
    assert {"check", "status", "detail"} <= set(payload["results"][0])


# ---------------------------------------------------------------------------
# KiCad library discovery
# ---------------------------------------------------------------------------


def test_the_fixture_libraries_are_found(libraries: KicadLibraries) -> None:
    assert "Device" in libraries.symbol_libraries()
    assert "Resistor_SMD" in libraries.footprint_libraries()


def test_a_symbol_is_fetched_by_its_lib_id(libraries: KicadLibraries) -> None:
    symbol = libraries.find_symbol("Device:R")

    assert symbol is not None
    assert len(iter_pins(symbol)) == 2


def test_a_missing_library_or_symbol_returns_nothing(libraries: KicadLibraries) -> None:
    assert libraries.find_symbol("NoSuchLib:R") is None
    assert libraries.find_symbol("Device:NoSuchSymbol") is None
    assert libraries.find_symbol("malformed") is None
    assert libraries.find_footprint("Resistor_SMD:NoSuchFootprint") is None


def test_no_kicad_installed_is_a_degradation_not_a_failure(
    no_libraries: KicadLibraries,
) -> None:
    assert not no_libraries.available
    assert no_libraries.find_symbol("Device:R") is None


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


def test_kicads_library_is_preferred_over_a_template(
    env: tuple[sqlite3.Connection, AssetStore], libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part())

    report = acquire_assets(conn, store, part, libraries=libraries)

    symbol = report.of(AssetKind.SYMBOL)
    footprint = report.of(AssetKind.FOOTPRINT)
    assert symbol is not None and symbol.origin == AssetOrigin.KICAD
    assert footprint is not None and footprint.origin == AssetOrigin.KICAD


def test_a_template_is_used_when_kicad_is_absent(
    env: tuple[sqlite3.Connection, AssetStore], no_libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part())

    report = acquire_assets(conn, store, part, libraries=no_libraries)

    symbol = report.of(AssetKind.SYMBOL)
    footprint = report.of(AssetKind.FOOTPRINT)
    assert symbol is not None and symbol.origin == AssetOrigin.GENERATED
    assert footprint is not None and footprint.origin == AssetOrigin.GENERATED


def test_a_symbol_taken_from_kicad_is_renamed_to_the_part(
    env: tuple[sqlite3.Connection, AssetStore], libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part())
    acquire_assets(conn, store, part, libraries=libraries)

    assert part.symbol_hash is not None
    text = store.read_text(part.symbol_hash, AssetKind.SYMBOL)
    assert "RC0402FR-074K7L" in text


def test_kicad_assets_record_their_licence(
    env: tuple[sqlite3.Connection, AssetStore], libraries: KicadLibraries
) -> None:
    """Q4 is open; the answer starts with knowing where a file came from."""
    conn, store = env
    part = save_part(conn, a_part())
    acquire_assets(conn, store, part, libraries=libraries)

    row = conn.execute(
        "SELECT license_note FROM asset WHERE content_hash = ?", (part.footprint_hash,)
    ).fetchone()
    assert "KiCad Library License" in row["license_note"]


def test_a_second_part_in_the_same_package_reuses_the_footprint(
    env: tuple[sqlite3.Connection, AssetStore], no_libraries: KicadLibraries
) -> None:
    """This is the branch that keeps a catalog from growing fifty 0402 pads."""
    conn, store = env
    first = save_part(conn, a_part())
    acquire_assets(conn, store, first, libraries=no_libraries)

    second = save_part(
        conn, a_part(klm_id="01JB4K7QW8ZR3XN5M2VYT9DCFB", mpn="RC0402FR-0710KL")
    )
    report = acquire_assets(conn, store, second, libraries=no_libraries)

    footprint = report.of(AssetKind.FOOTPRINT)
    assert footprint is not None
    assert footprint.reused
    assert second.footprint_hash == first.footprint_hash


def test_two_parts_never_share_a_symbol(
    env: tuple[sqlite3.Connection, AssetStore], no_libraries: KicadLibraries
) -> None:
    """A symbol carries the part's own name, so sharing one is not possible."""
    conn, store = env
    first = save_part(conn, a_part())
    acquire_assets(conn, store, first, libraries=no_libraries)
    second = save_part(
        conn, a_part(klm_id="01JB4K7QW8ZR3XN5M2VYT9DCFB", mpn="RC0402FR-0710KL")
    )
    acquire_assets(conn, store, second, libraries=no_libraries)

    assert first.symbol_hash != second.symbol_hash


def test_a_3d_model_comes_from_kicad_where_one_exists(
    env: tuple[sqlite3.Connection, AssetStore], libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part())

    report = acquire_assets(conn, store, part, libraries=libraries)

    model = report.of(AssetKind.MODEL3D)
    assert model is not None
    assert part.model3d_hash is not None


def test_what_klm_could_not_get_is_named_rather_than_left_silent(
    env: tuple[sqlite3.Connection, AssetStore], no_libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part(package="SOME-CUSTOM-THING", category="IC"))

    report = acquire_assets(conn, store, part, libraries=no_libraries)

    kinds = {kind for kind, _reason in report.unavailable}
    assert {"symbol", "footprint", "model3d"} <= kinds
    assert not report.ok


def test_acquisition_leaves_a_hand_corrected_asset_alone(
    env: tuple[sqlite3.Connection, AssetStore], no_libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part())
    acquire_assets(conn, store, part, libraries=no_libraries)
    corrected = store.add_bytes(b'(footprint "hand" (layer "F.Cu"))', AssetKind.FOOTPRINT)
    part.footprint_hash = corrected
    save_part(conn, part)

    acquire_assets(conn, store, part, libraries=no_libraries)

    assert part.footprint_hash == corrected


def test_overwrite_replaces_what_is_there(
    env: tuple[sqlite3.Connection, AssetStore], no_libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part())
    acquire_assets(conn, store, part, libraries=no_libraries)
    part.footprint_hash = store.add_bytes(b'(footprint "hand" (layer "F.Cu"))', AssetKind.FOOTPRINT)
    save_part(conn, part)

    acquire_assets(conn, store, part, libraries=no_libraries, overwrite=True)

    assert part.footprint_hash is not None
    assert "R_0402_1005Metric" in store.read_text(part.footprint_hash, AssetKind.FOOTPRINT)


def test_the_qa_verdict_is_stored_on_the_asset(
    env: tuple[sqlite3.Connection, AssetStore], no_libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part())
    acquire_assets(conn, store, part, libraries=no_libraries)

    row = conn.execute(
        "SELECT qa_status, qa_report, source FROM asset WHERE content_hash = ?",
        (part.symbol_hash,),
    ).fetchone()
    assert row["qa_status"] == "pass"
    assert row["source"] == AssetOrigin.GENERATED
    assert row["qa_report"]


def test_qa_can_be_re_run_over_stored_assets(
    env: tuple[sqlite3.Connection, AssetStore], no_libraries: KicadLibraries
) -> None:
    conn, store = env
    part = save_part(conn, a_part())
    acquire_assets(conn, store, part, libraries=no_libraries)

    reports = run_qa(conn, store, part)

    assert reports[AssetKind.SYMBOL].passed
    assert reports[AssetKind.FOOTPRINT].passed


# ---------------------------------------------------------------------------
# Reuse hygiene
# ---------------------------------------------------------------------------


def test_duplicate_footprints_are_found_by_geometry_not_name(
    env: tuple[sqlite3.Connection, AssetStore],
) -> None:
    conn, store = env
    package = find_package("0402")
    assert package is not None
    original = chip_footprint(package, "R")
    text = dumps_canonical(original)
    # Same land pattern, different name and a stray comment's worth of bytes.
    renamed = loads(text.replace("R_0402_1005Metric", "MY_0402_RESISTOR")).root

    for node, name in ((original, "R_0402_1005Metric"), (renamed, "MY_0402_RESISTOR")):
        content_hash = store.add_bytes(dumps_canonical(node).encode("utf-8"), AssetKind.FOOTPRINT)
        register_asset(
            conn, content_hash, AssetKind.FOOTPRINT, filename=name, source=AssetOrigin.GENERATED
        )

    candidates = reuse_candidates(conn, store)

    assert len(candidates) == 1


def test_genuinely_different_footprints_are_not_reported(
    env: tuple[sqlite3.Connection, AssetStore],
) -> None:
    conn, store = env
    for name in ("0402", "0805"):
        package = find_package(name)
        assert package is not None
        content_hash = store.add_bytes(
            dumps_canonical(chip_footprint(package, "R")).encode("utf-8"), AssetKind.FOOTPRINT
        )
        register_asset(
            conn, content_hash, AssetKind.FOOTPRINT, filename=name, source=AssetOrigin.GENERATED
        )

    assert reuse_candidates(conn, store) == []


# ---------------------------------------------------------------------------
# FreeCAD
# ---------------------------------------------------------------------------


def test_the_conversion_script_ships_with_the_package() -> None:
    assert script_path().is_file()
    assert "obj2step" in script_path().read_text(encoding="utf-8")


def test_a_missing_freecad_is_a_named_degradation(tmp_path: Path) -> None:
    mesh = tmp_path / "part.obj"
    mesh.write_text("v 0 0 0\n", encoding="utf-8")

    with pytest.raises(FreeCadUnavailable, match="freecadcmd"):
        convert_mesh(mesh, tmp_path / "out", freecad=None, script=script_path())


def test_a_format_freecad_does_not_read_is_refused_early(tmp_path: Path) -> None:
    mesh = tmp_path / "part.dxf"
    mesh.write_text("nope", encoding="utf-8")

    with pytest.raises(ValueError, match="not a mesh format"):
        convert_mesh(mesh, tmp_path / "out")


def test_a_missing_mesh_is_reported_before_anything_is_launched(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        convert_mesh(tmp_path / "absent.obj", tmp_path / "out")


def test_conversion_shells_out_and_caches_by_mesh_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mesh = tmp_path / "part.obj"
    mesh.write_text("v 0 0 0\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        Path(command[3]).write_bytes(STEP_BYTES)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    first = convert_mesh(
        mesh, tmp_path / "out", freecad=Path("/usr/bin/freecadcmd"), script=script_path()
    )
    second = convert_mesh(
        mesh, tmp_path / "out", freecad=Path("/usr/bin/freecadcmd"), script=script_path()
    )

    assert isinstance(first, ConversionResult)
    assert not first.cached
    assert second.cached
    assert len(calls) == 1


def test_a_failed_conversion_leaves_no_half_written_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated STEP would pass a "file exists" check and fail in KiCad."""
    mesh = tmp_path / "part.obj"
    mesh.write_text("v 0 0 0\n", encoding="utf-8")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(command[3]).write_bytes(b"half a file")
        return subprocess.CompletedProcess(command, 3, "", "obj2step: could not make a solid\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="could not make a solid"):
        convert_mesh(
            mesh, tmp_path / "out", freecad=Path("/usr/bin/freecadcmd"), script=script_path()
        )
    assert not list((tmp_path / "out").glob("*.step"))


def test_a_non_watertight_warning_is_surfaced_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mesh = tmp_path / "part.obj"
    mesh.write_text("v 0 0 0\n", encoding="utf-8")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(command[3]).write_bytes(STEP_BYTES)
        return subprocess.CompletedProcess(
            command, 0, "", "obj2step: warning: the solid is not watertight\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = convert_mesh(
        mesh, tmp_path / "out", freecad=Path("/usr/bin/freecadcmd"), script=script_path()
    )

    assert not result.watertight
