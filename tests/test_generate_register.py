"""Tests for library generation and KiCad registration."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from klm.fields import KLM_ID
from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.libtable import LibEntry, TableKind, load_table, read_entries, upsert_entry
from klm.kicad.sexpr import dumps, loads
from klm.model import Lifecycle, Part, PartStatus
from klm.services.catalog import save_part
from klm.services.generate import generate
from klm.services.register import LIBRARY_NAME, apply_plan, plan_registration
from klm.store.assets import AssetKind, AssetStore
from klm.store.db import connect, migrate
from klm.store.paths import Paths

FIXTURES = Path(__file__).parent / "fixtures"

SYMBOL_ASSET = """(kicad_symbol_lib
	(version 20231120)
	(generator "test")
	(symbol "RAW_NAME"
		(property "Reference" "U" (at 0 0 0))
		(property "Value" "RAW_NAME" (at 0 0 0))
		(property "Manufacturer_Part_Number" "legacy-spelling" (at 0 0 0))
		(symbol "RAW_NAME_0_1"
			(rectangle (start -5 5) (end 5 -5))
		)
		(symbol "RAW_NAME_1_1"
			(pin power_in line (at -7.62 0 0) (length 2.54)
				(name "VI") (number "3"))
		)
	)
)
"""

FOOTPRINT_ASSET = """(footprint "SOT-223-3"
	(layer "F.Cu")
	(pad "1" smd rect (at -2.3 -3.2) (size 1.2 1.2) (layers "F.Cu"))
	(model "/home/someone/absolute/path/SOT-223.wrl"
		(offset (xyz 0 0 0))
	)
)
"""

STEP_ASSET = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=POINT('',(0.,0.,0.));\nENDSEC;\n"


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "home")
    paths.create()
    conn = connect(paths.db)
    migrate(conn)
    yield paths, conn, AssetStore(paths.assets)
    conn.close()


def seed_part(store: AssetStore, conn: sqlite3.Connection, **overrides: object) -> Part:
    defaults: dict[str, object] = {
        "klm_id": "01JB4K7QW8ZR3XN5M2VYT9DCFA",
        "mpn": "AMS1117-3.3",
        "manufacturer": "AMS",
        "description": "1 A LDO regulator",
        "category": "IC/Power/Regulator/LDO",
        "package": "SOT-223-3",
        "lifecycle": Lifecycle.ACTIVE,
        "status": PartStatus.APPROVED,
        "datasheet_url": "http://example.com/ds.pdf",
        "symbol_hash": store.add_bytes(SYMBOL_ASSET.encode(), AssetKind.SYMBOL),
        "footprint_hash": store.add_bytes(FOOTPRINT_ASSET.encode(), AssetKind.FOOTPRINT),
        "model3d_hash": store.add_bytes(STEP_ASSET, AssetKind.MODEL3D),
    }
    defaults.update(overrides)
    return save_part(conn, Part(**defaults))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------


def test_renaming_a_symbol_also_renames_its_units() -> None:
    """A unit left under the old name loads but renders nothing."""
    symbol = sym.extract_symbols(loads(SYMBOL_ASSET))[0]
    sym.rename_symbol(symbol, "AMS1117-3.3")
    assert sym.symbol_name(symbol) == "AMS1117-3.3"
    units = [sym.symbol_name(c) for c in symbol.find_all("symbol")]
    assert units == ["AMS1117-3.3_0_1", "AMS1117-3.3_1_1"]


def test_a_unit_not_following_the_convention_still_ends_up_under_the_parent() -> None:
    symbol = sym.extract_symbols(loads('(symbol "A" (symbol "ODD_NAME" (rectangle)))'))[0]
    sym.rename_symbol(symbol, "B")
    assert [sym.symbol_name(c) for c in symbol.find_all("symbol")] == ["B_ODD_NAME"]


def test_setting_an_existing_property_keeps_its_placement() -> None:
    """A hand-placed Reference must not jump because klm rewrote the value."""
    source = '(symbol "A" (property "Reference" "U" (at 3 4 90)))'
    symbol = sym.extract_symbols(loads(source))[0]
    sym.set_property(symbol, "Reference", "R**")
    prop = sym.find_property(symbol, "Reference")
    assert prop is not None
    assert prop.values()[:2] == ["Reference", "R**"]
    assert dumps(prop.find("at")).strip() == "(at 3 4 90)"


def test_a_new_property_is_inserted_before_the_graphical_units() -> None:
    source = '(symbol "A" (property "Reference" "U") (symbol "A_0_1"))'
    symbol = sym.extract_symbols(loads(source))[0]
    sym.set_property(symbol, "MPN", "X")
    names = [c.name for c in symbol.children if hasattr(c, "name")]
    assert names.index("property") < names.index("symbol")
    assert names[-1] == "symbol"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("A/B", "A_B"), ("A:B", "A_B"), ("has space", "has_space"), ("  ", "unnamed")],
)
def test_names_are_sanitized(raw: str, expected: str) -> None:
    assert sym.sanitize_name(raw) == expected


def test_extract_symbols_accepts_a_library_or_a_bare_symbol() -> None:
    assert len(sym.extract_symbols(loads(SYMBOL_ASSET))) == 1
    assert len(sym.extract_symbols(loads('(symbol "A")'))) == 1
    assert sym.extract_symbols(loads("(something_else)")) == []


# ---------------------------------------------------------------------------
# Footprint helpers
# ---------------------------------------------------------------------------


def test_absolute_model_paths_are_rewritten_to_an_env_var() -> None:
    """An absolute path is what makes a library unshareable."""
    doc = loads(FOOTPRINT_ASSET)
    assert fp.rewrite_model_paths(doc, filename="SOT-223.step") == 1
    assert fp.model_paths(doc) == ["${KLM_3DMODELS}/SOT-223.step"]
    assert "/home/someone" not in dumps(doc)


def test_rewriting_reports_when_there_was_no_model() -> None:
    assert fp.rewrite_model_paths(loads('(footprint "X")')) == 0


def test_windows_paths_are_handled() -> None:
    doc = loads('(footprint "X" (model "C:\\\\models\\\\part.wrl"))')
    fp.rewrite_model_paths(doc)
    assert fp.model_paths(doc) == ["${KLM_3DMODELS}/part.wrl"]


def test_footprint_name_is_read_from_the_asset() -> None:
    assert fp.footprint_name(loads(FOOTPRINT_ASSET)) == "SOT-223-3"


def test_real_fixture_footprint_round_trips_through_rewriting() -> None:
    doc = loads((FIXTURES / "footprint_sample.kicad_mod").read_text())
    assert fp.rewrite_model_paths(doc) == 1
    assert fp.model_paths(doc)[0].startswith("${KLM_3DMODELS}/")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def test_generate_produces_the_three_library_artifacts(env) -> None:
    paths, conn, store = env
    seed_part(store, conn)

    result = generate(conn, paths)
    assert result.ok
    assert (result.symbols, result.footprints, result.models) == (1, 1, 1)
    assert paths.generated_symbols.exists()
    assert (paths.generated_footprints / "SOT-223-3.kicad_mod").exists()
    assert (paths.generated_models3d / "SOT-223-3.step").exists()


def test_generated_symbol_carries_the_canonical_fields(env) -> None:
    paths, conn, store = env
    part = seed_part(store, conn)
    generate(conn, paths)

    symbol = sym.extract_symbols(loads(paths.generated_symbols.read_text()))[0]
    properties = {
        p.values()[0]: p.values()[1] for p in symbol.find_all("property") if len(p.values()) >= 2
    }
    assert properties["MPN"] == part.mpn
    assert properties["Manufacturer"] == "AMS"
    assert properties[KLM_ID] == part.klm_id
    assert properties["Footprint"] == "KLM:SOT-223-3"
    assert properties["Datasheet"] == "http://example.com/ds.pdf"
    assert properties["Value"] == part.mpn


def test_the_symbol_is_renamed_to_the_mpn(env) -> None:
    paths, conn, store = env
    seed_part(store, conn)
    generate(conn, paths)
    library = sym.extract_symbols(loads(paths.generated_symbols.read_text()))
    assert [sym.symbol_name(s) for s in library] == ["AMS1117-3.3"]


def test_generated_footprint_has_no_absolute_paths(env) -> None:
    paths, conn, store = env
    seed_part(store, conn)
    generate(conn, paths)
    text = (paths.generated_footprints / "SOT-223-3.kicad_mod").read_text()
    assert "/home/someone" not in text
    assert "${KLM_3DMODELS}/SOT-223-3.step" in text


def test_only_approved_parts_are_generated(env) -> None:
    """A draft is not usable in a design; the library must not make it usable."""
    paths, conn, store = env
    seed_part(store, conn, klm_id="01APPROVED", mpn="A", status=PartStatus.APPROVED)
    seed_part(store, conn, klm_id="01DRAFT", mpn="B", status=PartStatus.DRAFT)

    result = generate(conn, paths)
    assert result.symbols == 1
    assert "MPN: B" not in paths.generated_symbols.read_text()


def test_generation_is_idempotent(env) -> None:
    paths, conn, store = env
    seed_part(store, conn)
    assert generate(conn, paths).changed
    before = paths.generated_symbols.stat().st_mtime_ns

    second = generate(conn, paths)
    assert not second.changed
    assert paths.generated_symbols.stat().st_mtime_ns == before


def test_a_shared_footprint_is_written_once(env) -> None:
    """Fifty 0402 resistors must not produce fifty identical files."""
    paths, conn, store = env
    for i in range(5):
        seed_part(store, conn, klm_id=f"01PART{i:020d}", mpn=f"R{i}")

    result = generate(conn, paths)
    assert result.symbols == 5
    assert result.footprints == 1
    assert len(list(paths.generated_footprints.glob("*.kicad_mod"))) == 1


def test_symbol_name_collisions_resolve_deterministically(env) -> None:
    """The same MPN from two manufacturers is legitimate — AMS1117 has several."""
    paths, conn, store = env
    seed_part(store, conn, klm_id="01AAA", mpn="SAME", manufacturer="Maker One")
    seed_part(store, conn, klm_id="01BBB", mpn="SAME", manufacturer="Maker Two")

    generate(conn, paths)
    names = sorted(
        sym.symbol_name(s) for s in sym.extract_symbols(loads(paths.generated_symbols.read_text()))
    )
    assert len(set(names)) == 2
    assert "SAME" in names


def test_a_part_without_a_symbol_is_skipped_with_a_reason(env) -> None:
    paths, conn, store = env
    seed_part(store, conn, symbol_hash=None)
    result = generate(conn, paths)
    assert not result.ok
    assert result.skipped[0][1] == "no symbol asset"


def test_stale_outputs_are_removed(env) -> None:
    paths, conn, store = env
    seed_part(store, conn)
    generate(conn, paths)

    orphan = paths.generated_footprints / "OLD.kicad_mod"
    orphan.write_text("(footprint \"OLD\")")
    generate(conn, paths)
    assert not orphan.exists()


def test_manifest_records_what_produced_the_build(env) -> None:
    paths, conn, store = env
    part = seed_part(store, conn)
    generate(conn, paths)

    manifest = json.loads((paths.generated / "manifest.json").read_text())
    assert manifest["parts"][0]["klm_id"] == part.klm_id
    assert manifest["parts"][0]["symbol_hash"] == part.symbol_hash
    assert manifest["klm_version"]


def test_an_empty_catalog_generates_an_empty_library(env) -> None:
    paths, conn, _store = env
    result = generate(conn, paths)
    assert result.ok
    assert result.symbols == 0
    assert "kicad_symbol_lib" in paths.generated_symbols.read_text()


# ---------------------------------------------------------------------------
# Library tables
# ---------------------------------------------------------------------------


def test_a_missing_table_is_synthesised_not_an_error(tmp_path: Path) -> None:
    doc = load_table(tmp_path / "sym-lib-table", TableKind.SYMBOL)
    assert doc.root.name == "sym_lib_table"
    assert read_entries(doc) == []


def test_upsert_adds_a_row_and_reports_the_change(tmp_path: Path) -> None:
    doc = load_table(tmp_path / "sym-lib-table", TableKind.SYMBOL)
    assert upsert_entry(doc, LibEntry("KLM", "${KLM_LIBS}/KLM.kicad_sym"))
    entries = read_entries(doc)
    assert [e.name for e in entries] == ["KLM"]
    assert entries[0].uri == "${KLM_LIBS}/KLM.kicad_sym"


def test_upsert_is_idempotent(tmp_path: Path) -> None:
    doc = load_table(tmp_path / "sym-lib-table", TableKind.SYMBOL)
    entry = LibEntry("KLM", "${KLM_LIBS}/KLM.kicad_sym")
    upsert_entry(doc, entry)
    assert not upsert_entry(doc, entry)


def test_other_libraries_survive_byte_for_byte(tmp_path: Path) -> None:
    """This is the user's file, shared with every other library they have."""
    original = (
        '(sym_lib_table\n'
        '  (version 7)\n'
        '  (lib (name "MyOldLib")(type "Legacy")(uri "/some/path.lib")(options "")(descr "mine"))\n'
        ")\n"
    )
    path = tmp_path / "sym-lib-table"
    path.write_text(original)

    doc = load_table(path, TableKind.SYMBOL)
    upsert_entry(doc, LibEntry("KLM", "${KLM_LIBS}/KLM.kicad_sym"))
    result = dumps(doc)

    assert '(name "MyOldLib")(type "Legacy")(uri "/some/path.lib")' in result
    assert '(name "KLM")' in result


