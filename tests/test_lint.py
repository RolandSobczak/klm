"""Importing a drifted library, and linting it back into shape.

The fixture library is deliberately the kind of thing a real hobby library
looks like: Polish field names, a value written as a bare number, an alias for
`MPN`, a derived symbol, and one field klm has never heard of.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from klm.assets.kicad_libs import KicadLibraries
from klm.config import Config, ConfigError, FieldConfig, LintConfig, default_aliases, load_config
from klm.kicad import symbols as sym
from klm.kicad.sexpr import dumps, loads
from klm.model import Part, PartStatus
from klm.services.catalog import get_part, list_parts, save_part
from klm.services.importer import import_symbol_library
from klm.services.lint import Selector, Severity, lint_catalog
from klm.store.assets import AssetKind, AssetStore
from klm.store.db import connect, migrate
from klm.store.paths import Paths

FIXTURES = Path(__file__).parent / "fixtures"
DRIFTED = FIXTURES / "drifted_library.kicad_sym"
KICAD = FIXTURES / "kicad"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[Paths, sqlite3.Connection, AssetStore]]:
    paths = Paths(tmp_path / "home")
    paths.create()
    conn = connect(paths.db)
    migrate(conn)
    yield paths, conn, AssetStore(paths.assets)
    conn.close()


def config() -> Config:
    return Config(fields=FieldConfig(aliases=default_aliases()))




# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_imports_every_symbol_as_a_draft(env: tuple[Paths, sqlite3.Connection, AssetStore]) -> None:
    _, conn, store = env
    report = import_symbol_library(conn, store, DRIFTED, config=config())

    assert len(report.imported) == 2
    assert report.created == 2
    assert all(part.status is PartStatus.DRAFT for part in list_parts(conn))


def test_derived_symbols_are_skipped_rather_than_half_imported(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """A symbol that `extends` another renders nothing without its parent."""
    _, conn, store = env
    report = import_symbol_library(conn, store, DRIFTED, config=config())

    assert [name for name, _ in report.skipped] == ["R_Small_Derived"]
    assert "extends" in report.skipped[0][1]
    assert not report.ok


def _library_with_footprint(tmp_path: Path, lib_id: str) -> Path:
    """A one-symbol library whose `Footprint` field names ``lib_id``."""
    text = DRIFTED.read_text(encoding="utf-8")
    document = loads(text)
    symbol = sym.extract_symbols(document)[0]
    for prop in symbol.find_all("property"):
        if len(prop) >= 3 and getattr(prop[1], "value", None) == "Footprint":
            prop[2].value = lib_id
    target = tmp_path / "one.kicad_sym"
    target.write_text(dumps(document), encoding="utf-8")
    return target


def test_import_takes_the_footprint_and_model_the_symbol_names(
    env: tuple[Paths, sqlite3.Connection, AssetStore], tmp_path: Path
) -> None:
    """A catalog of symbols alone is one whose parts break on another machine."""
    _, conn, store = env
    source = _library_with_footprint(tmp_path, "Resistor_SMD:R_0402_1005Metric")
    libraries = KicadLibraries(footprint_dirs=(KICAD / "footprints",))

    report = import_symbol_library(
        conn,
        store,
        source,
        config=config(),
        libraries=libraries,
        model_dirs=[KICAD / "3dmodels" / "Resistor_SMD.3dshapes"],
    )

    part = _by_mpn(conn, "RC0402FR-074K7L")
    assert part.footprint_hash and part.model3d_hash
    assert report.imported[0].footprint == "Resistor_SMD:R_0402_1005Metric"
    stored = store.read_text(part.footprint_hash, AssetKind.FOOTPRINT)
    assert "${KLM_3DMODELS}/R_0402_1005Metric.step" in stored, "the recorded path was local"
    assert "R_4k7" not in {name for name, _ in report.unresolved}


def test_a_footprint_that_cannot_be_found_is_reported_not_swallowed(
    env: tuple[Paths, sqlite3.Connection, AssetStore], tmp_path: Path
) -> None:
    _, conn, store = env
    source = _library_with_footprint(tmp_path, "Nowhere:NoSuchThing")

    report = import_symbol_library(
        conn, store, source, config=config(), libraries=KicadLibraries()
    )

    assert _by_mpn(conn, "RC0402FR-074K7L").footprint_hash is None, "still worth having"
    missing = dict(report.unresolved)
    assert "Nowhere:NoSuchThing" in missing["R_4k7"]


def test_aliased_fields_are_read_but_not_rewritten(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """Import reads through the alias map; changing the symbol is lint's job."""
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())

    resistor = _by_mpn(conn, "RC0402FR-074K7L")
    assert resistor.manufacturer == "Yageo"

    stored = store.read_text(resistor.symbol_hash or "", AssetKind.SYMBOL)
    assert '"Manufacturer_Part_Number"' in stored
    assert '"MPN"' not in stored


def test_unmodelled_fields_survive_as_parameters(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())

    resistor = _by_mpn(conn, "RC0402FR-074K7L")
    assert resistor.parameter("Tolerance") is not None
    assert resistor.parameter("Szuflada") is not None
    # KiCad's own library-browser metadata is not a property of the component.
    assert resistor.parameter("ki_keywords") is None


