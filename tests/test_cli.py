"""Tests for the CLI entry points and path resolution."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from tests.projects import make_project, schematic, seed_resistor

from klm.assets.kicad_libs import default_libraries
from klm.cad.freecad import FreeCadUnavailable
from klm.cli.main import EXIT_CHECK_FAILED, EXIT_ERROR, EXIT_OK, main, parse_age
from klm.model import PartStatus
from klm.services.catalog import get_part, list_parts
from klm.services.stock import adjust
from klm.store import AssetKind, AssetStore, Paths, connect, resolve_home
from klm.store.db import SCHEMA_VERSION


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated catalog home, so tests never touch the real one."""
    target = tmp_path / "klm-home"
    monkeypatch.setenv("KLM_HOME", str(target))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return target


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_explicit_path_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KLM_HOME", str(tmp_path / "from-env"))
    assert resolve_home(tmp_path / "explicit") == (tmp_path / "explicit").resolve()


def test_klm_home_wins_over_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KLM_HOME", str(tmp_path / "klm"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_home() == (tmp_path / "klm").resolve()


def test_xdg_is_used_when_klm_home_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The POSIX branch. `sys.platform` is patched so it runs on Windows too.

    The alternative — skipping this on Windows and its sibling on Linux — means
    each branch is only ever exercised on one machine, which is how a platform
    path rots. `resolve_home` reads `sys.platform` at call time, so both are
    reachable from anywhere.
    """
    monkeypatch.setattr("klm.store.paths.sys.platform", "linux")
    monkeypatch.delenv("KLM_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_home() == (tmp_path / "xdg").resolve() / "klm"


def test_appdata_wins_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`%APPDATA%` outranks `XDG_DATA_HOME` on Windows, and is meant to.

    XDG is a freedesktop convention; a Windows user who happens to have the
    variable set — WSL, a stray dotfile, a shell that exports it — expects their
    catalog in `AppData\\Roaming`, not in a Unix-shaped path.
    """
    monkeypatch.setattr("klm.store.paths.sys.platform", "win32")
    monkeypatch.delenv("KLM_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_home() == (tmp_path / "appdata").resolve() / "klm"


def test_windows_without_appdata_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset `%APPDATA%` must not strand the catalog — it falls through."""
    monkeypatch.setattr("klm.store.paths.sys.platform", "win32")
    monkeypatch.delenv("KLM_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_home() == (tmp_path / "xdg").resolve() / "klm"


def test_resolving_a_home_does_not_create_it(tmp_path: Path) -> None:
    """Read-only commands must not bring a catalog into existence."""
    target = tmp_path / "untouched"
    Paths.resolve(target)
    assert not target.exists()


def test_layout_is_derived_from_a_single_root(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    assert paths.db == tmp_path / "catalog.db"
    assert paths.footprints == tmp_path / "assets" / "footprints"
    assert paths.generated_symbols == tmp_path / "generated" / "KLM.kicad_sym"
    assert all(d.is_relative_to(tmp_path) for d in paths.all_dirs())


# ---------------------------------------------------------------------------
# klm init
# ---------------------------------------------------------------------------


def test_init_creates_the_layout_and_schema(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == EXIT_OK

    paths = Paths(home)
    for directory in paths.all_dirs():
        assert directory.is_dir(), directory
    assert paths.db.exists()

    with sqlite3.connect(paths.db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    assert "catalog created" in capsys.readouterr().out


def test_init_is_safe_to_repeat(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == EXIT_OK
    capsys.readouterr()
    assert main(["init"]) == EXIT_OK
    assert "already exists" in capsys.readouterr().out


def test_init_force_reruns_migrations(home: Path) -> None:
    assert main(["init"]) == EXIT_OK
    assert main(["init", "--force"]) == EXIT_OK


def test_catalog_flag_overrides_the_environment(tmp_path: Path, home: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    assert main(["--catalog", str(elsewhere), "init"]) == EXIT_OK
    assert (elsewhere / "catalog.db").exists()
    assert not home.exists()


# ---------------------------------------------------------------------------
# klm doctor
# ---------------------------------------------------------------------------


def test_doctor_fails_before_init_and_says_what_to_run(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor"]) == EXIT_CHECK_FAILED
    out = capsys.readouterr().out
    assert "not initialised" in out
    assert "klm init" in out


def test_doctor_passes_on_a_fresh_catalog(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["init"])
    capsys.readouterr()
    assert main(["doctor"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "No issues found" in out
    assert f"version {SCHEMA_VERSION}" in out


def test_doctor_names_the_consequence_of_a_missing_tool(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing optional tool degrades klm; it must never look like a crash."""
    monkeypatch.setattr("shutil.which", lambda _name: None)
    main(["init"])
    capsys.readouterr()

    main(["doctor"])
    out = capsys.readouterr().out
    assert "kicad-cli" in out
    assert "not found" in out
    assert "install:" in out


def test_doctor_reports_a_missing_api_key_as_a_warning_not_a_failure(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    capsys.readouterr()
    assert main(["doctor"]) == EXIT_OK
    assert "agent features unavailable" in capsys.readouterr().out


def test_doctor_reports_a_schema_older_than_this_klm(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    with sqlite3.connect(Paths(home).db) as conn:
        conn.execute("PRAGMA user_version = 0")
    capsys.readouterr()

    assert main(["doctor"]) == EXIT_CHECK_FAILED
    assert "expected" in capsys.readouterr().out


def test_doctor_reports_a_schema_newer_than_this_klm(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    with sqlite3.connect(Paths(home).db) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 99}")
    capsys.readouterr()

    assert main(["doctor"]) == EXIT_CHECK_FAILED
    assert "upgrade klm" in capsys.readouterr().out


def test_doctor_counts_assets(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["init"])
    store = AssetStore(Paths(home).assets)
    store.add_bytes(b'(footprint "R")', AssetKind.FOOTPRINT)
    store.add_bytes(b'(symbol "R")', AssetKind.SYMBOL)
    capsys.readouterr()

    main(["doctor"])
    out = capsys.readouterr().out
    assert "2 (" in out and "footprint" in out


def test_doctor_deep_detects_a_corrupted_asset(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    store = AssetStore(Paths(home).assets)
    digest = store.add_bytes(b'(footprint "R")', AssetKind.FOOTPRINT)
    store.path_for(digest, AssetKind.FOOTPRINT).write_bytes(b'(footprint "TAMPERED")')
    capsys.readouterr()

    assert main(["doctor", "--deep"]) == EXIT_CHECK_FAILED
    assert "do not match their content hash" in capsys.readouterr().out


def test_doctor_deep_passes_on_intact_assets(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["init"])
    AssetStore(Paths(home).assets).add_bytes(b'(footprint "R")', AssetKind.FOOTPRINT)
    capsys.readouterr()

    assert main(["doctor", "--deep"]) == EXIT_OK
    assert "verified" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------


def test_bare_invocation_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    assert "usage:" in capsys.readouterr().out


def test_version_flag_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "klm" in capsys.readouterr().out


def test_unexpected_errors_become_exit_code_2(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr("klm.cli.main.connect", explode)
    assert main(["init"]) == EXIT_ERROR
    assert "disk on fire" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# import --from-kicad / lint / hook
# ---------------------------------------------------------------------------

DRIFTED = Path(__file__).parent / "fixtures" / "drifted_library.kicad_sym"


def test_import_from_kicad_reports_what_it_skipped(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    # A library with a derived symbol is partially imported, and says so.
    assert main(["import", "--from-kicad", str(DRIFTED)]) == EXIT_CHECK_FAILED
    out = capsys.readouterr().out
    assert "imported 2 symbol(s)" in out
    assert "R_Small_Derived" in out


def test_import_from_kicad_rejects_a_missing_file(home: Path) -> None:
    assert main(["init"]) == EXIT_OK
    assert main(["import", "--from-kicad", str(home / "nope.kicad_sym")]) == EXIT_ERROR


def test_lint_exit_code_follows_max_severity(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    main(["import", "--from-kicad", str(DRIFTED), "--category", "Passive/Resistor"])
    capsys.readouterr()

    assert main(["lint", "--select", "S002"]) == EXIT_OK
    assert main(["lint", "--select", "S002", "--max-severity", "warning"]) == EXIT_CHECK_FAILED


def test_lint_fix_leaves_the_catalog_clean_of_fixable_findings(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    main(["import", "--from-kicad", str(DRIFTED), "--category", "Passive/Resistor"])
    capsys.readouterr()

    assert main(["lint", "--select", "S002,V001", "--fix"]) == EXIT_OK
    assert "fixed" in capsys.readouterr().out
    assert main(["lint", "--select", "S002,V001", "--max-severity", "warning"]) == EXIT_OK


def test_lint_json_output_is_machine_readable(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    main(["import", "--from-kicad", str(DRIFTED)])
    capsys.readouterr()

    main(["lint", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["parts_checked"] == 2
    assert {"rule", "severity", "location", "message", "fixable", "fixed"} <= set(
        payload["findings"][0]
    )


def test_lint_rules_listing_needs_no_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lint", "--rules"]) == EXIT_OK
    assert "S002" in capsys.readouterr().out


def test_hook_writes_a_config_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert main(["hook", str(repo)]) == EXIT_OK
    assert "klm-lint" in (repo / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert main(["hook", "--check", str(repo)]) == EXIT_OK
    assert main(["hook", str(repo)]) == EXIT_OK


def test_hook_never_rewrites_someone_elses_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = "repos:\n  - repo: local\n    hooks: []\n"
    (repo / ".pre-commit-config.yaml").write_text(existing, encoding="utf-8")

    assert main(["hook", "--check", str(repo)]) == EXIT_CHECK_FAILED
    assert main(["hook", str(repo)]) == EXIT_CHECK_FAILED
    assert (repo / ".pre-commit-config.yaml").read_text(encoding="utf-8") == existing
    assert "klm-lint" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# refresh / offers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "days"),
    [("7d", 7), ("30", 30), ("2w", 14), ("24h", 1), ("36h", 2), ("1h", 1)],
)
def test_age_strings_round_up_to_whole_days(raw: str, days: int) -> None:
    assert parse_age(raw) == days


@pytest.mark.parametrize("raw", ["", "soon", "-3d", "0d"])
def test_a_nonsense_age_is_refused_rather_than_guessed(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_age(raw)


def _imported(home: Path) -> str:
    """Import the fixture library and return one part's id.

    The fixture holds a derived symbol klm skips, so the import legitimately
    exits non-zero. That is `test_import_from_kicad_*`'s business, not this
    helper's.
    """
    assert main(["init"]) == EXIT_OK
    main(["import", "--from-kicad", str(DRIFTED)])
    conn = connect(Paths(home).db, create=False)
    try:
        return list_parts(conn)[0].klm_id
    finally:
        conn.close()


def test_offers_on_an_empty_catalog_says_so_and_names_the_next_step(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _imported(home)
    capsys.readouterr()

    assert main(["offers"]) == EXIT_OK
    assert "no offers recorded" in capsys.readouterr().out


def test_offers_add_records_a_manual_lcsc_offer(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    klm_id = _imported(home)
    capsys.readouterr()

    assert (
        main(
            [
                "offers", klm_id,
                "--supplier", "lcsc",
                "--add", "C25900",
                "--price", "0.0012",
                "--stock", "100000",
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()

    main(["offers", klm_id, "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["supplier_pn"] == "C25900"
    assert payload[0]["unit_price"] == pytest.approx(0.0012)
    assert payload[0]["match_confidence"] == "high"
    assert payload[0]["url"].endswith("C25900.html")


def test_offers_add_needs_a_part_and_a_supplier(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _imported(home)
    capsys.readouterr()

    assert main(["offers", "--add", "C25900"]) == EXIT_ERROR


def test_offers_remove_reports_whether_there_was_one(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    klm_id = _imported(home)
    main(["offers", klm_id, "--supplier", "lcsc", "--add", "C25900"])
    capsys.readouterr()

    assert main(["offers", "--supplier", "lcsc", "--remove", "C25900"]) == EXIT_OK
    assert main(["offers", "--supplier", "lcsc", "--remove", "C25900"]) == EXIT_CHECK_FAILED


def test_an_unknown_part_reference_is_an_error_not_a_silent_empty_list(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _imported(home)
    capsys.readouterr()

    assert main(["offers", "no-such-part"]) == EXIT_ERROR
    assert "no part with id or MPN" in capsys.readouterr().err


def test_a_part_can_be_named_by_its_mpn(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    klm_id = _imported(home)
    conn = connect(Paths(home).db, create=False)
    try:
        mpn = get_part(conn, klm_id).mpn  # type: ignore[union-attr]
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["offers", mpn, "--supplier", "lcsc", "--add", "C25900"]) == EXIT_OK
    assert klm_id in capsys.readouterr().out


def test_refresh_offline_with_a_cold_cache_degrades_instead_of_crashing(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TME has no credentials here, so it reports as unconfigured — once."""
    monkeypatch.delenv("TME_API_KEY", raising=False)
    monkeypatch.delenv("TME_API_SECRET", raising=False)
    _imported(home)
    capsys.readouterr()

    assert main(["refresh", "--offline"]) == EXIT_CHECK_FAILED
    out = capsys.readouterr().out
    assert out.count("TME needs an application token") == 1
    assert "klm keeps what it had" in out


def test_refresh_names_a_supplier_that_is_not_enabled(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _imported(home)
    capsys.readouterr()

    assert main(["refresh", "--supplier", "mouser"]) == EXIT_CHECK_FAILED
    assert "no enabled supplier named mouser" in capsys.readouterr().out


def test_refresh_does_not_reach_the_network_for_a_manual_supplier(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """LCSC defaults to manual mode, which must be quiet rather than failing."""
    _imported(home)
    capsys.readouterr()

    assert main(["refresh", "--supplier", "lcsc"]) == EXIT_OK
    assert "0 newly linked" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# part add / assets
# ---------------------------------------------------------------------------

KICAD_FIXTURES = Path(__file__).parent / "fixtures" / "kicad"


@pytest.fixture
def kicad_libs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point library discovery at the fixture tree, and clear its cache."""
    monkeypatch.setenv("KICAD9_SYMBOL_DIR", str(KICAD_FIXTURES / "symbols"))
    monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(KICAD_FIXTURES / "footprints"))
    default_libraries.cache_clear()
    yield
    default_libraries.cache_clear()


def test_part_add_creates_a_draft_with_assets(
    home: Path, kicad_libs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    capsys.readouterr()

    assert (
        main(
            [
                "part", "add",
                "--mpn", "RC0402FR-074K7L",
                "--mfr", "Yageo",
                "--category", "Passive/Resistor",
                "--package", "0402",
                "--value", "4700",
                "--description", "4.7k 1% 0402",
                "--field", "Tolerance=1%",
                "--field", "Power=0.063W",
                "--datasheet", "https://example.invalid/ds.pdf",
                "--offline",
            ]
        )
        == EXIT_OK
    )
    out = capsys.readouterr().out
    assert "created" in out
    assert "footprint" in out

    conn = connect(Paths(home).db, create=False)
    try:
        (part,) = list_parts(conn)
    finally:
        conn.close()
    assert part.status is PartStatus.DRAFT
    assert part.symbol_hash and part.footprint_hash


def test_a_part_added_by_klm_lints_clean(
    home: Path, kicad_libs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The point of the pipeline: what it produces does not need fixing."""
    assert main(["init"]) == EXIT_OK
    main(
        [
            "part", "add",
            "--mpn", "RC0402FR-074K7L",
            "--mfr", "Yageo",
            "--category", "Passive/Resistor",
            "--package", "0402",
            "--value", "0.0047k",
            "--description", "4.7k 1% 0402",
            "--field", "Tolerance=1%",
            "--field", "Power=0.063W",
            "--offline",
        ]
    )
    capsys.readouterr()

    assert main(["lint", "--select", "S,V", "--max-severity", "warning"]) == EXIT_OK


def test_the_value_is_normalized_on_the_way_in(
    home: Path, kicad_libs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    main(
        [
            "part", "add",
            "--mpn", "CL05B104KO5NNNC",
            "--mfr", "Samsung",
            "--category", "Passive/Capacitor/Ceramic",
            "--package", "0402",
            "--value", "0.1uF",
            "--offline",
        ]
    )
    capsys.readouterr()

    conn = connect(Paths(home).db, create=False)
    store = AssetStore(Paths(home).assets)
    try:
        (part,) = list_parts(conn)
    finally:
        conn.close()
    assert part.symbol_hash is not None
    assert '"100nF"' in store.read_text(part.symbol_hash, AssetKind.SYMBOL)


def test_a_malformed_field_pair_is_refused(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    capsys.readouterr()

    assert main(["part", "add", "--mpn", "X", "--field", "Tolerance", "--offline"]) == EXIT_ERROR


def test_assets_qa_reports_and_exits_zero_when_clean(
    home: Path, kicad_libs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    main([
        "part", "add", "--mpn", "RC0402FR-074K7L", "--mfr", "Yageo",
        "--category", "Passive/Resistor", "--package", "0402", "--offline",
    ])
    capsys.readouterr()

    assert main(["assets", "qa"]) == EXIT_OK
    assert "no blocking failures" in capsys.readouterr().out


def test_assets_qa_json_is_machine_readable(
    home: Path, kicad_libs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    main([
        "part", "add", "--mpn", "RC0402FR-074K7L", "--mfr", "Yageo",
        "--category", "Passive/Resistor", "--package", "0402", "--offline",
    ])
    capsys.readouterr()

    main(["assets", "qa", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    (reports,) = payload.values()
    assert reports["symbol"]["status"] == "pass"


def test_assets_acquire_on_an_already_complete_part_does_nothing(
    home: Path, kicad_libs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    main([
        "part", "add", "--mpn", "RC0402FR-074K7L", "--mfr", "Yageo",
        "--category", "Passive/Resistor", "--package", "0402", "--offline",
    ])
    capsys.readouterr()

    assert main(["assets", "acquire", "RC0402FR-074K7L"]) == EXIT_OK
    assert "nothing to do" in capsys.readouterr().out


def test_reuse_check_is_quiet_on_a_healthy_catalog(
    home: Path, kicad_libs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    main([
        "part", "add", "--mpn", "RC0402FR-074K7L", "--mfr", "Yageo",
        "--category", "Passive/Resistor", "--package", "0402", "--offline",
    ])
    capsys.readouterr()

    assert main(["assets", "reuse-check"]) == EXIT_OK
    assert "no duplicate footprints" in capsys.readouterr().out


def test_convert_3d_without_freecad_degrades(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("klm.cli.main.convert_mesh", _no_freecad)
    assert main(["init"]) == EXIT_OK
    mesh = tmp_path / "part.obj"
    mesh.write_text("v 0 0 0\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["assets", "convert-3d", str(mesh)]) == EXIT_CHECK_FAILED
    assert "freecadcmd" in capsys.readouterr().out


def _no_freecad(*args: object, **kwargs: object) -> None:
    raise FreeCadUnavailable("freecadcmd was not found; 3D models stay as meshes.")


# ---------------------------------------------------------------------------
# vendor / sync / promote
# ---------------------------------------------------------------------------


def _vendored_project(home: Path, tmp_path: Path, capsys) -> Path:
    """An initialised catalog with one approved part, and a project using it."""
    assert main(["init"]) == EXIT_OK
    paths = Paths(home)
    conn = connect(paths.db)
    try:
        seed_resistor(AssetStore(paths.assets), conn)
    finally:
        conn.close()
    root = make_project(tmp_path / "proj")
    capsys.readouterr()
    return root


def test_vendor_and_sync_status_round_trip(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)

    assert main(["vendor", "--project", str(root), "--with-3d"]) == EXIT_OK
    assert "vendored 1 symbols" in capsys.readouterr().out

    assert main(["sync", "status", "--project", str(root), "--exit-code"]) == EXIT_OK
    assert "in step with the catalog" in capsys.readouterr().out


def test_sync_status_json_lists_every_part(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    main(["vendor", "--project", str(root)])
    capsys.readouterr()

    assert main(["sync", "status", "--project", str(root), "--format", "json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["library_name"] == "my-board"
    assert payload["parts"][0]["state"] == "clean"


def test_vendor_dry_run_writes_nothing(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    assert main(["vendor", "--project", str(root), "--dry-run"]) == EXIT_OK
    assert "nothing written" in capsys.readouterr().out
    assert not (root / "klm.lock.json").exists()


def test_vendor_reports_what_it_cannot_resolve(home: Path, tmp_path: Path, capsys) -> None:
    """A half-vendored project opens for its author and for nobody else."""
    assert main(["init"]) == EXIT_OK
    root = make_project(tmp_path / "proj", sheet=schematic(klm_id=None))

    assert main(["vendor", "--project", str(root)]) == EXIT_CHECK_FAILED
    out = capsys.readouterr().out
    assert "no approved catalog part" in out
    assert not (root / "libraries").exists()


def test_sync_status_flags_drift_with_exit_code(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    main(["vendor", "--project", str(root)])
    target = root / "libraries" / "my-board.pretty" / "R_0402_1005Metric.kicad_mod"
    target.write_text(target.read_text(encoding="utf-8").replace("0.56", "0.60"), encoding="utf-8")
    capsys.readouterr()

    assert main(["sync", "status", "--project", str(root), "--exit-code"]) == EXIT_CHECK_FAILED
    assert "project-ahead" in capsys.readouterr().out


def test_sync_push_then_clean(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    main(["vendor", "--project", str(root)])
    target = root / "libraries" / "my-board.pretty" / "R_0402_1005Metric.kicad_mod"
    target.write_text(target.read_text(encoding="utf-8").replace("0.56", "0.60"), encoding="utf-8")
    capsys.readouterr()

    assert main(["sync", "push", "--project", str(root)]) == EXIT_OK
    assert main(["sync", "status", "--project", str(root), "--exit-code"]) == EXIT_OK


def test_unvendor_restores_the_project(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    before = (root / "my-board.kicad_sch").read_text(encoding="utf-8")
    main(["vendor", "--project", str(root)])
    capsys.readouterr()

    assert main(["unvendor", "--project", str(root)]) == EXIT_OK
    assert (root / "my-board.kicad_sch").read_text(encoding="utf-8") == before
    assert not (root / "klm.lock.json").exists()


def test_sync_adopt_rebuilds_the_lock(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    main(["vendor", "--project", str(root)])
    (root / "klm.lock.json").unlink()
    capsys.readouterr()

    assert main(["sync", "adopt", "--project", str(root)]) == EXIT_OK
    assert "rebuilt the lock" in capsys.readouterr().out
    assert (root / "klm.lock.json").exists()


def test_promote_refuses_a_part_that_is_not_an_orphan(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    main(["vendor", "--project", str(root)])
    capsys.readouterr()

    assert main(["promote", "RC0402FR-074K7L", "--project", str(root)]) == EXIT_CHECK_FAILED
    assert "no orphan part matches" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# bom / fab
# ---------------------------------------------------------------------------


def test_bom_lists_and_flags_unresolved(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    assert main(["bom", "--project", str(root)]) == EXIT_OK
    assert "RC0402FR-074K7L" not in capsys.readouterr().out  # grouped by Value


def test_bom_json_carries_the_designators(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    assert main(["bom", "--project", str(root), "--format", "json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["lines"][0]["designators"] == "R1"


def test_an_unknown_variant_names_the_ones_that_exist(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    (root / "klm.toml").write_text('[variants.basic]\ndnp = ["R1"]\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="basic"):
        main(["bom", "--project", str(root), "--variant", "nonesuch"])


def test_a_variant_from_the_project_toml_is_applied(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    (root / "klm.toml").write_text('[variants.basic]\ndnp = ["R1"]\n', encoding="utf-8")
    assert main(["bom", "--project", str(root), "--variant", "basic"]) == EXIT_OK
    assert "not populated" in capsys.readouterr().out


def test_an_empty_correction_table_explains_itself(home: Path, capsys) -> None:
    """ADR-0011: empty is the expected state, and an empty screen reads as a bug."""
    assert main(["init"]) == EXIT_OK
    capsys.readouterr()
    assert main(["fab", "corrections", "list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "no corrections recorded yet" in out
    assert "adr/0011" in out


def test_corrections_can_be_set_and_removed(home: Path, capsys) -> None:
    assert main(["init"]) == EXIT_OK
    capsys.readouterr()

    assert main(["fab", "corrections", "set", "^SOT-23", "180"]) == EXIT_OK
    assert main(["fab", "corrections", "list"]) == EXIT_OK
    assert "^SOT-23" in capsys.readouterr().out

    assert main(["fab", "corrections", "remove", "^SOT-23"]) == EXIT_OK
    assert main(["fab", "corrections", "remove", "^SOT-23"]) == EXIT_CHECK_FAILED


def test_feedback_wants_a_reference_and_a_number(home: Path, tmp_path: Path, capsys) -> None:
    assert main(["init"]) == EXIT_OK
    with pytest.raises(SystemExit, match="REF:DEGREES"):
        main(["fab", "feedback", str(tmp_path), "--wrong", "U3"])


# ---------------------------------------------------------------------------
# verify / scaffold / report
# ---------------------------------------------------------------------------


def test_verify_fails_a_linked_project_and_passes_a_vendored_one(
    home: Path, tmp_path: Path, capsys
) -> None:
    root = _vendored_project(home, tmp_path, capsys)

    assert main(["verify", "--clean-room", "--project", str(root)]) == EXIT_CHECK_FAILED
    assert "partly vendored" in capsys.readouterr().out

    assert main(["vendor", "--project", str(root)]) == EXIT_OK
    capsys.readouterr()
    assert main(["verify", "--clean-room", "--project", str(root)]) == EXIT_OK
    assert "enough to open the project" in capsys.readouterr().out


def test_verify_github_format_emits_annotations(home: Path, tmp_path: Path, capsys) -> None:
    """A reviewer should see the offending lib_id in the diff, not in a log."""
    root = _vendored_project(home, tmp_path, capsys)
    capsys.readouterr()

    main(["verify", "--clean-room", "--project", str(root), "--format", "github"])
    out = capsys.readouterr().out
    assert out.startswith("::error")
    assert "file=my-board.kicad_sch" in out


def test_verify_json_format_is_machine_readable(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    main(["vendor", "--project", str(root)])
    capsys.readouterr()

    main(["verify", "--clean-room", "--project", str(root), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] is False
    assert any(c["name"] == "symbol resolution" for c in payload["checks"])


def test_scaffold_then_check_is_clean(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    assert main(["scaffold", "--project", str(root), "--preset", "private"]) == EXIT_OK
    capsys.readouterr()

    assert (
        main(["scaffold", "--project", str(root), "--preset", "private", "--check"]) == EXIT_OK
    )
    assert "matches klm" in capsys.readouterr().out


def test_check_against_a_larger_preset_reports_what_is_missing(
    home: Path, tmp_path: Path, capsys
) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    main(["scaffold", "--project", str(root), "--preset", "private"])
    capsys.readouterr()

    assert main(["scaffold", "--project", str(root), "--preset", "publish", "--check"]) == (
        EXIT_CHECK_FAILED
    )
    assert "release.yml" in capsys.readouterr().out


def test_scaffold_check_reports_drift(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    main(["scaffold", "--project", str(root)])
    (root / ".gitignore").write_text("# gutted\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["scaffold", "--project", str(root), "--check"]) == EXIT_CHECK_FAILED
    assert ".gitignore" in capsys.readouterr().out


def test_report_renders_a_markdown_table(home: Path, tmp_path: Path, capsys) -> None:
    root = _vendored_project(home, tmp_path, capsys)
    assert main(["report", "--project", str(root)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "| BOM lines | 1 |" in out
    assert "<details>" in out


# ---------------------------------------------------------------------------
# part approve / deprecate / show
# ---------------------------------------------------------------------------
#
# These exist because the GUI had them and the CLI did not, which by this
# project's own rule is a bug in the CLI rather than a feature of the GUI.


def test_part_approve_and_deprecate_move_the_status(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    store = AssetStore(Paths(home).assets)
    conn = connect(Paths(home).db, create=False)
    try:
        part = seed_resistor(store, conn, status=PartStatus.DRAFT)
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["part", "approve", part.mpn]) == EXIT_OK
    assert "draft → approved" in capsys.readouterr().out

    assert main(["part", "deprecate", part.klm_id]) == EXIT_OK
    out = capsys.readouterr().out
    assert "approved → deprecated" in out
    # S005: a deprecated part stops being generated, and anything already using
    # it keeps working from its vendored copy. Worth saying out loud.
    assert "stop being generated" in out

    conn = connect(Paths(home).db, create=False)
    try:
        stored = get_part(conn, part.klm_id)
    finally:
        conn.close()
    assert stored is not None and stored.status is PartStatus.DEPRECATED


def test_approving_twice_is_not_an_error(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Idempotent, because a script that runs twice should not fail the second time."""
    assert main(["init"]) == EXIT_OK
    conn = connect(Paths(home).db, create=False)
    try:
        part = seed_resistor(AssetStore(Paths(home).assets), conn)
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["part", "approve", part.mpn]) == EXIT_OK
    assert "already approved" in capsys.readouterr().out


def test_approving_an_unknown_part_says_so(home: Path) -> None:
    assert main(["init"]) == EXIT_OK
    assert main(["part", "approve", "NOT-A-PART"]) == EXIT_ERROR


def test_part_show_reports_offers_and_stock(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == EXIT_OK
    conn = connect(Paths(home).db, create=False)
    try:
        part = seed_resistor(AssetStore(Paths(home).assets), conn)
        adjust(conn, part.klm_id, "Cabinet A/1", set_to=42)
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["part", "show", part.mpn]) == EXIT_OK
    out = capsys.readouterr().out
    assert "RC0402FR-074K7L" in out
    assert "Cabinet A/1" in out
    assert "none — `klm refresh` fetches them" in out


def test_part_show_json_matches_what_the_api_serves(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI and the GUI must describe a part the same way (docs/12 §1)."""
    assert main(["init"]) == EXIT_OK
    conn = connect(Paths(home).db, create=False)
    try:
        part = seed_resistor(AssetStore(Paths(home).assets), conn)
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["part", "show", part.klm_id, "--format", "json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["klm_id"] == part.klm_id
    assert payload["mpn"] == part.mpn
    assert payload["status"] == str(part.status)
    assert payload["symbol_hash"] == part.symbol_hash


def test_output_survives_a_windows_code_page(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """klm's glyphs must not turn into a crash on a cp1252 stream.

    This is the Windows failure exactly: `klm doctor` prints `✗`, and a
    *redirected* stream on Windows encodes with the ANSI code page, which has
    no such character. It worked in a terminal and died the moment anyone
    wrote it to a file — so the regression test writes to one.
    """
    assert main(["init"]) == EXIT_OK

    buffer = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", buffer)
    monkeypatch.setattr(sys, "stderr", buffer)

    code = main(["doctor"])

    buffer.flush()
    written = buffer.buffer.getvalue().decode("utf-8", errors="replace")  # type: ignore[attr-defined]
    assert code in (EXIT_OK, EXIT_CHECK_FAILED), "a code page must not become an error"
    assert "catalog home" in written
    assert "charmap" not in written, "the encoding must not leak into the output"


REQUIREMENT = """\
kind = "buck_converter"

[constraints]
vin = { min = 4.5, max = 18, unit = "V" }
iout = ">=1A"

[[preferences]]
kind = "minimize"
what = "unit_price"
"""


def test_research_check_reads_a_requirement(tmp_path: Path, capsys) -> None:
    path = tmp_path / "req.toml"
    path.write_text(REQUIREMENT, encoding="utf-8")

    assert main(["research", "check", str(path)]) == EXIT_OK

    out = capsys.readouterr().out
    assert "vin: 4.5V..18V" in out
    assert "minimize unit_price" in out


def test_research_check_needs_no_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A requirement is checkable on a machine that has nothing set up."""
    monkeypatch.setenv("KLM_HOME", str(tmp_path / "nowhere"))
    path = tmp_path / "req.toml"
    path.write_text(REQUIREMENT, encoding="utf-8")
    assert main(["research", "check", str(path)]) == EXIT_OK


def test_research_check_reports_every_problem_and_fails(tmp_path: Path, capsys) -> None:
    path = tmp_path / "req.toml"
    path.write_text('[constrains]\nvin = ">=5V"\n', encoding="utf-8")

    assert main(["research", "check", str(path)]) == EXIT_CHECK_FAILED

    out = capsys.readouterr().out
    assert "unknown section 'constrains'" in out
    assert "'kind' is required" in out


def test_research_check_writes_back_what_klm_understood(tmp_path: Path, capsys) -> None:
    path = tmp_path / "req.toml"
    path.write_text(REQUIREMENT, encoding="utf-8")

    assert main(["research", "check", str(path), "--toml"]) == EXIT_OK

    written = capsys.readouterr().out
    assert 'iout = { min = 1.0, unit = "A" }' in written, "the shorthand is shown resolved"


def test_research_tools_lists_what_the_agent_could_do(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    assert main(["init"]) == EXIT_OK
    monkeypatch.setenv("TME_API_KEY", "key")
    monkeypatch.setenv("TME_API_SECRET", "secret")

    assert main(["research", "tools"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "catalog_search" in out and "supplier_search" in out
    assert "no tool writes to the catalog" in out
    assert "lcsc: manual mode" in out, "an absent tool says why it is absent"


def test_research_tools_without_credentials_offers_no_supplier(home: Path, capsys) -> None:
    """Learning this from a session that returned nothing useful is expensive."""
    assert main(["init"]) == EXIT_OK

    assert main(["research", "tools"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "supplier_search" not in out
    assert "tme: no credentials" in out


def test_research_run_needs_the_agent_extra(home: Path, tmp_path: Path, capsys) -> None:
    """No SDK is a thing to report, not a traceback."""
    assert main(["init"]) == EXIT_OK
    path = tmp_path / "req.toml"
    path.write_text(REQUIREMENT, encoding="utf-8")

    assert main(["research", "run", str(path)]) == EXIT_CHECK_FAILED

    out = capsys.readouterr().out
    assert "klm[agent]" in out


def test_research_run_checks_the_requirement_before_spending_anything(
    home: Path, tmp_path: Path, capsys
) -> None:
    assert main(["init"]) == EXIT_OK
    path = tmp_path / "req.toml"
    path.write_text('[constrains]\nvin = ">=5V"\n', encoding="utf-8")

    assert main(["research", "run", str(path)]) == EXIT_CHECK_FAILED

    out = capsys.readouterr().out
    assert "unknown section 'constrains'" in out
    assert "klm[agent]" not in out, "the file is read before a model is built"