def test_updating_an_existing_row_changes_only_the_uri(tmp_path: Path) -> None:
    path = tmp_path / "sym-lib-table"
    path.write_text(
        '(sym_lib_table\n  (lib (name "KLM")(type "KiCad")'
        '(uri "/old")(options "x")(descr "d"))\n)\n'
    )
    doc = load_table(path, TableKind.SYMBOL)
    assert upsert_entry(doc, LibEntry("KLM", "/new", options="x", descr="d"))
    assert read_entries(doc)[0].uri == "/new"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_plan_reports_everything_missing_without_writing(env, tmp_path: Path) -> None:
    paths, _conn, _store = env
    config = tmp_path / "kicad" / "9.0"

    plan = plan_registration(paths, config)
    assert plan.needed
    assert len(plan.changes) == 4  # two tables, two env vars
    assert not config.exists()


def test_apply_registers_both_tables_and_both_env_vars(env, tmp_path: Path) -> None:
    paths, _conn, _store = env
    config = tmp_path / "kicad" / "9.0"

    apply_plan(paths, config)

    for filename in ("sym-lib-table", "fp-lib-table"):
        entries = read_entries(load_table(config / filename, TableKind.SYMBOL))
        assert [e.name for e in entries] == [LIBRARY_NAME]

    common = json.loads((config / "kicad_common.json").read_text())
    assert common["environment"]["vars"]["KLM_LIBS"] == str(paths.generated)
    assert common["environment"]["vars"]["KLM_3DMODELS"] == str(paths.generated_models3d)


