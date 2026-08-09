"""``klm.lock.json`` — the contract between a repository and the catalog.

The lock is what makes vendoring reversible and auditable rather than a one-way
export. For each part it records **both** the catalog hash at vendoring time and
the hash of the copy that was written into the project. Those two numbers are
what let ``klm sync status`` tell "the catalog moved on" from "someone edited
the project copy" from "both changed" — three cases that need different
handling, and conflating which is how sync tools lose data (docs/06 §2).

The file is committed. It is therefore written for humans and for git: one
object per part, keys sorted, parts ordered by ``klm_id``, so a merge conflict
is per-part and readable rather than a single unreviewable blob.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from klm import __version__

__all__ = [
    "FORMAT_VERSION",
    "LockEntry",
    "LockError",
    "LockFile",
    "read_lock",
    "render_lock",
    "write_lock",
]

FORMAT_VERSION = 1


class LockError(Exception):
    """Raised when a lock file cannot be read as one."""


@dataclass
class LockEntry:
    """One vendored part: what it was, and what both copies hashed to."""

    klm_id: str
    mpn: str = ""
    symbol_name: str | None = None
    """``None`` for a footprint-only entry — a mounting hole placed on the board
    but never on a schematic still needs its footprint vendored (docs/06 §7)."""
    footprint_name: str | None = None
    model_name: str | None = None
    global_symbol_hash: str | None = None
    global_footprint_hash: str | None = None
    global_model3d_hash: str | None = None
    vendored_symbol_hash: str | None = None
    vendored_footprint_hash: str | None = None
    vendored_model3d_hash: str | None = None
    references: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "klm_id": self.klm_id,
            "mpn": self.mpn,
            "symbol_name": self.symbol_name,
            "footprint_name": self.footprint_name,
            "model_name": self.model_name,
            "global_symbol_hash": self.global_symbol_hash,
            "global_footprint_hash": self.global_footprint_hash,
            "global_model3d_hash": self.global_model3d_hash,
            "vendored_symbol_hash": self.vendored_symbol_hash,
            "vendored_footprint_hash": self.vendored_footprint_hash,
            "vendored_model3d_hash": self.vendored_model3d_hash,
            "references": list(self.references),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> LockEntry:
        klm_id = raw.get("klm_id")
        if not isinstance(klm_id, str) or not klm_id:
            raise LockError("lock entry has no klm_id")
        return cls(
            klm_id=klm_id,
            mpn=_text(raw.get("mpn")) or "",
            symbol_name=_text(raw.get("symbol_name")),
            footprint_name=_text(raw.get("footprint_name")),
            model_name=_text(raw.get("model_name")),
            global_symbol_hash=_text(raw.get("global_symbol_hash")),
            global_footprint_hash=_text(raw.get("global_footprint_hash")),
            global_model3d_hash=_text(raw.get("global_model3d_hash")),
            vendored_symbol_hash=_text(raw.get("vendored_symbol_hash")),
            vendored_footprint_hash=_text(raw.get("vendored_footprint_hash")),
            vendored_model3d_hash=_text(raw.get("vendored_model3d_hash")),
            references=tuple(str(r) for r in raw.get("references") or ()),
        )


@dataclass
class LockFile:
    library_name: str
    include_3d: bool = False
    entries: list[LockEntry] = field(default_factory=list)
    vendored_at: str | None = None
    klm_version: str = __version__
    format_version: int = FORMAT_VERSION

    def sorted_entries(self) -> list[LockEntry]:
        return sorted(self.entries, key=lambda e: e.klm_id)

    def by_id(self, klm_id: str) -> LockEntry | None:
        return next((e for e in self.entries if e.klm_id == klm_id), None)

    def by_symbol(self, symbol_name: str) -> LockEntry | None:
        return next((e for e in self.entries if e.symbol_name == symbol_name), None)

    def by_footprint(self, footprint_name: str) -> LockEntry | None:
        return next((e for e in self.entries if e.footprint_name == footprint_name), None)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "format_version": self.format_version,
            "klm_version": self.klm_version,
            "library_name": self.library_name,
            "include_3d": self.include_3d,
            "parts": [entry.to_json() for entry in self.sorted_entries()],
        }
        if self.vendored_at is not None:
            payload["vendored_at"] = self.vendored_at
        return payload

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> LockFile:
        version = raw.get("format_version")
        if version != FORMAT_VERSION:
            raise LockError(
                f"lock format version {version!r} is not supported "
                f"(this klm writes version {FORMAT_VERSION})"
            )
        parts = raw.get("parts")
        if not isinstance(parts, list):
            raise LockError("lock file has no 'parts' list")
        library_name = _text(raw.get("library_name"))
        if not library_name:
            raise LockError("lock file has no library_name")
        return cls(
            library_name=library_name,
            include_3d=bool(raw.get("include_3d", False)),
            entries=[LockEntry.from_json(item) for item in parts],
            vendored_at=_text(raw.get("vendored_at")),
            klm_version=_text(raw.get("klm_version")) or "",
            format_version=FORMAT_VERSION,
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def render_lock(lock: LockFile) -> str:
    """The exact bytes of the lock file, so callers can diff without writing."""
    return json.dumps(lock.to_json(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def read_lock(path: Path) -> LockFile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LockError(f"no lock file at {path}") from exc
    except json.JSONDecodeError as exc:
        raise LockError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise LockError(f"{path} is not a lock file")
    return LockFile.from_json(raw)


def write_lock(lock: LockFile, path: Path) -> bool:
    """Write the lock atomically. Returns True if the bytes changed."""
    content = render_lock(lock)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".klm-tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return True
