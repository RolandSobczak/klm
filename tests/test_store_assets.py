"""Tests for content-addressed asset storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from klm.store.assets import (
    AssetError,
    AssetKind,
    AssetStore,
    canonicalize,
    hash_bytes,
)

FIXTURES = Path(__file__).parent / "fixtures"

FOOTPRINT = b'(footprint "R"\n\t(layer "F.Cu")\n\t(pad "1" smd)\n)\n'
# Same content, different formatting. Must land on the same address.
FOOTPRINT_REFORMATTED = b'(footprint "R" (layer "F.Cu") (pad "1" smd))'

STEP_TEMPLATE = b"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('part.step','%s',(''),(''),'','','');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));
ENDSEC;
DATA;
#1=CARTESIAN_POINT('',(0.,0.,0.));
ENDSEC;
END-ISO-10303-21;
"""


@pytest.fixture
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / "assets")


# ---------------------------------------------------------------------------
# Hashing and canonicalisation
# ---------------------------------------------------------------------------


def test_hash_has_the_documented_shape() -> None:
    digest = hash_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_formatting_differences_do_not_change_the_address() -> None:
    """The point of canonicalising before hashing: re-export must not look new."""
    assert hash_bytes(FOOTPRINT, AssetKind.FOOTPRINT) == hash_bytes(
        FOOTPRINT_REFORMATTED, AssetKind.FOOTPRINT
    )


def test_float_spelling_does_not_change_the_address() -> None:
    a = b'(pad "1" (at 1.270000 0.000000))'
    b = b'(pad "1" (at 1.27 0))'
    assert hash_bytes(a, AssetKind.FOOTPRINT) == hash_bytes(b, AssetKind.FOOTPRINT)


def test_content_differences_do_change_the_address() -> None:
    a = b'(footprint "R" (pad "1" smd))'
    b = b'(footprint "R" (pad "2" smd))'
    assert hash_bytes(a, AssetKind.FOOTPRINT) != hash_bytes(b, AssetKind.FOOTPRINT)


def test_step_timestamps_are_ignored() -> None:
    """Two exports of identical geometry differ only in the header timestamp."""
    older = STEP_TEMPLATE % b"2026-01-15T09:00:00"
    newer = STEP_TEMPLATE % b"2026-08-09T17:42:11"
    assert hash_bytes(older, AssetKind.MODEL3D) == hash_bytes(newer, AssetKind.MODEL3D)


def test_step_geometry_differences_are_not_ignored() -> None:
    base = STEP_TEMPLATE % b"2026-01-15T09:00:00"
    moved = base.replace(b"(0.,0.,0.)", b"(1.,0.,0.)")
    assert hash_bytes(base, AssetKind.MODEL3D) != hash_bytes(moved, AssetKind.MODEL3D)


def test_step_line_endings_are_normalised() -> None:
    unix = STEP_TEMPLATE % b"2026-01-15T09:00:00"
    dos = unix.replace(b"\n", b"\r\n")
    assert hash_bytes(unix, AssetKind.MODEL3D) == hash_bytes(dos, AssetKind.MODEL3D)


def test_unparseable_input_is_hashed_verbatim_rather_than_rejected() -> None:
    """The store holds bytes; validating them is the QA gate's job."""
    broken = b"(footprint unterminated"
    assert canonicalize(broken, AssetKind.FOOTPRINT) == broken
    assert hash_bytes(broken, AssetKind.FOOTPRINT).startswith("sha256:")


def test_non_utf8_input_is_hashed_verbatim() -> None:
    data = b"\xff\xfe not text at all"
    assert canonicalize(data, AssetKind.SYMBOL) == data


# ---------------------------------------------------------------------------
# Store operations
# ---------------------------------------------------------------------------


def test_add_then_read_returns_the_original_bytes(store: AssetStore) -> None:
    digest = store.add_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    assert store.read(digest, AssetKind.FOOTPRINT) == FOOTPRINT


def test_storing_identical_content_twice_deduplicates(store: AssetStore) -> None:
    first = store.add_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    second = store.add_bytes(FOOTPRINT_REFORMATTED, AssetKind.FOOTPRINT)
    assert first == second
    assert len(store.list_hashes(AssetKind.FOOTPRINT)) == 1


