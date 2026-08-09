"""Tests for mechanical pin compatibility.

The verdict has to be trustworthy in one direction above all: `compatible`
must never mean "I could not look". A part whose symbol or footprint klm
cannot read comes back `unchecked` — the asset QA gate's rule, in the place
where getting it wrong puts the wrong part on a board.

The subtle case these protect is the dangerous one: two parts sharing a
footprint and a pin-*type* map, where pin 3 is `EN` on one and `GND` on the
other. Both inputs, same pad, and one of them destroys the board.
"""

from __future__ import annotations

import pytest
from tests.projects import FOOTPRINT_ASSET, SYMBOL_ASSET, seed_resistor

from klm.kicad.sexpr import loads
from klm.kicad.symbols import extract_symbols
from klm.model import Part, PartStatus
from klm.services.catalog import save_part
from klm.services.substitutes import compare, find_substitutes, pin_map
from klm.store.assets import AssetKind

TWO_PIN = """\
(kicad_symbol_lib (version 20231120) (generator klm)
  (symbol "A" (pin passive line (at -3.81 0 0) (length 2.54)
      (name "1" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 3.81 0 180) (length 2.54)
      (name "2" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))
)
"""

REGULATOR = """\
(kicad_symbol_lib (version 20231120) (generator klm)
  (symbol "REG" (pin power_in line (at -7.62 2.54 0) (length 2.54)
      (name "VIN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin power_in line (at 0 -7.62 90) (length 2.54)
      (name "GND" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
    (pin input line (at -7.62 0 0) (length 2.54)
      (name "EN" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27))))))
)
"""

#: Same pads, same pin numbers, same electrical types — and pin 3 does something
#: else entirely.
REGULATOR_OTHER_PIN3 = REGULATOR.replace('(name "EN"', '(name "NC"')


def stored(store, conn, mpn: str, symbol: str, footprint: str = FOOTPRINT_ASSET, **overrides):  # type: ignore[no-untyped-def]
    settings = {
        "klm_id": f"KLM{abs(hash(mpn)) % 10**8:08d}",
        "mpn": mpn,
        "manufacturer": "Acme",
        "package": "SOT-23-5",
        "category": "IC/Power/Regulator/Linear",
        "status": PartStatus.APPROVED,
        "symbol_hash": store.add_bytes(symbol.encode(), AssetKind.SYMBOL),
        "footprint_hash": store.add_bytes(footprint.encode(), AssetKind.FOOTPRINT),
    }
    settings.update(overrides)
    return save_part(conn, Part(**settings))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Reading a pinout
# ---------------------------------------------------------------------------


