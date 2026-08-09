"""Tests for identity, the part model, the catalog repository and export/import."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from klm import ids
from klm.model import Confidence, Lifecycle, Parameter, Part, PartStatus, SourceKind
from klm.serial.part_file import from_yaml, to_yaml
from klm.serial.yaml import YamlError
from klm.services.catalog import (
    count_parts,
    delete_part,
    get_part,
    list_parts,
    normalize_manufacturer,
    save_part,
)
from klm.services.exporter import export_catalog, import_catalog
from klm.store.db import connect, migrate


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "catalog.db")
    migrate(connection)
    yield connection
    connection.close()


def make_part(**overrides: object) -> Part:
    defaults: dict[str, object] = {
        "klm_id": "01JB4K7QW8ZR3XN5M2VYT9DCFA",
        "mpn": "STM32F103C8T6",
        "manufacturer": "STMicroelectronics",
        "description": "ARM Cortex-M3 MCU, 64 kB flash, LQFP-48",
        "category": "IC/Microcontroller/ARM",
        "package": "LQFP-48",
        "lifecycle": Lifecycle.ACTIVE,
        "status": PartStatus.APPROVED,
        "datasheet_url": "https://example.com/ds.pdf",
        "symbol_hash": "sha256:" + "a" * 64,
        "parameters": [
            Parameter(
                name="vdd_max",
                source=SourceKind.DATASHEET,
                value_num=3.6,
                unit="V",
                source_ref="page 12",
                confidence=Confidence.HIGH,
            ),
            Parameter(name="flash", source=SourceKind.DATASHEET, value_num=65536.0, unit="B"),
        ],
    }
    defaults.update(overrides)
    return Part(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_generated_ids_are_valid_and_unique() -> None:
    generated = {ids.new_id() for _ in range(500)}
    assert len(generated) == 500
    assert all(ids.is_valid(i) for i in generated)
    assert all(len(i) == ids.ULID_LENGTH for i in generated)


def test_ids_sort_by_creation_time() -> None:
    earlier = ids.new_id(timestamp_ms=1_700_000_000_000, randomness=b"\xff" * 10)
    later = ids.new_id(timestamp_ms=1_700_000_001_000, randomness=b"\x00" * 10)
    assert earlier < later


def test_timestamp_survives_the_round_trip() -> None:
    stamp = 1_754_700_000_000
    assert ids.timestamp_of(ids.new_id(timestamp_ms=stamp)) == stamp


def test_alphabet_excludes_ambiguous_characters() -> None:
    """The ID gets read off a drawer label; I/L/O/U must not appear."""
    generated = "".join(ids.new_id() for _ in range(200))
    assert not (set(generated) & set("ILOU"))


def test_transcription_lookalikes_are_accepted_on_input() -> None:
    klm_id = ids.new_id(timestamp_ms=1_700_000_000_000, randomness=b"\x00" * 10)
    assert ids.is_valid(klm_id.lower())


@pytest.mark.parametrize("bad", ["", "short", "!" * 26, "x" * 25])
def test_invalid_ids_are_rejected(bad: str) -> None:
    assert not ids.is_valid(bad)
    with pytest.raises(ValueError):
        ids.timestamp_of(bad)


def test_short_id_comes_from_the_end_not_the_timestamp() -> None:
    """Parts created in one session share a timestamp prefix."""
    a = ids.new_id(timestamp_ms=1_700_000_000_000, randomness=b"\x01" * 10)
    b = ids.new_id(timestamp_ms=1_700_000_000_000, randomness=b"\x02" * 10)
    assert ids.short_id(a) != ids.short_id(b)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_parameter_lookup_prefers_the_most_trustworthy_source() -> None:
    part = make_part(
        parameters=[
            Parameter(name="vdd_max", source=SourceKind.SUPPLIER, value_num=3.3),
            Parameter(name="vdd_max", source=SourceKind.DATASHEET, value_num=3.6),
        ]
    )
    found = part.parameter("vdd_max")
    assert found is not None and found.source is SourceKind.DATASHEET


def test_conflicts_surfaces_disagreeing_sources() -> None:
    """A datasheet and a supplier disagreeing is reported, never resolved."""
    part = make_part(
        parameters=[
            Parameter(name="vdd_max", source=SourceKind.SUPPLIER, value_num=3.3),
            Parameter(name="vdd_max", source=SourceKind.DATASHEET, value_num=3.6),
            Parameter(name="flash", source=SourceKind.DATASHEET, value_num=65536.0),
        ]
    )
    conflicts = part.conflicts()
    assert [name for name, _ in conflicts] == ["vdd_max"]


def test_agreeing_sources_are_not_a_conflict() -> None:
    part = make_part(
        parameters=[
            Parameter(name="vdd_max", source=SourceKind.SUPPLIER, value_num=3.6),
            Parameter(name="vdd_max", source=SourceKind.DATASHEET, value_num=3.6),
        ]
    )
    assert part.conflicts() == []


# ---------------------------------------------------------------------------
# part.yaml
# ---------------------------------------------------------------------------


def test_part_yaml_round_trip() -> None:
    """Content survives exactly.

    Parameters come back in canonical order rather than insertion order, because
    the export sorts them so its bytes do not depend on how the part happened to
    be built. Order carries no meaning — a parameter's identity is its name and
    source.
    """
    part = make_part()
    restored = from_yaml(to_yaml(part))
    assert restored == replace(part, parameters=part.sorted_parameters())


def test_part_yaml_is_byte_stable() -> None:
    part = make_part()
    assert to_yaml(part) == to_yaml(from_yaml(to_yaml(part)))


def test_parameters_are_written_in_a_stable_order() -> None:
    """Export bytes must not depend on the order parameters happen to be in."""
    a = make_part()
    b = make_part(parameters=list(reversed(a.parameters)))
    assert to_yaml(a) == to_yaml(b)


def test_unset_optional_fields_are_omitted_not_written_as_null() -> None:
    text = to_yaml(Part(klm_id="01A", mpn="X", manufacturer="Y"))
    assert "notes" not in text
    assert "null" not in text


def test_identity_fields_come_first() -> None:
    lines = to_yaml(make_part()).splitlines()
    assert lines[0].startswith("format_version:")
    assert [line.split(":")[0] for line in lines[1:4]] == ["klm_id", "mpn", "manufacturer"]


def test_a_newer_format_version_is_refused() -> None:
    text = to_yaml(make_part()).replace("format_version: 1", "format_version: 99")
    with pytest.raises(YamlError, match="newer than this klm understands"):
        from_yaml(text)


@pytest.mark.parametrize("field", ["klm_id", "mpn", "manufacturer"])
def test_missing_required_fields_are_refused(field: str) -> None:
    text = "\n".join(
        line for line in to_yaml(make_part()).splitlines() if not line.startswith(f"{field}:")
    )
    with pytest.raises(YamlError, match=f"missing required field '{field}'"):
        from_yaml(text)


def test_an_unknown_enum_value_is_refused_with_the_valid_options() -> None:
    text = to_yaml(make_part()).replace("status: approved", "status: probably-fine")
    with pytest.raises(YamlError, match="invalid status") as exc:
        from_yaml(text)
    assert "approved" in str(exc.value)


# ---------------------------------------------------------------------------
# Catalog repository
# ---------------------------------------------------------------------------


def test_save_then_get_round_trips(conn: sqlite3.Connection) -> None:
    saved = save_part(conn, make_part())
    fetched = get_part(conn, saved.klm_id)
    assert fetched is not None
    assert fetched.mpn == "STM32F103C8T6"
    assert fetched.manufacturer == "STMicroelectronics"
    assert fetched.category == "IC/Microcontroller/ARM"
    assert len(fetched.parameters) == 2


def test_saving_twice_updates_rather_than_duplicating(conn: sqlite3.Connection) -> None:
    save_part(conn, make_part())
    save_part(conn, make_part(description="revised"))
    assert count_parts(conn) == {"approved": 1}
    fetched = get_part(conn, "01JB4K7QW8ZR3XN5M2VYT9DCFA")
    assert fetched is not None and fetched.description == "revised"


def test_created_at_is_preserved_across_updates(conn: sqlite3.Connection) -> None:
    first = save_part(conn, make_part())
    second = save_part(conn, make_part(description="changed", created_at=None))
    assert second.created_at == first.created_at


def test_parameters_are_replaced_wholesale(conn: sqlite3.Connection) -> None:
    save_part(conn, make_part())
    save_part(conn, make_part(parameters=[Parameter(name="iq", source=SourceKind.USER)]))
    fetched = get_part(conn, "01JB4K7QW8ZR3XN5M2VYT9DCFA")
    assert fetched is not None
    assert [p.name for p in fetched.parameters] == ["iq"]


def test_manufacturer_spellings_collapse_to_one_company(conn: sqlite3.Connection) -> None:
    save_part(conn, make_part(klm_id="01A", mpn="A", manufacturer="Texas Instruments"))
    save_part(conn, make_part(klm_id="01B", mpn="B", manufacturer="texas instruments."))
    assert conn.execute("SELECT COUNT(*) FROM manufacturer").fetchone()[0] == 1


def test_category_path_creates_each_level(conn: sqlite3.Connection) -> None:
    save_part(conn, make_part(category="IC/Power/Regulator/LDO"))
    paths = [r["path"] for r in conn.execute("SELECT path FROM category ORDER BY path")]
    assert paths == ["IC", "IC/Power", "IC/Power/Regulator", "IC/Power/Regulator/LDO"]


def test_get_part_follows_a_merge_alias(conn: sqlite3.Connection) -> None:
    """A retired id keeps resolving, so old schematics do not break."""
    save_part(conn, make_part())
    conn.execute(
        "INSERT INTO part_alias (alias_klm_id, klm_id, merged_at) VALUES (?, ?, ?)",
        ("01OLDID", "01JB4K7QW8ZR3XN5M2VYT9DCFA", "2026-08-09T00:00:00Z"),
    )
    fetched = get_part(conn, "01OLDID")
    assert fetched is not None and fetched.klm_id == "01JB4K7QW8ZR3XN5M2VYT9DCFA"


def test_list_parts_filters_and_orders(conn: sqlite3.Connection) -> None:
    save_part(conn, make_part(klm_id="01B", mpn="B", status=PartStatus.DRAFT))
    save_part(conn, make_part(klm_id="01A", mpn="A", status=PartStatus.APPROVED))
    assert [p.klm_id for p in list_parts(conn)] == ["01A", "01B"]
    assert [p.klm_id for p in list_parts(conn, status=PartStatus.DRAFT)] == ["01B"]


def test_list_parts_by_category_includes_descendants(conn: sqlite3.Connection) -> None:
    save_part(conn, make_part(klm_id="01A", mpn="A", category="IC/Power/LDO"))
    save_part(conn, make_part(klm_id="01B", mpn="B", category="Passive/Resistor"))
    assert [p.klm_id for p in list_parts(conn, category="IC")] == ["01A"]


def test_missing_part_is_none_and_delete_reports_whether_it_acted(
    conn: sqlite3.Connection,
) -> None:
    assert get_part(conn, "nope") is None
    assert not delete_part(conn, "nope")
    save_part(conn, make_part())
    assert delete_part(conn, "01JB4K7QW8ZR3XN5M2VYT9DCFA")


def test_normalize_manufacturer_ignores_case_and_punctuation() -> None:
    assert normalize_manufacturer("Texas Instruments") == normalize_manufacturer(
        "texas-instruments"
    )


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------


def test_export_writes_one_directory_per_part(conn: sqlite3.Connection, tmp_path: Path) -> None:
    save_part(conn, make_part())
    result = export_catalog(conn, tmp_path / "catalog")
    assert result.written == ["01JB4K7QW8ZR3XN5M2VYT9DCFA"]
    assert (tmp_path / "catalog" / "01JB4K7QW8ZR3XN5M2VYT9DCFA" / "part.yaml").exists()


def test_re_export_with_no_changes_writes_nothing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """This is what makes `git status` clean mean something."""
    save_part(conn, make_part())
    catalog = tmp_path / "catalog"
    export_catalog(conn, catalog)
    target = catalog / "01JB4K7QW8ZR3XN5M2VYT9DCFA" / "part.yaml"
    before = target.stat().st_mtime_ns

    second = export_catalog(conn, catalog)
    assert second.written == []
    assert second.unchanged == ["01JB4K7QW8ZR3XN5M2VYT9DCFA"]
    assert target.stat().st_mtime_ns == before


def test_export_import_export_is_byte_identical(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """ADR-0001's load-bearing property: the database is reconstructible."""
    for i in range(5):
        save_part(conn, make_part(klm_id=f"01PART{i:020d}", mpn=f"MPN-{i}"))

    catalog = tmp_path / "catalog"
    export_catalog(conn, catalog)
    first = {p.name: (p / "part.yaml").read_text() for p in sorted(catalog.iterdir())}

    fresh = connect(tmp_path / "rebuilt.db")
    migrate(fresh)
    try:
        result = import_catalog(fresh, catalog)
        assert result.ok
        assert len(result.created) == 5

        rebuilt = tmp_path / "catalog2"
        export_catalog(fresh, rebuilt)
        second = {p.name: (p / "part.yaml").read_text() for p in sorted(rebuilt.iterdir())}
    finally:
        fresh.close()

    assert first == second


