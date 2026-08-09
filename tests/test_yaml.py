"""Tests for the restricted YAML emitter and reader.

Byte-reproducibility is the whole point (docs/adr/0008), so it is tested by
property as well as by example.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from klm.serial.yaml import YamlError, dumps, loads

# ---------------------------------------------------------------------------
# Emitting
# ---------------------------------------------------------------------------


def test_flat_mapping() -> None:
    assert dumps({"mpn": "STM32F103C8T6", "pins": 48}) == "mpn: STM32F103C8T6\npins: 48\n"


def test_key_order_is_the_callers_not_sorted() -> None:
    """Field order is the file format; the caller decides how a part reads."""
    assert dumps({"z": 1, "a": 2}) == "z: 1\na: 2\n"


def test_nested_mapping_is_indented_two_spaces() -> None:
    out = dumps({"assets": {"symbol": "sha256:aa", "footprint": "sha256:bb"}})
    assert out == "assets:\n  symbol: sha256:aa\n  footprint: sha256:bb\n"


def test_sequence_of_scalars() -> None:
    assert dumps({"tags": ["smd", "passive"]}) == "tags:\n  - smd\n  - passive\n"


def test_sequence_of_mappings_puts_the_first_key_on_the_dash() -> None:
    out = dumps({"parameters": [{"name": "vdd", "unit": "V"}, {"name": "iq"}]})
    assert out == "parameters:\n  - name: vdd\n    unit: V\n  - name: iq\n"


def test_empty_containers_are_written_inline() -> None:
    assert dumps({"a": {}, "b": []}) == "a: {}\nb: []\n"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "null"),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (-17, "-17"),
        (1.5, "1.5"),
        (2.0, "2.0"),
        (1e-7, "1e-07"),
    ],
)
def test_scalar_spellings(value: Any, expected: str) -> None:
    assert dumps({"k": value}) == f"k: {expected}\n"


@pytest.mark.parametrize(
    "value",
    ["", " leading", "trailing ", "true", "False", "null", "~", "42", "3.14", "yes", "no"],
    ids=lambda v: repr(v),
)
def test_strings_that_would_be_misread_are_quoted(value: str) -> None:
    """A string must never come back as a bool, a number or None."""
    assert loads(dumps({"k": value}))["k"] == value
    assert dumps({"k": value}) != f"k: {value}\n"


def test_strings_needing_no_quotes_get_none() -> None:
    assert dumps({"k": "SOT-23-6"}) == "k: SOT-23-6\n"
    assert dumps({"k": "IC/Power/Regulator"}) == "k: IC/Power/Regulator\n"


@pytest.mark.parametrize(
    "value",
    [
        "01K27AF180000G40R40M30E209",
        "2026-08-09T09:36:40Z",
        "sha256:3f9a71c4",
        "http://example.com/ds.pdf",
    ],
    ids=["klm_id", "timestamp", "content-hash", "url"],
)
def test_the_values_a_part_file_is_mostly_made_of_are_not_quoted(value: str) -> None:
    """These are the fields a person reads; needless quoting is pure noise."""
    assert dumps({"k": value}) == f"k: {value}\n"
    assert loads(dumps({"k": value}))["k"] == value


def test_a_leading_digit_does_not_defeat_number_detection() -> None:
    """Relaxing the first character must not let a real number become a string."""
    assert dumps({"k": "42"}) == 'k: "42"\n'
    assert dumps({"k": "3.14"}) == 'k: "3.14"\n'


def test_special_characters_are_escaped() -> None:
    assert loads(dumps({"k": 'a "quoted" and \\ back'}))["k"] == 'a "quoted" and \\ back'
    assert loads(dumps({"k": "line1\nline2\ttabbed"}))["k"] == "line1\nline2\ttabbed"


def test_unicode_passes_through() -> None:
    value = "Ω 100µF ±1% 日本語"
    assert loads(dumps({"k": value}))["k"] == value


def test_output_always_ends_with_a_newline() -> None:
    assert dumps({"a": 1}).endswith("\n")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_comments_and_blank_lines_are_ignored() -> None:
    text = "# a comment\n\nmpn: X\n\n  # indented comment\npins: 3\n"
    assert loads(text) == {"mpn": "X", "pins": 3}


def test_empty_document_is_an_empty_mapping() -> None:
    assert loads("") == {}
    assert loads("\n\n# only comments\n") == {}


def test_bare_key_with_no_block_reads_as_null() -> None:
    assert loads("a:\nb: 1\n") == {"a": None, "b": 1}


def test_deeply_nested_structures() -> None:
    data = {"a": {"b": {"c": {"d": "deep"}}}}
    assert loads(dumps(data)) == data


def test_sequence_directly_under_a_key_at_the_same_indent() -> None:
    """A hand-written file may not indent the dashes; accept it."""
    assert loads("tags:\n- a\n- b\n") == {"tags": ["a", "b"]}


def test_colon_inside_a_quoted_value_is_not_a_key_separator() -> None:
    assert loads('url: "https://example.com/a:b"') == {"url": "https://example.com/a:b"}


def test_unquoted_url_keeps_its_scheme() -> None:
    """`http://x` must not be split at the first colon."""
    assert loads("url: http://example.com/x") == {"url": "http://example.com/x"}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("a: 1\n\tb: 2\n", "tabs"),
        ("a: 1\n  b: 2\n", "indentation"),
        ("a: 1\na: 2\n", "duplicate key"),
        ("just a bare string\n", "expected 'key: value'"),
        ('a: "unterminated\n', "unterminated quoted string"),
    ],
)
def test_malformed_input_is_rejected_with_a_line_number(text: str, fragment: str) -> None:
    with pytest.raises(YamlError) as exc:
        loads(text)
    assert fragment in str(exc.value)


def test_non_mapping_top_level_is_rejected() -> None:
    with pytest.raises(YamlError, match="top level"):
        dumps([1, 2])  # type: ignore[arg-type]
    with pytest.raises(YamlError, match="top level"):
        loads("- a\n- b\n")


def test_unsupported_constructs_raise_rather_than_being_guessed() -> None:
    with pytest.raises(YamlError, match="nested sequences"):
        dumps({"a": [[1, 2]]})
    with pytest.raises(YamlError, match="unsupported scalar type"):
        dumps({"a": {1, 2}})
    with pytest.raises(YamlError, match="mapping keys must be strings"):
        dumps({1: "a"})
    with pytest.raises(YamlError, match="non-finite"):
        dumps({"a": float("inf")})


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(
        alphabet=st.characters(
            min_codepoint=32, max_codepoint=0x2FFF, blacklist_categories=("Cs",)
        ),
        max_size=30,
    ),
)

_keys = st.from_regex(r"\A[A-Za-z_][A-Za-z0-9_]{0,15}\Z")


def _values(depth: int = 0) -> st.SearchStrategy[Any]:
    if depth >= 2:
        return _scalars
    return st.one_of(
        _scalars,
        st.lists(_scalars, max_size=4),
        st.dictionaries(_keys, _values(depth + 1), max_size=4),
        st.lists(st.dictionaries(_keys, _scalars, min_size=1, max_size=3), max_size=3),
    )


_documents = st.dictionaries(_keys, _values(), min_size=1, max_size=6)


@given(_documents)
@settings(max_examples=400, deadline=None)
def test_round_trip_preserves_the_data(data: dict[str, Any]) -> None:
    assert loads(dumps(data)) == data


@given(_documents)
@settings(max_examples=400, deadline=None)
def test_emitting_is_a_fixed_point(data: dict[str, Any]) -> None:
    """Re-exporting parsed content must reproduce identical bytes."""
    once = dumps(data)
    assert dumps(loads(once)) == once
