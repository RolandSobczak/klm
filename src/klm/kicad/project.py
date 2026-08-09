"""Finding the pieces of a KiCad project on disk.

A project is a directory containing a ``.kicad_pro`` and, beside it, one or more
``.kicad_sch`` sheets and usually a ``.kicad_pcb``. klm needs all of them: the
schematic carries the symbols, the board carries footprints that never appear in
a schematic at all (mounting holes, fiducials), and vendoring has to reach both.

Nothing here parses; it only locates. Discovery is deliberately separate from
reading so that a missing board is a fact a caller can inspect rather than an
exception thrown from inside the vendor pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "LIBRARIES_DIR",
    "LOCK_FILE",
    "MODELS_DIR",
    "KiCadProject",
    "ProjectError",
    "find_project",
]

#: Where a vendored project keeps everything it needs to open on its own.
LIBRARIES_DIR = "libraries"
MODELS_DIR = "packages3d"
LOCK_FILE = "klm.lock.json"

#: KiCad writes timestamped backup copies beside the project. Vendoring one
#: would rewrite a snapshot of a past state, which is never what was meant.
_SKIP_DIR_SUFFIXES = ("-backups",)


class ProjectError(Exception):
    """Raised when a path is not a usable KiCad project."""


@dataclass(frozen=True)
class KiCadProject:
    """One KiCad project and the files klm may need to read or rewrite."""

    root: Path
    name: str
    pro_file: Path | None
    schematics: tuple[Path, ...]
    board: Path | None

    # -- vendored layout -------------------------------------------------

    @property
    def libraries(self) -> Path:
        return self.root / LIBRARIES_DIR

    @property
    def models3d(self) -> Path:
        return self.libraries / MODELS_DIR

    @property
    def lock_file(self) -> Path:
        return self.root / LOCK_FILE

    @property
    def sym_lib_table(self) -> Path:
        return self.root / "sym-lib-table"

    @property
    def fp_lib_table(self) -> Path:
        return self.root / "fp-lib-table"

    def symbol_library(self, library_name: str) -> Path:
        return self.libraries / f"{library_name}.kicad_sym"

    def footprint_library(self, library_name: str) -> Path:
        return self.libraries / f"{library_name}.pretty"

    @property
    def is_vendored(self) -> bool:
        """A project is vendored exactly when it carries a lock file.

        The lock is the contract, not the ``libraries/`` directory: a project
        with libraries and no lock is the case ``klm sync adopt`` exists for.
        """
        return self.lock_file.exists()

    def design_files(self) -> tuple[Path, ...]:
        """Every file whose ``lib_id`` references vendoring rewrites."""
        return (*self.schematics, *([self.board] if self.board else ()))


def find_project(path: str | Path) -> KiCadProject:
    """Locate the project at ``path``, which may be a directory or a project file.

    Raises rather than guessing when a directory holds two projects: picking one
    would silently vendor the wrong schematic.
    """
    target = Path(path).expanduser().resolve()

    if target.is_file():
        if target.suffix not in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
            raise ProjectError(f"not a KiCad project file: {target}")
        root, stem = target.parent, target.stem
    elif target.is_dir():
        root, stem = target, None
    else:
        raise ProjectError(f"no such path: {target}")

    pro_files = sorted(root.glob("*.kicad_pro"))
    if stem is None and len(pro_files) > 1:
        names = ", ".join(p.name for p in pro_files)
        raise ProjectError(f"{root} holds more than one project ({names}); name one explicitly")

    pro_file = next((p for p in pro_files if stem is None or p.stem == stem), None)
    if pro_file is None and pro_files and stem is not None:
        pro_file = pro_files[0]

    schematics = _collect(root, "*.kicad_sch")
    boards = _collect(root, "*.kicad_pcb")
    if pro_file is None and not schematics and not boards:
        raise ProjectError(f"{root} does not look like a KiCad project")

    name = pro_file.stem if pro_file is not None else (stem or root.name)
    board = next((b for b in boards if b.stem == name), boards[0] if boards else None)

    return KiCadProject(
        root=root,
        name=name,
        pro_file=pro_file,
        schematics=schematics,
        board=board,
    )


def _collect(root: Path, pattern: str) -> tuple[Path, ...]:
    """Matching files anywhere under ``root``, skipping backups and libraries.

    Hierarchical sheets usually sit beside the root sheet but are allowed to
    live in a subdirectory, so the search recurses.
    """
    found = []
    for candidate in sorted(root.rglob(pattern)):
        relative = candidate.relative_to(root).parts[:-1]
        if any(_skip_dir(part) for part in relative):
            continue
        found.append(candidate)
    return tuple(found)


def _skip_dir(name: str) -> bool:
    return (
        name.startswith(".")
        or name == LIBRARIES_DIR
        or name.endswith(_SKIP_DIR_SUFFIXES)
    )