def test_stored_bytes_are_the_first_version_written(store: AssetStore) -> None:
    """Deduplication keeps the original bytes; it does not rewrite them."""
    store.add_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    digest = store.add_bytes(FOOTPRINT_REFORMATTED, AssetKind.FOOTPRINT)
    assert store.read(digest, AssetKind.FOOTPRINT) == FOOTPRINT


def test_kinds_are_stored_separately(store: AssetStore) -> None:
    data = b"(x)"
    sym = store.add_bytes(data, AssetKind.SYMBOL)
    fp = store.add_bytes(data, AssetKind.FOOTPRINT)
    assert store.path_for(sym, AssetKind.SYMBOL) != store.path_for(fp, AssetKind.FOOTPRINT)
    assert store.list_hashes(AssetKind.SYMBOL) == [sym]
    assert store.list_hashes(AssetKind.FOOTPRINT) == [fp]


def test_filenames_use_the_kind_suffix(store: AssetStore) -> None:
    digest = store.add_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    assert store.path_for(digest, AssetKind.FOOTPRINT).suffix == ".kicad_mod"


def test_add_file_reads_from_disk(store: AssetStore, tmp_path: Path) -> None:
    source = tmp_path / "in.kicad_mod"
    source.write_bytes(FOOTPRINT)
    digest = store.add_file(source, AssetKind.FOOTPRINT)
    assert digest == hash_bytes(FOOTPRINT, AssetKind.FOOTPRINT)


def test_copy_to_writes_the_asset_out(store: AssetStore, tmp_path: Path) -> None:
    digest = store.add_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    dest = tmp_path / "out" / "R.kicad_mod"
    store.copy_to(digest, AssetKind.FOOTPRINT, dest)
    assert dest.read_bytes() == FOOTPRINT


def test_exists_reflects_storage(store: AssetStore) -> None:
    digest = hash_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    assert not store.exists(digest, AssetKind.FOOTPRINT)
    store.add_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    assert store.exists(digest, AssetKind.FOOTPRINT)


def test_list_hashes_is_sorted_and_empty_when_absent(store: AssetStore) -> None:
    assert store.list_hashes(AssetKind.SYMBOL) == []
    for i in range(5):
        store.add_bytes(f"(sym {i})".encode(), AssetKind.SYMBOL)
    hashes = store.list_hashes(AssetKind.SYMBOL)
    assert len(hashes) == 5
    assert hashes == sorted(hashes)


def test_real_fixture_files_store_and_verify(store: AssetStore) -> None:
    fp = store.add_file(FIXTURES / "footprint_sample.kicad_mod", AssetKind.FOOTPRINT)
    sym = store.add_file(FIXTURES / "symbol_sample.kicad_sym", AssetKind.SYMBOL)
    assert store.verify(fp, AssetKind.FOOTPRINT)
    assert store.verify(sym, AssetKind.SYMBOL)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_reading_a_missing_asset_raises(store: AssetStore) -> None:
    digest = hash_bytes(b"(nope)", AssetKind.SYMBOL)
    with pytest.raises(AssetError, match="not found"):
        store.read(digest, AssetKind.SYMBOL)


@pytest.mark.parametrize(
    "bad",
    ["", "deadbeef", "sha256:xyz", "md5:" + "a" * 32, "sha256:" + "A" * 64],
    ids=["empty", "no-prefix", "short", "wrong-algo", "uppercase"],
)
def test_malformed_hashes_are_rejected(store: AssetStore, bad: str) -> None:
    with pytest.raises(AssetError, match="malformed"):
        store.path_for(bad, AssetKind.SYMBOL)


def test_verify_detects_corruption(store: AssetStore) -> None:
    digest = store.add_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    assert store.verify(digest, AssetKind.FOOTPRINT)

    store.path_for(digest, AssetKind.FOOTPRINT).write_bytes(b'(footprint "TAMPERED")')
    assert not store.verify(digest, AssetKind.FOOTPRINT)


def test_verify_is_false_for_a_missing_asset(store: AssetStore) -> None:
    assert not store.verify(hash_bytes(b"(x)", AssetKind.SYMBOL), AssetKind.SYMBOL)


def test_no_temporary_file_survives_a_write(store: AssetStore) -> None:
    store.add_bytes(FOOTPRINT, AssetKind.FOOTPRINT)
    leftovers = list(store.dir_for(AssetKind.FOOTPRINT).glob("*.tmp"))
    assert leftovers == []