def test_a_pinout_is_read_by_number() -> None:
    (symbol,) = extract_symbols(loads(REGULATOR))
    assert pin_map(symbol) == {
        "1": ("vin", "power_in"),
        "2": ("gnd", "power_in"),
        "3": ("en", "input"),
    }


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def test_identical_parts_are_compatible(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    left = stored(store, conn, "REG-A", REGULATOR)
    right = stored(store, conn, "REG-B", REGULATOR)

    result = compare(store, left, right)

    assert result.compatible
    assert result.footprint == "same" and result.pins == "same"
    assert result.differences == []


def test_a_renamed_pin_is_a_difference_not_a_pass(env) -> None:  # type: ignore[no-untyped-def]
    """Same pad, same electrical type, different job — the dangerous case."""
    _, conn, store = env
    left = stored(store, conn, "REG-A", REGULATOR)
    right = stored(store, conn, "REG-C", REGULATOR_OTHER_PIN3)

    result = compare(store, left, right)

    assert result.status == "differs"
    assert "pin 3" in result.differences[0]
    assert "'en'" in result.differences[0] and "'nc'" in result.differences[0]


def test_a_different_pin_count_is_reported(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    left = stored(store, conn, "REG-A", REGULATOR)
    right = stored(store, conn, "TWO", TWO_PIN)

    result = compare(store, left, right)

    assert result.status == "differs"
    assert any("no counterpart" in line or "extra pin" in line for line in result.differences)


def test_a_part_with_no_symbol_is_unchecked_not_incompatible(env) -> None:  # type: ignore[no-untyped-def]
    """A check that could not run is `unchecked`, never a verdict."""
    _, conn, store = env
    left = stored(store, conn, "REG-A", REGULATOR)
    right = stored(store, conn, "REG-D", REGULATOR, symbol_hash=None)

    result = compare(store, left, right)

    assert result.status == "unchecked"
    assert result.pins == "unchecked"
    assert not result.compatible
    assert any("no readable symbol" in note for note in result.notes)


def test_a_part_with_no_footprint_is_unchecked(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    left = stored(store, conn, "REG-A", REGULATOR)
    right = stored(store, conn, "REG-E", REGULATOR, footprint_hash=None)

    result = compare(store, left, right)

    assert result.status == "unchecked"
    assert result.footprint == "unchecked"


def test_the_same_land_pattern_under_two_hashes_is_the_same(env) -> None:  # type: ignore[no-untyped-def]
    """Two footprints for one land pattern is the ordinary case.

    Formatting alone would not do it — asset hashes are canonicalised — so
    this differs where footprints really differ: a different 3D model beside
    identical pads.
    """
    _, conn, store = env
    other_model = FOOTPRINT_ASSET.replace("R_0402_1005Metric.step", "R_0402_alt.step")
    left = stored(store, conn, "REG-A", REGULATOR)
    right = stored(store, conn, "REG-F", REGULATOR, footprint=other_model)

    assert left.footprint_hash != right.footprint_hash, "different assets"
    assert compare(store, left, right).footprint == "same", "the same pads, though"


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


def test_a_compatible_part_is_found(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    part = stored(store, conn, "REG-A", REGULATOR)
    stored(store, conn, "REG-B", REGULATOR)

    (candidate,) = find_substitutes(conn, store, part)

    assert candidate.part.mpn == "REG-B"
    assert candidate.compatibility.compatible


def test_a_differing_part_is_hidden_until_asked_for(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    part = stored(store, conn, "REG-A", REGULATOR)
    stored(store, conn, "REG-C", REGULATOR_OTHER_PIN3)

    assert find_substitutes(conn, store, part) == []
    (candidate,) = find_substitutes(conn, store, part, include_differing=True)
    assert candidate.compatibility.status == "differs"


def test_a_part_is_never_its_own_substitute(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    part = stored(store, conn, "REG-A", REGULATOR)
    assert find_substitutes(conn, store, part) == []


def test_a_deprecated_part_is_not_offered(env) -> None:  # type: ignore[no-untyped-def]
    """It is what you are substituting away from."""
    _, conn, store = env
    part = stored(store, conn, "REG-A", REGULATOR)
    stored(store, conn, "REG-B", REGULATOR, status=PartStatus.DEPRECATED)

    assert find_substitutes(conn, store, part) == []


def test_a_part_in_another_package_is_not_a_candidate(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    part = stored(store, conn, "REG-A", REGULATOR)
    stored(store, conn, "REG-G", REGULATOR, package="SOIC-8")

    assert find_substitutes(conn, store, part, include_differing=True) == []


def test_what_is_on_the_shelf_sorts_first(env) -> None:  # type: ignore[no-untyped-def]
    from klm.services.stock import adjust

    _, conn, store = env
    part = stored(store, conn, "REG-A", REGULATOR)
    stored(store, conn, "REG-B", REGULATOR)
    stocked = stored(store, conn, "REG-H", REGULATOR)
    adjust(conn, stocked.klm_id, "A/1", set_to=40)

    found = find_substitutes(conn, store, part)

    assert found[0].part.mpn == "REG-H"
    assert found[0].stock == 40


def test_an_unrelated_catalog_part_is_ignored(env) -> None:  # type: ignore[no-untyped-def]
    _, conn, store = env
    seed_resistor(store, conn)
    part = stored(store, conn, "REG-A", REGULATOR)

    assert find_substitutes(conn, store, part, include_differing=True) == []


@pytest.mark.parametrize("symbol", [SYMBOL_ASSET, TWO_PIN])
def test_every_fixture_symbol_reads(symbol: str) -> None:
    (parsed,) = extract_symbols(loads(symbol))
    assert pin_map(parsed)