def test_registration_is_idempotent(env, tmp_path: Path) -> None:
    paths, _conn, _store = env
    config = tmp_path / "kicad" / "9.0"
    apply_plan(paths, config)
    assert not plan_registration(paths, config).needed


def test_unrelated_kicad_settings_are_preserved(env, tmp_path: Path) -> None:
    """kicad_common.json holds far more than environment variables."""
    paths, _conn, _store = env
    config = tmp_path / "kicad" / "9.0"
    config.mkdir(parents=True)
    (config / "kicad_common.json").write_text(
        json.dumps(
            {
                "system": {"editor_name": "vim"},
                "environment": {"vars": {"SOMETHING_ELSE": "/keep/me"}},
            },
            indent=2,
        )
    )

    apply_plan(paths, config)

    common = json.loads((config / "kicad_common.json").read_text())
    assert common["system"]["editor_name"] == "vim"
    assert common["environment"]["vars"]["SOMETHING_ELSE"] == "/keep/me"
    assert common["environment"]["vars"]["KLM_LIBS"] == str(paths.generated)


def test_existing_files_are_backed_up_before_being_changed(env, tmp_path: Path) -> None:
    paths, _conn, _store = env
    config = tmp_path / "kicad" / "9.0"
    config.mkdir(parents=True)
    original = '(sym_lib_table\n  (version 7)\n)\n'
    (config / "sym-lib-table").write_text(original)

    apply_plan(paths, config)
    assert (config / "sym-lib-table.klm-bak").read_text() == original