def test_import_is_idempotent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    save_part(conn, make_part())
    catalog = tmp_path / "catalog"
    export_catalog(conn, catalog)

    assert len(import_catalog(conn, catalog).unchanged) == 1
    assert import_catalog(conn, catalog).updated == []


def test_import_detects_a_genuine_edit(conn: sqlite3.Connection, tmp_path: Path) -> None:
    save_part(conn, make_part())
    catalog = tmp_path / "catalog"
    export_catalog(conn, catalog)

    target = catalog / "01JB4K7QW8ZR3XN5M2VYT9DCFA" / "part.yaml"
    target.write_text(target.read_text().replace("LQFP-48", "LQFP-64"))

    assert import_catalog(conn, catalog).updated == ["01JB4K7QW8ZR3XN5M2VYT9DCFA"]
    fetched = get_part(conn, "01JB4K7QW8ZR3XN5M2VYT9DCFA")
    assert fetched is not None and fetched.package == "LQFP-64"


def test_prune_is_opt_in(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Deleting a user's files must be something they asked for."""
    save_part(conn, make_part())
    catalog = tmp_path / "catalog"
    export_catalog(conn, catalog)
    orphan = catalog / "01ORPHANED"
    orphan.mkdir()
    (orphan / "part.yaml").write_text("klm_id: 01ORPHANED\n")

    assert export_catalog(conn, catalog).removed == []
    assert orphan.exists()

    assert export_catalog(conn, catalog, prune=True).removed == ["01ORPHANED"]
    assert not orphan.exists()


def test_one_malformed_file_does_not_block_the_rest(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    save_part(conn, make_part())
    catalog = tmp_path / "catalog"
    export_catalog(conn, catalog)
    broken = catalog / "01BROKEN"
    broken.mkdir()
    (broken / "part.yaml").write_text("mpn: X\n")  # no klm_id

    fresh = connect(tmp_path / "fresh.db")
    migrate(fresh)
    try:
        result = import_catalog(fresh, catalog)
        assert not result.ok
        assert len(result.created) == 1
        assert len(result.errors) == 1
    finally:
        fresh.close()


def test_strict_import_raises_on_the_first_problem(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    catalog = tmp_path / "catalog"
    broken = catalog / "01BROKEN"
    broken.mkdir(parents=True)
    (broken / "part.yaml").write_text("mpn: X\n")
    with pytest.raises(YamlError):
        import_catalog(conn, catalog, strict=True)


def test_a_file_in_the_wrong_directory_is_an_error(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    catalog = tmp_path / "catalog"
    wrong = catalog / "01WRONGDIR"
    wrong.mkdir(parents=True)
    (wrong / "part.yaml").write_text(to_yaml(make_part()))

    result = import_catalog(conn, catalog)
    assert not result.ok
    assert "does not match its directory" in result.errors[0][1]


def test_importing_an_absent_directory_is_not_an_error(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    assert import_catalog(conn, tmp_path / "nothing-here").ok
