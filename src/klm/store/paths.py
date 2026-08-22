"""Where klm keeps its data.

One object resolves the whole layout so that nothing else has to guess at a
directory name. See docs/03-architecture.md §4.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GITIGNORE", "Paths", "resolve_home"]

#: What must not go into a shared catalog repository. `catalog/` and `assets/`
#: are what a sync is *for*, so everything else here is either derived from them
#: or local to this machine.
GITIGNORE = """\
# klm — what is worth sharing is catalog/ and assets/, and nothing else here.
catalog.db
catalog.db-wal
catalog.db-shm
generated/
cache/

# Names the environment variables holding supplier credentials, and is
# machine-local anyway.
config.toml
"""


def resolve_home(explicit: str | Path | None = None) -> Path:
    """Resolve the klm data directory.

    Precedence, first match wins:

    1. ``explicit`` — a ``--catalog`` flag
    2. ``KLM_HOME``
    3. ``XDG_DATA_HOME/klm`` (or ``APPDATA/klm`` on Windows)
    4. ``~/.local/share/klm``

    The path is expanded and made absolute but not created; creation is
    :meth:`Paths.create`, so that read-only commands never bring a catalog into
    existence as a side effect of being run in the wrong directory.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    env = os.environ.get("KLM_HOME")
    if env:
        return Path(env).expanduser().resolve()

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata).expanduser().resolve() / "klm"

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser().resolve() / "klm"

    return Path.home().resolve() / ".local" / "share" / "klm"


@dataclass(frozen=True)
class Paths:
    """The klm directory layout, derived from a single root."""

    home: Path

    @classmethod
    def resolve(cls, explicit: str | Path | None = None) -> Paths:
        return cls(resolve_home(explicit))

    # -- operational store ---------------------------------------------

    @property
    def db(self) -> Path:
        return self.home / "catalog.db"

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    # -- git-versioned mirror ------------------------------------------

    @property
    def catalog(self) -> Path:
        """One directory per part, exported deterministically for git."""
        return self.home / "catalog"

    def part_dir(self, klm_id: str) -> Path:
        return self.catalog / klm_id

    # -- content-addressed assets --------------------------------------

    @property
    def assets(self) -> Path:
        return self.home / "assets"

    @property
    def symbols(self) -> Path:
        return self.assets / "symbols"

    @property
    def footprints(self) -> Path:
        return self.assets / "footprints"

    @property
    def models3d(self) -> Path:
        return self.assets / "models3d"

    # -- regenerable output --------------------------------------------

    @property
    def generated(self) -> Path:
        """Build output. Safe to delete; rebuilt by ``klm generate``."""
        return self.home / "generated"

    @property
    def generated_symbols(self) -> Path:
        return self.generated / "KLM.kicad_sym"

    @property
    def generated_footprints(self) -> Path:
        return self.generated / "KLM.pretty"

    @property
    def generated_models3d(self) -> Path:
        return self.generated / "packages3d"

    # -- caches --------------------------------------------------------

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def supplier_cache(self) -> Path:
        return self.cache / "suppliers"

    @property
    def datasheet_cache(self) -> Path:
        return self.cache / "datasheets"

    # -- lifecycle -----------------------------------------------------

    def all_dirs(self) -> list[Path]:
        return [
            self.home,
            self.catalog,
            self.assets,
            self.symbols,
            self.footprints,
            self.models3d,
            self.generated,
            self.cache,
            self.supplier_cache,
            self.datasheet_cache,
        ]

    def create(self) -> None:
        """Create every directory, and the ignore file. Idempotent."""
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)
        self.write_gitignore()

    def write_gitignore(self) -> bool:
        """Make the catalog safe to `git add .`. Returns True if it wrote.

        Sharing a catalog between machines means putting `catalog/` and
        `assets/` in a repository, and the natural way to do that adds
        everything beside them: a binary database nothing can merge, a build
        directory that is regenerated anyway, a cache of datasheet PDFs, and a
        config file naming the environment variables that hold credentials.
        An existing file is left alone — it is the user's.
        """
        target = self.home / ".gitignore"
        if target.exists():
            return False
        target.write_text(GITIGNORE, encoding="utf-8", newline="\n")
        return True

    def exists(self) -> bool:
        """True if this looks like an initialised catalog."""
        return self.db.exists()
