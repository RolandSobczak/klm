"""Tests for the CLI entry points and path resolution."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from klm.cli.main import EXIT_CHECK_FAILED, EXIT_ERROR, EXIT_OK, main
from klm.store import AssetKind, AssetStore, Paths, resolve_home
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
    monkeypatch.delenv("KLM_HOME", raising=False)
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