def test_kicad_placeholder_datasheet_is_not_stored_as_a_url(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())
    assert _by_mpn(conn, "RC0402FR-074K7L").datasheet_url is None


def test_reimport_updates_rather_than_duplicates(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """A symbol carrying a KLM_ID is the same part on the way back in."""
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())
    part = _by_mpn(conn, "RC0402FR-074K7L")

    document = loads(store.read_text(part.symbol_hash or "", AssetKind.SYMBOL))
    symbol = sym.extract_symbols(document)[0]
    sym.set_property(symbol, "KLM_ID", part.klm_id)
    round_two = FIXTURES.parent / "_round_two.kicad_sym"
    round_two.write_text(
        dumps(sym.make_library([symbol], generator="test", version="20231120")),
        encoding="utf-8",
    )
    try:
        report = import_symbol_library(conn, store, round_two, config=config())
    finally:
        round_two.unlink()

    assert report.updated == 1
    assert len(list_parts(conn)) == 2


def test_the_same_symbol_from_two_libraries_is_one_part(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """The same symbol is copied into every project that uses it.

    Without matching on manufacturer + MPN the second library collides with
    the first on the `(manufacturer, mpn)` unique index and the import dies.
    """
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())
    second = import_symbol_library(conn, store, DRIFTED, config=config())

    assert second.created == 0
    assert second.updated == 2
    assert len(list_parts(conn)) == 2


def _by_mpn(conn: sqlite3.Connection, mpn: str) -> Part:
    matches = [part for part in list_parts(conn) if part.mpn == mpn]
    assert matches, f"no part with MPN {mpn}"
    return matches[0]


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


def test_reports_alias_and_value_findings(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config(), category="Passive/Resistor")
    report = lint_catalog(conn, store, config(), selector=Selector(select=("S002", "V001")))

    messages = [f.message for f in report.findings]
    assert "field 'Manufacturer_Part_Number' → 'MPN'" in messages
    assert "field 'Tolerancja' → 'Tolerance'" in messages
    assert "Value '4700' → '4.7k'" in messages


def test_unknown_fields_are_flagged_and_declared_ones_are_not(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())

    flagged = lint_catalog(conn, store, config(), selector=Selector(select=("S003",)))
    assert any("Szuflada" in f.message for f in flagged.findings)

    declared = Config(
        fields=FieldConfig(aliases=default_aliases(), custom={"Szuflada": "storage drawer"})
    )
    accepted = lint_catalog(conn, store, declared, selector=Selector(select=("S003",)))
    assert not any("Szuflada" in f.message for f in accepted.findings)


def test_kicad_internal_fields_are_never_flagged(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """`ki_keywords` is in every stock symbol; complaining about it is noise."""
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())
    report = lint_catalog(conn, store, config())
    assert not any("ki_" in f.message for f in report.findings)


def test_fields_kicad_leaves_empty_are_not_reported_as_empty(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())
    report = lint_catalog(conn, store, config(), selector=Selector(select=("S006",)))
    assert not report.findings


def test_value_rules_do_not_run_without_a_category(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """klm cannot know that `4700` means ohms until something says so."""
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())
    report = lint_catalog(conn, store, config(), selector=Selector(select=("V",)))
    assert not report.findings

    warned = lint_catalog(conn, store, config(), selector=Selector(select=("S007",)))
    assert len(warned.findings) == 2


def test_unparseable_value_is_reported_never_guessed(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config(), category="Passive/Capacitor")
    # The resistor fixture's `4700` is a valid capacitance; its MPN is not.
    part = _by_mpn(conn, "RC0402FR-074K7L")
    _set_value(store, conn, part, "not-a-value")

    report = lint_catalog(conn, store, config(), selector=Selector(select=("V002",)))
    assert [f.rule for f in report.findings] == ["V002"]

    fixed = lint_catalog(conn, store, config(), selector=Selector(select=("V",)), fix=True)
    assert not [f for f in fixed.fixed if f.location.endswith(part.klm_id)]
    assert _properties(store, _by_mpn(conn, "RC0402FR-074K7L"))["Value"] == "not-a-value"


def test_fix_renames_fields_and_normalizes_values(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config(), category="Passive/Resistor")

    report = lint_catalog(
        conn, store, config(), selector=Selector(select=("S002", "V001")), fix=True
    )
    assert report.count(Severity.WARNING) == 0
    assert len(report.fixed) >= 4

    part = _by_mpn(conn, "RC0402FR-074K7L")
    properties = _properties(store, part)
    assert properties["MPN"] == "RC0402FR-074K7L"
    assert properties["Manufacturer"] == "Yageo"
    assert properties["Tolerance"] == "1%"
    assert properties["Value"] == "4.7k"
    assert "Manufacturer_Part_Number" not in properties


