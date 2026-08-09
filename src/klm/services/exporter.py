"""Export and import between the database and the git-versioned mirror.

The database is the operational store; ``catalog/<klm_id>/part.yaml`` is what
git tracks. The pair must round-trip exactly, because the database being
*reconstructible* from the export is what makes ADR-0001's arrangement safe
rather than merely convenient.

Two properties are load-bearing and both are tested:

* ``export`` is byte-reproducible — the same content always produces the same
  bytes, so a re-export with no changes leaves ``git status`` clean.
* ``export(import(export(db)))`` is byte-identical to ``export(db)``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from klm.model import Part
from klm.serial.part_file import from_yaml, to_yaml
from klm.serial.yaml import YamlError
from klm.services.catalog import get_part, list_parts, save_part

__all__ = ["ExportResult", "ImportResult", "export_catalog", "import_catalog"]

PART_FILE = "part.yaml"


@dataclass
class ExportResult:
    written: list[str] = field(default_factory=list)
    """Parts whose file changed on disk."""
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    """Directories deleted because the part no longer exists."""

    @property
    def total(self) -> int:
        return len(self.written) + len(self.unchanged)


@dataclass
class ImportResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def export_catalog(
    conn: sqlite3.Connection, catalog_dir: Path, *, prune: bool = False
) -> ExportResult:
    """Write every part to ``catalog_dir``.

    A file is only rewritten when its bytes would actually change. That is what
    keeps mtimes stable and makes "nothing to commit" mean nothing changed.

    ``prune`` removes directories for parts no longer in the database. It is off
    by default because deleting a user's files should be something they asked
    for explicitly.
    """
    catalog_dir = Path(catalog_dir)
    catalog_dir.mkdir(parents=True, exist_ok=True)
    result = ExportResult()

    seen: set[str] = set()
    for part in list_parts(conn):
        seen.add(part.klm_id)
        target = catalog_dir / part.klm_id / PART_FILE
        content = to_yaml(part)

        if target.exists() and target.read_text(encoding="utf-8") == content:
            result.unchanged.append(part.klm_id)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, content)
        result.written.append(part.klm_id)

    if prune:
        for directory in sorted(catalog_dir.iterdir()):
            if directory.is_dir() and directory.name not in seen:
                _remove_tree(directory)
                result.removed.append(directory.name)

    return result


def import_catalog(
    conn: sqlite3.Connection, catalog_dir: Path, *, strict: bool = False
) -> ImportResult:
    """Load every ``part.yaml`` under ``catalog_dir`` into the database.

    By default a malformed file is collected as an error and the remaining parts
    still import, so one bad file does not block a restore. ``strict`` raises on
    the first problem instead.
    """
    catalog_dir = Path(catalog_dir)
    result = ImportResult()
    if not catalog_dir.is_dir():
        return result

    for path in sorted(catalog_dir.glob(f"*/{PART_FILE}")):
        try:
            part = from_yaml(path.read_text(encoding="utf-8"))
        except (YamlError, OSError) as exc:
            if strict:
                raise
            result.errors.append((path, str(exc)))
            continue

        if part.klm_id != path.parent.name:
            message = (
                f"klm_id {part.klm_id!r} does not match its directory {path.parent.name!r}"
            )
            if strict:
                raise YamlError(message)
            result.errors.append((path, message))
            continue

        before = get_part(conn, part.klm_id)
        if before is None:
            save_part(conn, part)
            result.created.append(part.klm_id)
        elif _differs(before, part):
            save_part(conn, part)
            result.updated.append(part.klm_id)
        else:
            result.unchanged.append(part.klm_id)

    return result


def _differs(before: Part, after: Part) -> bool:
    """Compare on exported content, ignoring bookkeeping timestamps.

    Two parts that export to identical bytes are the same part, even if their
    ``updated_at`` differs — otherwise every import would mark everything dirty.
    """
    return to_yaml(_without_timestamps(before)) != to_yaml(_without_timestamps(after))


def _without_timestamps(part: Part) -> Part:
    from dataclasses import replace

    return replace(part, created_at=None, updated_at=None)


def _write_atomic(target: Path, content: str) -> None:
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def _remove_tree(directory: Path) -> None:
    for child in sorted(directory.iterdir(), reverse=True):
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    directory.rmdir()
