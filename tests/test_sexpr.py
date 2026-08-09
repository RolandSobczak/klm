"""Tests for the lossless S-expression layer.

The round-trip property is the foundation everything else in klm rests on, so
it is tested from several angles: real KiCad fixtures, hand-picked edge cases,
and generated input.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from klm.kicad.sexpr import (
    Atom,
    Document,
    SExp,
    SExprError,
    dumps,
    dumps_canonical,
    load,
    loads,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_FILES = sorted(FIXTURES.glob("*.kicad_*"))


# ---------------------------------------------------------------------------
# The core guarantee
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_real_kicad_files_round_trip_byte_for_byte(path: Path) -> None:
    original = path.read_text(encoding="utf-8")
    assert dumps(loads(original)) == original


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_load_from_disk_round_trips(path: Path) -> None:
    assert dumps(load(path)) == path.read_text(encoding="utf-8")


ROUND_TRIP_CASES = [
    pytest.param("(a)", id="minimal"),
    pytest.param("()", id="empty-list"),
    pytest.param("(a b c)", id="flat"),
    pytest.param("(a (b (c (d))))", id="deeply-nested"),
    pytest.param("(a\n  (b 1)\n  (c 2)\n)\n", id="indented"),
    pytest.param("  \n  (a)  \n\n", id="leading-and-trailing-whitespace"),
    pytest.param("(a)", id="no-trailing-newline"),
    pytest.param("(a)\r\n(b)\r\n", id="crlf"),
    pytest.param('(a "")', id="empty-string"),
    pytest.param('(a "hello world")', id="string-with-space"),
    pytest.param('(a "has \\"quotes\\" inside")', id="escaped-quotes"),
    pytest.param('(a "back\\\\slash")', id="escaped-backslash"),
    pytest.param('(a "line\\nbreak")', id="escaped-newline"),
    pytest.param('(a "C:\\Users\\rs\\lib")', id="unescaped-windows-path"),
    pytest.param('(a "tab\there")', id="literal-tab-in-string"),
    pytest.param('(a "multi\nline\nstring")', id="literal-newline-in-string"),
    pytest.param('(property "Value" "Ω 100µF ±1%")', id="unicode"),
    pytest.param('(descr "日本語のテキスト")', id="cjk"),
    pytest.param("(at 1.27 -3.810000 0)", id="float-precision-preserved"),
    pytest.param("(n 0.000000 -0.0 +1.5 .5 1e-3)", id="numeric-forms"),
    pytest.param("(a ; trailing comment\n  b)", id="comment"),
    pytest.param("(a\t\tb)", id="tabs-between-atoms"),
    pytest.param("(  a  b  )", id="padded-parens"),
    pytest.param('(model "${KICAD8_3DMODEL_DIR}/x.wrl")', id="env-var-in-string"),
]


@pytest.mark.parametrize("text", ROUND_TRIP_CASES)
def test_round_trip_edge_cases(text: str) -> None:
    assert dumps(loads(text)) == text


def test_float_text_is_never_reformatted_on_round_trip() -> None:
    """The motivating case: 1.270000 must not become 1.27, or vice versa."""
    text = "(at 1.270000 1.27 0.1 0.10000000000000001)"
    assert dumps(loads(text)) == text


# ---------------------------------------------------------------------------
# Surgical mutation
# ---------------------------------------------------------------------------


def test_editing_one_node_leaves_every_other_byte_untouched() -> None:
    original = (FIXTURES / "footprint_sample.kicad_mod").read_text(encoding="utf-8")
    doc = loads(original)

    model = doc.root.find("model")
    assert model is not None
    path_atom = model.children[1]
    assert isinstance(path_atom, Atom)
    path_atom.value = "${KLM_3DMODELS}/R_0402_1005Metric.step"

    result = dumps(doc)

    # Exactly one line differs, and it is the one we asked for.
    diff = [
        (a, b)
        for a, b in zip(original.splitlines(), result.splitlines(), strict=True)
        if a != b
    ]
    assert len(diff) == 1
    assert "KICAD8_3DMODEL_DIR" in diff[0][0]
    assert "KLM_3DMODELS" in diff[0][1]


def test_assigning_value_discards_cached_source_text() -> None:
    atom = loads("(a 1.270000)").root.children[1]
    assert isinstance(atom, Atom)
    assert atom.raw == "1.270000"
    atom.value = "2.54"
    assert atom.raw is None
    assert dumps(loads("(a 1.270000)")) == "(a 1.270000)"


def test_edited_atom_is_requoted_when_the_new_value_requires_it() -> None:
    doc = loads("(a bare)")
    atom = doc.root.children[1]
    assert isinstance(atom, Atom)
    atom.value = "now has spaces"
    assert dumps(doc) == '(a "now has spaces")'


def test_adjacent_constructed_atoms_do_not_fuse() -> None:
    """Regression: SExp([Atom('0'), Atom('0')]) once serialised as `(00)`.

    Constructed nodes carry no leading whitespace, so the writer has to insert a
    separator itself when two bare tokens would otherwise merge.
    """
    tree = SExp([Atom("0"), Atom("0")])
    assert dumps(tree) == "(0 0)"
    assert loads(dumps(tree)).root == tree


def test_no_separator_is_inserted_where_the_syntax_already_delimits() -> None:
    """Parens and quotes delimit themselves; adding a space would break round-trip."""
    assert dumps(SExp([Atom("a"), SExp([Atom("b")])])) == "(a(b))"
    assert dumps(SExp([Atom("a"), Atom("with space")])) == '(a"with space")'
    assert dumps(loads("(a(b))")) == "(a(b))"


def test_unknown_nodes_survive_untouched() -> None:
    """A node type from a future KiCad release must pass straight through."""
    text = '(footprint "X" (some_future_node 1 2 (nested "deep")) (pad "1"))'
    doc = loads(text)
    assert doc.root.find("some_future_node") is not None
    assert dumps(doc) == text


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def test_name_reads_the_leading_symbol() -> None:
    assert loads("(footprint 1 2)").root.name == "footprint"


def test_name_is_none_when_the_list_does_not_start_with_an_atom() -> None:
    assert loads("((a) b)").root.name is None
    assert loads("()").root.name is None


def test_find_all_is_recursive_by_default() -> None:
    doc = loads('(sym (pad "1") (group (pad "2") (pad "3")))')
    assert len(list(doc.root.find_all("pad"))) == 3
    assert len(list(doc.root.find_all("pad", recursive=False))) == 1


def test_find_returns_the_first_match_or_none() -> None:
    doc = loads('(sym (pad "1") (pad "2"))')
    found = doc.root.find("pad")
    assert found is not None
    assert found.values() == ["1"]
    assert doc.root.find("nonexistent") is None


def test_values_skips_the_leading_name_and_ignores_sublists() -> None:
    doc = loads('(property "Reference" "U" (at 0 0 0))')
    assert doc.root.values() == ["Reference", "U"]


def test_sequence_protocol() -> None:
    root = loads("(a b c)").root
    assert len(root) == 3
    assert isinstance(root[0], Atom)
    assert [n.value for n in root if isinstance(n, Atom)] == ["a", "b", "c"]


def test_symbol_fixture_navigation() -> None:
    doc = load(FIXTURES / "symbol_sample.kicad_sym")
    props = {
        p.values()[0]: p.values()[1]
        for p in doc.root.find_all("property")
        if len(p.values()) >= 2
    }
    assert props["MPN"] == "AMS1117-3.3"
    assert props["KLM_ID"] == "01JB4K7QW8ZR3XN5M2VYT9DCFA"
    assert len(list(doc.root.find_all("pin"))) == 2


# ---------------------------------------------------------------------------
# Atom semantics
# ---------------------------------------------------------------------------


def test_constructed_atoms_quote_only_when_necessary() -> None:
    assert Atom("bare").token() == "bare"
    assert Atom("1.27").token() == "1.27"
    assert Atom("has space").token() == '"has space"'
    assert Atom("").token() == '""'
    assert Atom("with(paren").token() == '"with(paren"'
    assert Atom('quo"te').token() == '"quo\\"te"'
    assert Atom("back\\slash").token() == '"back\\\\slash"'


def test_quoting_can_be_forced() -> None:
    assert Atom("bare", quoted=True).token() == '"bare"'


def test_atom_equality_ignores_formatting() -> None:
    """`"foo"` and `foo` differ in spelling but not in meaning."""
    parsed = loads('(a "foo")').root.children[1]
    assert parsed == Atom("foo", quoted=True)
    assert Atom("foo", quoted=True) != Atom("foo", quoted=False)


def test_sexp_equality_ignores_whitespace() -> None:
    assert loads("(a b c)").root == loads("(a\n  b\n  c)").root


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("(a", "unterminated '('"),
        ("(a))", "unexpected ')'"),
        (')', "unexpected ')'"),
        ('(a "unterminated', "unterminated string"),
        ('(a "trailing escape\\', "unterminated escape"),
    ],
)
def test_malformed_input_raises_with_a_location(text: str, fragment: str) -> None:
    with pytest.raises(SExprError) as exc:
        loads(text)
    assert fragment in str(exc.value)
    assert exc.value.line >= 1
    assert exc.value.column >= 1


def test_error_reports_the_correct_line_and_column() -> None:
    with pytest.raises(SExprError) as exc:
        loads("(a\n  b\n  (c\n")
    assert exc.value.line == 3
    assert exc.value.column == 3


def test_root_rejects_documents_without_exactly_one_expression() -> None:
    with pytest.raises(SExprError):
        _ = loads("(a) (b)").root
    with pytest.raises(SExprError):
        _ = loads("").root


def test_empty_input_parses_to_an_empty_document() -> None:
    doc = loads("")
    assert doc.nodes == []
    assert dumps(doc) == ""


# ---------------------------------------------------------------------------
# Canonical output
# ---------------------------------------------------------------------------


def test_canonical_output_is_stable_across_regeneration() -> None:
    """Vendor twice, get identical bytes. This is what makes git diffs useful."""
    doc = load(FIXTURES / "footprint_sample.kicad_mod")
    once = dumps_canonical(doc)
    twice = dumps_canonical(loads(once))
    assert once == twice


def test_canonical_output_converges_regardless_of_input_formatting() -> None:
    a = dumps_canonical(loads("(a (b 1.270000) (c 2))"))
    b = dumps_canonical(loads("(a\n\t(b 1.27)\n\t(c   2)\n)"))
    assert a == b


def test_canonical_normalises_decimals_but_leaves_integers_alone() -> None:
    out = dumps_canonical(loads("(at 1.270000 -0.000000 0 20240108 3)"))
    assert out.strip() == "(at 1.27 0 0 20240108 3)"


def test_canonical_keeps_all_atom_lists_on_one_line() -> None:
    assert dumps_canonical(loads("(size 0.54 0.64)")).strip() == "(size 0.54 0.64)"


def test_canonical_indents_nested_lists() -> None:
    out = dumps_canonical(loads('(footprint "R" (layer "F.Cu") (pad "1" smd))'))
    assert out == '(footprint "R"\n  (layer "F.Cu")\n  (pad "1" smd))\n'


def test_canonical_preserves_string_content_exactly() -> None:
    text = '(property "Value" "Ω 100µF ±1%")'
    assert dumps_canonical(loads(text)).strip() == text


def test_canonical_handles_an_empty_list() -> None:
    assert dumps_canonical(loads("(a ())")).strip() == "(a\n  ())"


# ---------------------------------------------------------------------------
# Generated input
# ---------------------------------------------------------------------------

# Printable text minus the characters that would need escaping is enough to
# exercise the interesting paths without generating unreadable failures.
_atom_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FFF, blacklist_categories=("Cs",)),
    min_size=0,
    max_size=12,
)


def _nodes(depth: int = 0):
    atoms = _atom_text.map(lambda s: Atom(s))
    if depth >= 3:
        return atoms
    return st.one_of(
        atoms,
        st.lists(_nodes(depth + 1), max_size=4).map(lambda cs: SExp(list(cs))),
    )


_trees = st.lists(_nodes(1), min_size=1, max_size=5).map(lambda cs: SExp(list(cs)))


@given(_trees)
@settings(max_examples=300, deadline=None)
def test_constructed_trees_survive_a_write_parse_cycle(tree: SExp) -> None:
    """Anything klm builds must parse back to an equal tree."""
    text = dumps(Document([tree]))
    assert loads(text).root == tree


@given(_trees)
@settings(max_examples=300, deadline=None)
def test_canonical_output_reaches_a_fixed_point(tree: SExp) -> None:
    once = dumps_canonical(SExp([tree]))
    twice = dumps_canonical(loads(once))
    assert once == twice


@given(st.lists(_nodes(1), min_size=1, max_size=4), st.sampled_from(["", " ", "\n", "\t\n  "]))
@settings(max_examples=200, deadline=None)
def test_generated_text_round_trips_byte_for_byte(children, gap: str) -> None:
    tree = SExp([c for c in children])
    for i, child in enumerate(tree.children):
        child.pre = gap if i else ""
    tree.pre_close = gap
    text = dumps(Document([tree], trailing=gap))
    assert dumps(loads(text)) == text
