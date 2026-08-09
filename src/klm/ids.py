"""Part identity: opaque, immutable, lexicographically sortable.

A ``klm_id`` is a ULID — 48 bits of millisecond timestamp followed by 80 bits of
randomness, rendered as 26 Crockford Base32 characters. It is generated once and
never changes, because every sync operation depends on being able to match a
schematic symbol back to a catalog part after renames, MPN corrections and
collaborator edits (docs/adr/0003).

ULID rather than UUID4 for three reasons that all matter here: IDs sort by
creation time, the alphabet excludes I/L/O/U so it survives being read aloud or
transcribed off a label, and the shortened prefix stays unambiguous on a 14 mm
drawer label (docs/10 §7).
"""

from __future__ import annotations

import os
import time

__all__ = ["ULID_LENGTH", "is_valid", "new_id", "short_id", "timestamp_of"]

# Crockford Base32: no I, L, O or U, so there is nothing to confuse with 1 or 0.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_ALPHABET)}
# Crockford treats these as their digit lookalikes when reading.
_DECODE.update({"I": 1, "L": 1, "O": 0, "U": _DECODE["V"]})

ULID_LENGTH = 26
_TIMESTAMP_CHARS = 10
_RANDOM_BITS = 80


def new_id(*, timestamp_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Generate a new identifier.

    Both inputs can be supplied to make generation deterministic in tests; in
    normal use they come from the clock and the OS entropy source.
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    if randomness is None:
        randomness = os.urandom(_RANDOM_BITS // 8)
    if len(randomness) != _RANDOM_BITS // 8:
        raise ValueError(f"randomness must be {_RANDOM_BITS // 8} bytes")

    value = (timestamp_ms << _RANDOM_BITS) | int.from_bytes(randomness, "big")
    return _encode(value, ULID_LENGTH)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars))


def is_valid(candidate: str) -> bool:
    """True if ``candidate`` is a well-formed identifier.

    Accepts Crockford's lookalike characters on input, since an ID may have been
    transcribed by hand from a label.
    """
    if len(candidate) != ULID_LENGTH:
        return False
    return all(c in _DECODE for c in candidate.upper())


def timestamp_of(klm_id: str) -> int:
    """Milliseconds since the epoch encoded in ``klm_id``."""
    if not is_valid(klm_id):
        raise ValueError(f"not a valid klm_id: {klm_id!r}")
    value = 0
    for char in klm_id.upper()[:_TIMESTAMP_CHARS]:
        value = value * 32 + _DECODE[char]
    return value


def short_id(klm_id: str, length: int = 8) -> str:
    """A shortened form for labels and human reference.

    Not unique by construction — the caller checks for collisions against the
    catalog. Taken from the *end* of the ID, because the leading characters are
    a timestamp and parts created in the same session share them.
    """
    if not is_valid(klm_id):
        raise ValueError(f"not a valid klm_id: {klm_id!r}")
    return klm_id.upper()[-length:]
