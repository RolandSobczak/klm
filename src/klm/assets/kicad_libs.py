"""Finding and reusing KiCad's own libraries (docs/08 §1, source priority 2).

These are the best source klm has that it did not write. They are IPC-compliant,
reviewed by more people than klm will ever have, and — the part that matters for
a project headed to GitHub — permissively licensed with an explicit exception
covering use in designs. That is why they sit above both template generation and
any import in the priority order.

Discovery, never hardcoding. KiCad's data path is version-pinned the same way
its config path is, and the environment variables it sets carry the major
version in the name (`KICAD9_SYMBOL_DIR`). klm looks for whichever is present
rather than asserting a version it has not seen (docs/14 Q5).

Absence is a degradation, not a failure: with no KiCad installed, `find_symbol`
and `find_footprint` return nothing and the pipeline falls through to the
templates.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from klm.kicad import footprints as fp
from klm.kicad import symbols as sym
from klm.kicad.sexpr import SExp, loads

__all__ = [
    "KicadLibraries",
    "find_libraries",
    "symbol_library_name",
]

_SYMBOL_ENV = re.compile(r"^KICAD(\d+)_SYMBOL_DIR$")
_FOOTPRINT_ENV = re.compile(r"^KICAD(\d+)_FOOTPRINT_DIR$")

#: Where distributions and installers put the shared data. Searched in order,
#: and every hit is kept — a machine can legitimately have two KiCad versions.
_DATA_ROOTS = (
    Path("/usr/share/kicad"),
    Path("/usr/local/share/kicad"),
    Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport"),
    Path("C:/Program Files/KiCad"),
)


def symbol_library_name(path: Path) -> str:
    """`Device.kicad_sym` → `Device`, the name KiCad's tables use."""
    return path.stem


def _versioned_env(pattern: re.Pattern[str]) -> list[Path]:
    """Directories named by `KICAD<N>_..._DIR`, newest major version first."""
    found: list[tuple[int, Path]] = []
    for key, value in os.environ.items():
        match = pattern.match(key)
        if match and value:
            path = Path(value).expanduser()
            if path.is_dir():
                found.append((int(match.group(1)), path))
    return [path for _version, path in sorted(found, reverse=True)]


def _scan_roots(subdirectory: str) -> list[Path]:
    directories: list[Path] = []
    for root in _DATA_ROOTS:
        if not root.is_dir():
            continue
        candidate = root / subdirectory
        if candidate.is_dir():
            directories.append(candidate)
            continue
        # Windows nests by version: C:/Program Files/KiCad/9.0/share/kicad/…
        for versioned in sorted(root.glob("*/share/kicad"), reverse=True):
            nested = versioned / subdirectory
            if nested.is_dir():
                directories.append(nested)
    return directories


@dataclass(frozen=True)
class KicadLibraries:
    """The stock library directories klm found, if any."""

    symbol_dirs: tuple[Path, ...] = ()
    footprint_dirs: tuple[Path, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.symbol_dirs or self.footprint_dirs)

    # -- symbols -------------------------------------------------------

    def symbol_libraries(self) -> dict[str, Path]:
        """Library name → file, first directory winning on a collision."""
        libraries: dict[str, Path] = {}
        for directory in self.symbol_dirs:
            for path in sorted(directory.glob("*.kicad_sym")):
                libraries.setdefault(symbol_library_name(path), path)
        return libraries

    def find_symbol(self, lib_id: str) -> SExp | None:
        """Fetch `Library:Symbol` as a detached symbol node, or ``None``.

        The node is a *copy* in the sense that matters — it is freshly parsed
        from disk, so a caller may rename it and rewrite its fields without
        anything else observing the change.
        """
        library, _, name = lib_id.partition(":")
        if not name:
            return None
        path = self.symbol_libraries().get(library)
        if path is None:
            return None
        try:
            document = loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        for symbol in sym.extract_symbols(document):
            if sym.symbol_name(symbol) == name:
                return symbol
        return None

    # -- footprints ----------------------------------------------------

    def footprint_libraries(self) -> dict[str, Path]:
        """Library name → `.pretty` directory."""
        libraries: dict[str, Path] = {}
        for directory in self.footprint_dirs:
            for path in sorted(directory.glob("*.pretty")):
                if path.is_dir():
                    libraries.setdefault(path.stem, path)
        return libraries

    def find_footprint(self, lib_id: str) -> SExp | None:
        """Fetch `Library:Footprint` as a footprint node, or ``None``."""
        library, _, name = lib_id.partition(":")
        if not name:
            return None
        directory = self.footprint_libraries().get(library)
        if directory is None:
            return None
        path = directory / f"{name}.kicad_mod"
        if not path.is_file():
            return None
        try:
            document = loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return document.root if fp.footprint_name(document) is not None else None


def find_libraries(*, extra: tuple[Path, ...] = ()) -> KicadLibraries:
    """Locate KiCad's stock symbol and footprint directories.

    ``extra`` prepends caller-supplied directories, which is how tests point
    this at a fixture and how a user with an unusual install points it at their
    own. Explicit beats discovered, always.
    """
    symbol_dirs: list[Path] = [p for p in extra if p.is_dir()]
    footprint_dirs: list[Path] = [p for p in extra if p.is_dir()]

    symbol_dirs.extend(_versioned_env(_SYMBOL_ENV))
    footprint_dirs.extend(_versioned_env(_FOOTPRINT_ENV))
    symbol_dirs.extend(_scan_roots("symbols"))
    footprint_dirs.extend(_scan_roots("footprints"))

    return KicadLibraries(
        symbol_dirs=tuple(dict.fromkeys(symbol_dirs)),
        footprint_dirs=tuple(dict.fromkeys(footprint_dirs)),
    )


@lru_cache(maxsize=1)
def default_libraries() -> KicadLibraries:
    """The discovered libraries, cached — scanning is filesystem work."""
    return find_libraries()
