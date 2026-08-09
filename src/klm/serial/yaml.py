"""A deterministic YAML emitter and reader for a restricted subset.

klm exports one file per part into a git repository, so the export has to be
*byte-reproducible*: the same catalog content must always produce the same
bytes, or every re-export churns the history and the diffs stop meaning
anything (docs/adr/0001, docs/adr/0008).

A general YAML library gives no such guarantee. Its output formatting is an
implementation detail that can shift between releases — a change in quoting
style or line folding would rewrite every file in the catalog for no semantic
reason. So klm owns the emitter, and the subset is deliberately small:

* block style only, one field per line, so a one-field change is a one-line diff
* mappings, sequences, strings, integers, floats, booleans and null
* no anchors, aliases, tags, multi-document streams, flow style or folded scalars

Anything outside that subset raises rather than being guessed at.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = ["YamlError", "dumps", "loads"]

_INDENT = "  "

# Plain (unquoted) scalars must not collide with any other YAML construct, and
# must not be re-read as a different type than they were written as. Colons are
# permitted because nearly every value in a part file contains one — content
# hashes and datasheet URLs — and quoting them all would make the format far
# less readable. Only `": "` is ambiguous, and that is rejected separately.
_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-./+:@ ]*$")
_KEY_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_WORDS = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "none", "~", ""}
)
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")

# `str.splitlines()` breaks on more than \n and \r — U+0085, U+2028 and U+2029
# among them. A raw one of those inside a quoted string would be read back as a
# line break and corrupt the document, so every such character is escaped.
_SHORT_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _must_escape(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or code == 0x7F or code in (0x85, 0x2028, 0x2029)


class YamlError(ValueError):
    """Raised when input is outside the supported subset or is malformed."""

    def __init__(self, message: str, line_number: int | None = None) -> None:
        if line_number is not None:
            message = f"{message} at line {line_number}"
        super().__init__(message)
        self.line_number = line_number


# ---------------------------------------------------------------------------
# Emitting
# ---------------------------------------------------------------------------


def dumps(data: Any) -> str:
    """Serialise ``data`` deterministically.

    Mapping keys are written in insertion order rather than sorted: the caller
    controls the field order, because a part reads far better with ``mpn`` near
    the top than alphabetically between ``lifecycle`` and ``notes``. Determinism
    comes from the caller building the mapping the same way every time.
    """
    if not isinstance(data, dict):
        raise YamlError("top level must be a mapping")
    lines: list[str] = []
    _emit_mapping(data, lines, depth=0)
    return "".join(f"{line}\n" for line in lines)


def _emit_mapping(mapping: dict[str, Any], lines: list[str], *, depth: int) -> None:
    pad = _INDENT * depth
    for key, value in mapping.items():
        _check_key(key)
        if isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}:")
                _emit_mapping(value, lines, depth=depth + 1)
        elif isinstance(value, list):
            if not value:
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}:")
                _emit_sequence(value, lines, depth=depth + 1)
        else:
            lines.append(f"{pad}{key}: {_scalar(value)}")


def _emit_sequence(items: list[Any], lines: list[str], *, depth: int) -> None:
    pad = _INDENT * depth
    for item in items:
        if isinstance(item, dict):
            if not item:
                lines.append(f"{pad}- {{}}")
                continue
            # The first key rides the dash; the rest align under it.
            nested: list[str] = []
            _emit_mapping(item, nested, depth=depth + 1)
            first, *rest = nested
            lines.append(f"{pad}- {first.lstrip()}")
            lines.extend(rest)
        elif isinstance(item, list):
            raise YamlError("nested sequences are outside the supported subset")
        else:
            lines.append(f"{pad}- {_scalar(item)}")


def _check_key(key: object) -> None:
    if not isinstance(key, str):
        raise YamlError(f"mapping keys must be strings, got {type(key).__name__}")
    if not _KEY_SAFE.match(key):
        raise YamlError(f"unsupported mapping key: {key!r}")


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, str):
        return _string(value)
    raise YamlError(f"unsupported scalar type: {type(value).__name__}")


def _float(value: float) -> str:
    """Format a float so that reading it back yields the identical value.

    ``repr`` gives the shortest representation that round-trips exactly, which
    is what byte-reproducibility needs. Non-finite values have no portable YAML
    spelling, so they are refused rather than written as something a reader
    would mangle.
    """
    if math.isnan(value) or math.isinf(value):
        raise YamlError(f"cannot serialise non-finite float: {value}")
    text = repr(value)
    # Keep a decimal point so the value reads back as a float, not an int.
    if _INT.match(text):
        text += ".0"
    return text


def _string(value: str) -> str:
    needs_quoting = (
        not _PLAIN_SAFE.match(value)
        or ": " in value
        or value.endswith(":")
        or value.lower() in _RESERVED_WORDS
        or bool(_INT.match(value))
        or bool(_FLOAT.match(value))
        or value != value.strip()
    )
    if not needs_quoting:
        return value

    out: list[str] = ['"']
    for char in value:
        if char in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[char])
        elif _must_escape(char):
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def loads(text: str) -> dict[str, Any]:
    """Parse a document produced by :func:`dumps`."""
    lines = _significant_lines(text)
    if not lines:
        return {}
    value, consumed = _parse_block(lines, 0, lines[0][0])
    if consumed != len(lines):
        raise YamlError("unexpected content", lines[consumed][2])
    if not isinstance(value, dict):
        raise YamlError("top level must be a mapping", 1)
    return value


def _significant_lines(text: str) -> list[tuple[int, str, int]]:
    """Return ``(indent, content, line_number)`` for each meaningful line."""
    out: list[tuple[int, str, int]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError("tabs are not valid indentation", number)
        out.append((len(raw) - len(raw.lstrip()), stripped, number))
    return out


def _parse_block(
    lines: list[tuple[int, str, int]], start: int, indent: int
) -> tuple[Any, int]:
    if lines[start][1].startswith("- "):
        return _parse_sequence(lines, start, indent)
    return _parse_mapping(lines, start, indent)


def _parse_mapping(
    lines: list[tuple[int, str, int]], start: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    i = start
    while i < len(lines):
        line_indent, content, number = lines[i]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise YamlError("unexpected indentation", number)
        if content.startswith("- "):
            break

        key, _, rest = content.partition(":")
        if not _:
            raise YamlError(f"expected 'key: value', got {content!r}", number)
        key = key.strip()
        rest = rest.strip()
        if key in result:
            raise YamlError(f"duplicate key {key!r}", number)

        if rest:
            result[key] = _parse_scalar(rest, number)
            i += 1
            continue

        # A bare `key:` introduces a nested block, or an explicit empty value.
        if i + 1 < len(lines) and lines[i + 1][0] > indent:
            value, i = _parse_block(lines, i + 1, lines[i + 1][0])
            result[key] = value
        elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
            value, i = _parse_sequence(lines, i + 1, indent)
            result[key] = value
        else:
            result[key] = None
            i += 1
    return result, i


def _parse_sequence(
    lines: list[tuple[int, str, int]], start: int, indent: int
) -> tuple[list[Any], int]:
    result: list[Any] = []
    i = start
    while i < len(lines):
        line_indent, content, number = lines[i]
        if line_indent < indent or not content.startswith("- "):
            break
        if line_indent > indent:
            raise YamlError("unexpected indentation", number)

        item = content[2:].strip()
        item_indent = line_indent + 2

        if _looks_like_mapping_entry(item):
            # Re-read the first field at the indentation its siblings will use.
            block = [(item_indent, item, number)]
            j = i + 1
            while j < len(lines) and lines[j][0] >= item_indent:
                block.append(lines[j])
                j += 1
            value, consumed = _parse_mapping(block, 0, item_indent)
            if consumed != len(block):
                raise YamlError("unexpected content in sequence item", number)
            result.append(value)
            i = j
        else:
            result.append(_parse_scalar(item, number))
            i += 1
    return result, i


def _looks_like_mapping_entry(item: str) -> bool:
    if item.startswith(('"', "'")):
        return False
    key, sep, rest = item.partition(":")
    return bool(sep) and bool(key.strip()) and (not rest or rest.startswith(" ") or rest == "")


def _parse_scalar(text: str, line_number: int) -> Any:
    if text.startswith('"'):
        return _parse_quoted(text, line_number)
    if text == "{}":
        return {}
    if text == "[]":
        return []
    lowered = text.lower()
    if lowered in ("null", "~"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def _parse_quoted(text: str, line_number: int) -> str:
    if len(text) < 2 or not text.endswith('"'):
        raise YamlError("unterminated quoted string", line_number)
    body = text[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue

        if i + 1 >= len(body):
            raise YamlError("unterminated escape", line_number)
        marker = body[i + 1]
        if marker == "u":
            hex_digits = body[i + 2 : i + 6]
            if len(hex_digits) != 4:
                raise YamlError("truncated \\u escape", line_number)
            try:
                out.append(chr(int(hex_digits, 16)))
            except ValueError:
                raise YamlError(f"invalid \\u escape: \\u{hex_digits}", line_number) from None
            i += 6
        else:
            out.append(_ESCAPES.get(marker, marker))
            i += 2
    return "".join(out)