def test_a_corrupt_kicad_config_is_not_overwritten(env, tmp_path: Path) -> None:
    """The user's broken file is theirs to fix; klm must not clobber it."""
    paths, _conn, _store = env
    config = tmp_path / "kicad" / "9.0"
    config.mkdir(parents=True)
    (config / "kicad_common.json").write_text("{ this is not json")

    with pytest.raises(json.JSONDecodeError):
        apply_plan(paths, config)
    assert (config / "kicad_common.json").read_text() == "{ this is not json"


def test_no_temporary_files_are_left_behind(env, tmp_path: Path) -> None:
    paths, _conn, _store = env
    config = tmp_path / "kicad" / "9.0"
    apply_plan(paths, config)
    assert list(config.glob("*.klm-tmp")) == []


def test_fields_are_emitted_in_canonical_schema_order(env) -> None:
    """Insertion order would make the same fields diff differently per part."""
    paths, conn, store = env
    seed_part(store, conn)
    generate(conn, paths)

    text = paths.generated_symbols.read_text(encoding="utf-8")
    names = re.findall(r'\(property "([^"]+)"', text)
    modelled = [n for n in names if n in ("Reference", "Value", "Footprint", "Datasheet",
                                          "MPN", "Manufacturer", "Description", "Package",
                                          KLM_ID)]
    assert modelled == [
        "Reference", "Value", "Footprint", "Datasheet",
        "MPN", "Manufacturer", "Description", "Package", KLM_ID,
    ]
    # Names klm does not model sort after the canonical ones, by name, so they
    # are stable too rather than merely last.
    unknown = [n for n in names if n not in modelled]
    assert unknown == sorted(unknown)
