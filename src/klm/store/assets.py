"""Content-addressed asset storage.

An asset's filename *is* the hash of its canonical bytes. Three properties fall
out of that, all of them things klm needs (docs/04-catalog-and-storage.md §4):

* **Deduplication is automatic.** Fifty 0402 resistors share one footprint and
  one 3D model. At catalog scale that is the difference between a repository of
  a couple of gigabytes and one of a couple of hundred megabytes, because STEP
  files dominate.
* **Mutation is impossible.** "Fixing" a footprint produces a new hash. Migrating
  parts onto it is an explicit, listable operation rather than a silent rewrite
  of everything that referenced the old one.
* **Equality is cheap.** Sync compares hashes, not file contents.

Canonicalisation happens *before* hashing, so two files that differ only in
formatting share an address. Without it, re-exporting the same footprint from
KiCad with different float formatting would look like a new asset.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from enum import StrEnum
from pathlib import Path

from klm.kicad import sexpr

__all__ = ["AssetKind", "AssetStore", "canonicalize", "hash_bytes"]

_HASH_PREFIX = "sha256:"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class AssetKind(StrEnum):
    """The three kinds of file klm manages on a part's behalf."""

    SYMBOL = "symbol"
    FOOTPRINT = "footprint"
    MODEL3D = "model3d"

    @property
    def suffix(self) -> str:
        return {
            AssetKind.SYMBOL: ".kicad_sym",
            AssetKind.FOOTPRINT: ".kicad_mod",
            AssetKind.MODEL3D: ".step",
        }[self]

    @property
    def dirname(self) -> str:
        return {
            AssetKind.SYMBOL: "symbols",
            AssetKind.FOOTPRINT: "footprints",
            AssetKind.MODEL3D: "models3d",
        }[self]


class AssetError(Exception):
    """Raised for a malformed hash or a missing asset."""


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------

# STEP files record when they were written. Two exports of identical geometry
# differ only in that timestamp, which would defeat deduplication entirely, so
# it is blanked inside the HEADER section before hashing.
_STEP_HEADER = re.compile(rb"HEADER;(.*?)ENDSEC;", re.DOTALL | re.IGNORECASE)
_ISO_TIMESTAMP = re.compile(rb"'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^']*'")


def canonicalize(data: bytes, kind: AssetKind) -> bytes:
    """Return the bytes that should be hashed for ``data``.

    Symbols and footprints go through the canonical S-expression writer, so
    indentation and float spelling stop mattering. 3D models get line endings
    normalised and their header timestamp blanked.

    Input that fails to parse is hashed verbatim rather than rejected — the
    store's job is to hold bytes, and validation belongs to the QA gate.
    """
    if kind is AssetKind.MODEL3D:
        return _canonicalize_step(data)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    try:
        return sexpr.dumps_canonical(sexpr.loads(text)).encode("utf-8")
    except sexpr.SExprError:
        return data


def _canonicalize_step(data: bytes) -> bytes:
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    def blank_timestamps(match: re.Match[bytes]) -> bytes:
        header = _ISO_TIMESTAMP.sub(b"''", match.group(1))
        return b"HEADER;" + header + b"ENDSEC;"

    return _STEP_HEADER.sub(blank_timestamps, normalized, count=1)


def hash_bytes(data: bytes, kind: AssetKind) -> str:
    """Content hash of ``data``, in ``sha256:<hex>`` form."""
    digest = hashlib.sha256(canonicalize(data, kind)).hexdigest()
    return f"{_HASH_PREFIX}{digest}"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class AssetStore:
    """Reads and writes assets under a root directory, addressed by content."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def dir_for(self, kind: AssetKind) -> Path:
        return self.root / kind.dirname

    def path_for(self, content_hash: str, kind: AssetKind) -> Path:
        """Where an asset with this hash lives. Does not check existence."""
        _validate_hash(content_hash)
        digest = content_hash[len(_HASH_PREFIX) :]
        return self.dir_for(kind) / f"{digest}{kind.suffix}"

    def exists(self, content_hash: str, kind: AssetKind) -> bool:
        return self.path_for(content_hash, kind).exists()

    def add_bytes(self, data: bytes, kind: AssetKind) -> str:
        """Store ``data`` and return its hash.

        Idempotent: storing identical content twice is a no-op that returns the
        same hash. The write goes to a temporary file and is then moved into
        place, so an interrupted write cannot leave a truncated asset sitting at
        a hash that claims to describe complete content.
        """
        content_hash = hash_bytes(data, kind)
        target = self.path_for(content_hash, kind)
        if target.exists():
            return content_hash

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        return content_hash

    def add_file(self, path: str | Path, kind: AssetKind) -> str:
        return self.add_bytes(Path(path).read_bytes(), kind)

    def read(self, content_hash: str, kind: AssetKind) -> bytes:
        path = self.path_for(content_hash, kind)
        if not path.exists():
            raise AssetError(f"asset not found: {content_hash} ({kind.value})")
        return path.read_bytes()

    def read_text(self, content_hash: str, kind: AssetKind, *, encoding: str = "utf-8") -> str:
        return self.read(content_hash, kind).decode(encoding)

    def copy_to(self, content_hash: str, kind: AssetKind, dest: str | Path) -> Path:
        """Copy an asset out to ``dest``, for generation and vendoring."""
        source = self.path_for(content_hash, kind)
        if not source.exists():
            raise AssetError(f"asset not found: {content_hash} ({kind.value})")
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def list_hashes(self, kind: AssetKind) -> list[str]:
        directory = self.dir_for(kind)
        if not directory.is_dir():
            return []
        return sorted(
            f"{_HASH_PREFIX}{p.name[: -len(kind.suffix)]}"
            for p in directory.iterdir()
            if p.is_file() and p.name.endswith(kind.suffix)
        )

    def verify(self, content_hash: str, kind: AssetKind) -> bool:
        """Re-hash stored content and confirm it still matches its address.

        Detects on-disk corruption. Not run routinely — this is what
        ``klm doctor --deep`` would call.
        """
        try:
            data = self.read(content_hash, kind)
        except AssetError:
            return False
        return hash_bytes(data, kind) == content_hash

    def total_bytes(self) -> int:
        return sum(
            p.stat().st_size
            for kind in AssetKind
            for p in self.dir_for(kind).glob("*")
            if p.is_file()
        )


def _validate_hash(content_hash: str) -> None:
    if not _HASH_RE.match(content_hash):
        raise AssetError(f"malformed content hash: {content_hash!r}")