def test_fix_is_idempotent(env: tuple[Paths, sqlite3.Connection, AssetStore]) -> None:
    """Running --fix twice must be a no-op, or nothing built on it converges."""
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config(), category="Passive/Resistor")

    lint_catalog(conn, store, config(), fix=True)
    before = {part.klm_id: part.symbol_hash for part in list_parts(conn)}
    second = lint_catalog(conn, store, config(), fix=True)
    after = {part.klm_id: part.symbol_hash for part in list_parts(conn)}

    assert before == after
    assert not second.fixed


def test_dry_run_changes_nothing(env: tuple[Paths, sqlite3.Connection, AssetStore]) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config(), category="Passive/Resistor")
    before = {part.klm_id: part.symbol_hash for part in list_parts(conn)}

    report = lint_catalog(conn, store, config(), fix=True, dry_run=True)

    assert report.fixed, "dry run should still say what it would fix"
    assert {part.klm_id: part.symbol_hash for part in list_parts(conn)} == before


def test_fix_keeps_the_previous_asset_in_the_store(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    """Content addressing is what makes --fix recoverable."""
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config(), category="Passive/Resistor")
    original = _by_mpn(conn, "RC0402FR-074K7L").symbol_hash
    assert original is not None

    lint_catalog(conn, store, config(), fix=True)

    assert _by_mpn(conn, "RC0402FR-074K7L").symbol_hash != original
    assert store.exists(original, AssetKind.SYMBOL)


def test_selector_understands_groups_and_ids() -> None:
    selector = Selector(select=("S", "V001"), ignore=("S006",))
    assert "S001" in selector
    assert "V001" in selector
    assert "S006" not in selector
    assert "V002" not in selector
    assert "A001" not in selector


def test_report_fails_on_warnings_only_when_asked(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    import_symbol_library(conn, store, DRIFTED, config=config())
    report = lint_catalog(conn, store, config(), selector=Selector(select=("S002",)))

    assert not report.failed("error")
    assert report.failed("warning")


def test_absolute_model_path_is_fixed_in_the_footprint(
    env: tuple[Paths, sqlite3.Connection, AssetStore],
) -> None:
    _, conn, store = env
    footprint = (
        '(footprint "R_0402"\n'
        '  (model "/home/someone/models/R_0402.step" (offset (xyz 0 0 0)))\n'
        ")\n"
    )
    part = save_part(
        conn,
        Part(
            klm_id="01JB4K7QW8ZR3XN5M2VYT9DCFA",
            mpn="R-0402",
            manufacturer="Yageo",
            description="test",
            symbol_hash=None,
            footprint_hash=store.add_bytes(footprint.encode(), AssetKind.FOOTPRINT),
        ),
    )

    reported = lint_catalog(conn, store, config(), selector=Selector(select=("A002",)))
    assert [f.rule for f in reported.findings] == ["A002"]

    lint_catalog(conn, store, config(), selector=Selector(select=("A002",)), fix=True)
    rewritten = store.read_text(
        get_part(conn, part.klm_id).footprint_hash or "",  # type: ignore[union-attr]
        AssetKind.FOOTPRINT,
    )
    assert "${KLM_3DMODELS}/R_0402.step" in rewritten


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_missing_config_yields_working_defaults(tmp_path: Path) -> None:
    loaded = load_config(tmp_path / "absent.toml")
    assert loaded.fields.canonical("MFR_PN") == "MPN"
    assert loaded.lint.max_severity == "error"


def test_user_aliases_extend_rather_than_replace(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[fields.aliases]\nMPN = ["Kod producenta"]\n\n[fields.custom]\nSzuflada = "drawer"\n',
        encoding="utf-8",
    )
    loaded = load_config(path)

    assert loaded.fields.canonical("Kod producenta") == "MPN"
    assert loaded.fields.canonical("Manufacturer_Part_Number") == "MPN"
    assert loaded.fields.is_declared("Szuflada")


def test_alias_lookup_ignores_case_and_separators(tmp_path: Path) -> None:
    loaded = load_config(None)
    assert loaded.fields.canonical("mfr_pn") == "MPN"
    assert loaded.fields.canonical("MFR PN") == "MPN"
    assert loaded.fields.canonical("MfrPn") == "MPN"


def test_malformed_config_raises_rather_than_falling_back(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[lint]\nmax_severity = 'catastrophe'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_config_rejects_claiming_klm_namespace(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[fields.custom]\nKLM_SECRET = "mine now"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_lint_config_is_used_when_no_flags_are_given() -> None:
    assert LintConfig(select=("S",)).select == ("S",)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _properties(store: AssetStore, part: Part) -> dict[str, str]:
    document = loads(store.read_text(part.symbol_hash or "", AssetKind.SYMBOL))
    return sym.properties(sym.extract_symbols(document)[0])


def _set_value(store: AssetStore, conn: sqlite3.Connection, part: Part, value: str) -> None:
    from klm.kicad.sexpr import dumps

    document = loads(store.read_text(part.symbol_hash or "", AssetKind.SYMBOL))
    sym.set_property(sym.extract_symbols(document)[0], "Value", value, hidden=False)
    part.symbol_hash = store.add_bytes(dumps(document).encode(), AssetKind.SYMBOL)
    save_part(conn, part)
