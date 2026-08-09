"""Lossless S-expression reader and writer for KiCad files.

The guarantee this module exists to provide, stated as a property:

    dumps(loads(text)) == text        for every well-formed input

That is not a nice-to-have. klm edits ``.kicad_sch`` and ``.kicad_pcb`` files
that represent hours of a user's work and cannot be regenerated. A parser that
normalises formatting, reorders nodes, or silently drops node types it does not
recognise will corrupt those files the first time KiCad adds a feature.

The design consequence is that nodes preserve their *source text*, not just
their value:

* An :class:`Atom` keeps the exact token text it was parsed from. ``1.27`` is
  written back as ``1.27``, never as ``1.2700000000000000622``.
* Every node keeps the whitespace that preceded it, so indentation and line
  breaks survive untouched.
* Mutating a node clears its cached source text, so only what you actually
  changed is re-serialised.

For files klm generates and owns outright — ``KLM.kicad_sym``, vendored
libraries — use :func:`dumps_canonical` instead, which imposes stable
formatting so that regenerating without changes produces a zero-byte diff.

See docs/adr/0002-lossless-sexpr-round-trip.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

__all__ = [
    "Atom",
    "Node",
    "SExp",
    "SExprError",
    "dump",
    "dumps",
    "dumps_canonical",
    "load",
    "loads",
]


class SExprError(ValueError):
    """Raised when input is not a well-formed S-expression.

    Carries the byte offset and a 1-indexed line/column so the caller can point
    at the problem in a file rather than just reporting that it failed.
    """

    def __init__(self, message: str, text: str, pos: int) -> None:
        line = text.count("\n", 0, pos) + 1
        col = pos - (text.rfind("\n", 0, pos) + 1) + 1
        super().__init__(f"{message} at line {line}, column {col}")
        self.pos = pos
        self.line = line
        self.column = col


# Characters that terminate a bare (unquoted) atom.
_ATOM_END = frozenset('()"; \t\r\n')

# A bare atom needs no quoting when written; anything else does. Backslash is
# excluded even though it parses fine bare, because it is an escape character
# inside quotes and leaving it unquoted invites disagreement with other readers.
_BARE_SAFE = re.compile(r"^[^\s()\"';\\]+$")

# Characters that cannot merge with an adjacent token, so never need a space.
_SELF_DELIMITING = frozenset('()"')

_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    '"': '"',
    "\\": "\\",
}


class Node:
    """Base class for atoms and lists."""

    __slots__ = ("pre",)

    pre: str
    """Whitespace (and comments) appearing immediately before this node."""

    def write(self, out: list[str]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class Atom(Node):
    """A single token: a symbol, a number, or a quoted string.

    ``raw`` holds the exact source text. It is dropped as soon as ``value`` or
    ``quoted`` is assigned, because the source text no longer describes the
    node. Constructed atoms have no ``raw`` and are serialised canonically.
    """

    __slots__ = ("_quoted", "_raw", "_value")

    def __init__(
        self,
        value: str,
        *,
        quoted: bool | None = None,
        raw: str | None = None,
        pre: str = "",
    ) -> None:
        self._value = value
        self._quoted = _needs_quoting(value) if quoted is None else quoted
        self._raw = raw
        self.pre = pre

    @property
    def value(self) -> str:
        return self._value

    @value.setter
    def value(self, new: str) -> None:
        self._value = new
        self._raw = None

    @property
    def quoted(self) -> bool:
        return self._quoted

    @quoted.setter
    def quoted(self, new: bool) -> None:
        self._quoted = new
        self._raw = None

    @property
    def raw(self) -> str | None:
        """Exact source text, or ``None`` if this atom was constructed or edited."""
        return self._raw

    def write(self, out: list[str]) -> None:
        if self.pre:
            out.append(self.pre)
        text = self._raw if self._raw is not None else self.token()
        # A constructed atom carries no leading whitespace, so two of them in a
        # row would otherwise fuse into one token: (a b) written as (ab).
        if not self.pre and _needs_separator(out, text):
            out.append(" ")
        out.append(text)

    def token(self) -> str:
        """Serialise this atom, ignoring any cached source text."""
        if self._quoted or _needs_quoting(self._value):
            return _quote(self._value)
        return self._value

    def __repr__(self) -> str:
        return f"Atom({self._value!r}{', quoted=True' if self._quoted else ''})"

    def __eq__(self, other: object) -> bool:
        """Value equality. Deliberately ignores ``raw`` and ``pre``.

        Two atoms that mean the same thing are equal even if one was written
        ``"foo"`` and the other ``foo``. Formatting is not semantics.
        """
        if isinstance(other, Atom):
            return self._value == other._value and self._quoted == other._quoted
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._value, self._quoted))


class SExp(Node):
    """A parenthesised list of nodes.

    Supports the small amount of navigation klm actually needs. Anything more
    elaborate belongs in a typed view layered on top, not here — this class has
    to stay simple enough to be obviously correct.
    """

    __slots__ = ("children", "pre_close")

    def __init__(
        self,
        children: list[Node] | None = None,
        *,
        pre: str = "",
        pre_close: str = "",
    ) -> None:
        self.children: list[Node] = children if children is not None else []
        self.pre = pre
        self.pre_close = pre_close
        """Whitespace between the last child and the closing parenthesis."""

    # -- navigation -----------------------------------------------------

    @property
    def name(self) -> str | None:
        """The leading symbol, e.g. ``"footprint"`` for ``(footprint ...)``."""
        if self.children and isinstance(self.children[0], Atom):
            return self.children[0].value
        return None

    def find_all(self, name: str, *, recursive: bool = True) -> Iterator[SExp]:
        """Yield descendant lists whose :attr:`name` matches, outermost first."""
        for child in self.children:
            if isinstance(child, SExp):
                if child.name == name:
                    yield child
                if recursive:
                    yield from child.find_all(name, recursive=True)

    def find(self, name: str, *, recursive: bool = True) -> SExp | None:
        """The first match of :meth:`find_all`, or ``None``."""
        return next(self.find_all(name, recursive=recursive), None)

    def values(self) -> list[str]:
        """Values of the immediate atom children, excluding the leading name."""
        return [c.value for c in self.children[1:] if isinstance(c, Atom)]

    def __getitem__(self, index: int) -> Node:
        return self.children[index]

    def __len__(self) -> int:
        return len(self.children)

    def __iter__(self) -> Iterator[Node]:
        return iter(self.children)

    # -- serialisation --------------------------------------------------

    def write(self, out: list[str]) -> None:
        if self.pre:
            out.append(self.pre)
        out.append("(")
        for child in self.children:
            child.write(out)
        if self.pre_close:
            out.append(self.pre_close)
        out.append(")")

    def __repr__(self) -> str:
        return f"SExp({self.name!r}, {len(self.children)} children)"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SExp):
            return self.children == other.children
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]


class Document:
    """A parsed file: top-level nodes plus any trailing whitespace.

    KiCad files hold exactly one top-level list, but the parser does not enforce
    that — being permissive here costs nothing and keeps the module usable for
    library tables and fragments.
    """

    __slots__ = ("nodes", "trailing")

    def __init__(self, nodes: list[Node], trailing: str = "") -> None:
        self.nodes = nodes
        self.trailing = trailing

    @property
    def root(self) -> SExp:
        """The single top-level list.

        Raises if the document does not have exactly one, because callers that
        want ``root`` are working with a real KiCad file and a surprise here
        should be loud.
        """
        lists = [n for n in self.nodes if isinstance(n, SExp)]
        if len(lists) != 1:
            raise SExprError(
                f"expected exactly one top-level expression, found {len(lists)}", "", 0
            )
        return lists[0]

    def write(self, out: list[str]) -> None:
        for node in self.nodes:
            node.write(out)
        if self.trailing:
            out.append(self.trailing)

    def __repr__(self) -> str:
        return f"Document({len(self.nodes)} top-level nodes)"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def loads(text: str) -> Document:
    """Parse ``text`` into a :class:`Document`, preserving formatting exactly."""
    return _Parser(text).parse()


def load(path, *, encoding: str = "utf-8") -> Document:  # type: ignore[no-untyped-def]
    """Parse the file at ``path``.

    Newlines are read verbatim (``newline=""``) so that a CRLF file round-trips
    as CRLF rather than being silently converted.
    """
    with open(path, encoding=encoding, newline="") as fh:
        return loads(fh.read())


class _Parser:
    __slots__ = ("i", "n", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.n = len(text)

    def parse(self) -> Document:
        nodes: list[Node] = []
        while True:
            pre = self._skip_trivia()
            if self.i >= self.n:
                return Document(nodes, trailing=pre)
            ch = self.text[self.i]
            if ch == ")":
                raise SExprError("unexpected ')'", self.text, self.i)
            nodes.append(self._parse_node(pre))

    def _parse_node(self, pre: str) -> Node:
        if self.text[self.i] == "(":
            return self._parse_list(pre)
        return self._parse_atom(pre)

    def _parse_list(self, pre: str) -> SExp:
        open_at = self.i
        self.i += 1  # consume '('
        children: list[Node] = []
        while True:
            trivia = self._skip_trivia()
            if self.i >= self.n:
                raise SExprError("unterminated '('", self.text, open_at)
            if self.text[self.i] == ")":
                self.i += 1
                return SExp(children, pre=pre, pre_close=trivia)
            children.append(self._parse_node(trivia))

    def _parse_atom(self, pre: str) -> Atom:
        start = self.i
        if self.text[self.i] == '"':
            value = self._consume_quoted()
            return Atom(value, quoted=True, raw=self.text[start : self.i], pre=pre)
        while self.i < self.n and self.text[self.i] not in _ATOM_END:
            self.i += 1
        if self.i == start:  # pragma: no cover - guarded by caller
            raise SExprError(f"unexpected {self.text[self.i]!r}", self.text, self.i)
        raw = self.text[start : self.i]
        return Atom(raw, quoted=False, raw=raw, pre=pre)

    def _consume_quoted(self) -> str:
        open_at = self.i
        self.i += 1  # consume opening quote
        parts: list[str] = []
        while True:
            if self.i >= self.n:
                raise SExprError("unterminated string", self.text, open_at)
            ch = self.text[self.i]
            if ch == '"':
                self.i += 1
                return "".join(parts)
            if ch == "\\":
                if self.i + 1 >= self.n:
                    raise SExprError("unterminated escape", self.text, self.i)
                nxt = self.text[self.i + 1]
                # An unrecognised escape keeps both characters. KiCad writes
                # backslashes in field text (Windows paths, LaTeX-ish notation)
                # without escaping them, and dropping the backslash would
                # silently corrupt user data.
                parts.append(_ESCAPES.get(nxt, "\\" + nxt))
                self.i += 2
                continue
            parts.append(ch)
            self.i += 1

    def _skip_trivia(self) -> str:
        """Consume whitespace and ``;`` comments, returning the text consumed."""
        start = self.i
        while self.i < self.n:
            ch = self.text[self.i]
            if ch in " \t\r\n":
                self.i += 1
            elif ch == ";":
                while self.i < self.n and self.text[self.i] != "\n":
                    self.i += 1
            else:
                break
        return self.text[start : self.i]


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def dumps(doc: Document | Node) -> str:
    """Serialise, reproducing the source text of everything left untouched."""
    out: list[str] = []
    doc.write(out)
    return "".join(out)


def dump(doc: Document | Node, path, *, encoding: str = "utf-8") -> None:  # type: ignore[no-untyped-def]
    """Write to ``path``. Newlines pass through unchanged."""
    with open(path, "w", encoding=encoding, newline="") as fh:
        fh.write(dumps(doc))


def dumps_canonical(
    doc: Document | SExp,
    *,
    indent: str = "  ",
    float_precision: int = 6,
) -> str:
    """Serialise with klm's canonical formatting, discarding source text.

    For generated files only — never for a file a user has edited. Guarantees
    that regenerating unchanged content produces identical bytes, which is what
    makes ``git diff`` on a vendored library meaningful.

    A list whose children are all atoms stays on one line; otherwise each child
    gets its own line. Numeric atoms are normalised to ``float_precision``
    decimal places with trailing zeros stripped, so ``1.270000`` and ``1.27``
    converge.
    """
    root = doc.root if isinstance(doc, Document) else doc
    out: list[str] = []
    _write_canonical(root, out, indent=indent, depth=0, precision=float_precision)
    out.append("\n")
    return "".join(out)


def _write_canonical(
    node: Node, out: list[str], *, indent: str, depth: int, precision: int
) -> None:
    if isinstance(node, Atom):
        out.append(_canonical_atom(node, precision))
        return

    assert isinstance(node, SExp)
    if not node.children:
        out.append("()")
        return

    # The leading run of atoms stays on the opening line, so a list of only
    # atoms is one line and `(footprint "R_0402"` keeps its name and value
    # together above the nested nodes — which is how KiCad writes it too.
    lead = 0
    while lead < len(node.children) and isinstance(node.children[lead], Atom):
        lead += 1
    head = node.children[:lead]
    rest = node.children[lead:]

    out.append("(")
    if head:
        out.append(" ".join(_canonical_atom(c, precision) for c in head))  # type: ignore[arg-type]
    pad = indent * (depth + 1)
    for child in rest:
        out.append("\n")
        out.append(pad)
        _write_canonical(child, out, indent=indent, depth=depth + 1, precision=precision)
    out.append(")")


def _canonical_atom(atom: Atom, precision: int) -> str:
    if not atom.quoted:
        normalised = _normalise_number(atom.value, precision)
        if normalised is not None:
            return normalised
    return atom.token()


_NUMBER = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)$")


def _normalise_number(text: str, precision: int) -> str | None:
    """Return a canonical form for a decimal number, or ``None`` if not one.

    Integers are left alone. Only decimals are reformatted, which keeps pin
    numbers, layer indices and version stamps exactly as written.
    """
    if not _NUMBER.match(text) or "." not in text:
        return None
    try:
        value = float(text)
    except ValueError:  # pragma: no cover - regex already guarantees this parses
        return None
    formatted = f"{value:.{precision}f}".rstrip("0").rstrip(".")
    if formatted in ("", "-0"):
        return "0"
    return formatted


# ---------------------------------------------------------------------------
# Atom text helpers
# ---------------------------------------------------------------------------


def _needs_quoting(value: str) -> bool:
    return not _BARE_SAFE.match(value)


def _needs_separator(out: list[str], text: str) -> bool:
    """Would appending ``text`` directly fuse it with the preceding token?

    Parentheses and quotes delimit themselves, so a separator is only required
    between two bare tokens.
    """
    if not text or text[0] in _SELF_DELIMITING:
        return False
    for chunk in reversed(out):
        if chunk:
            last = chunk[-1]
            return not (last.isspace() or last in _SELF_DELIMITING)
    return False


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'
