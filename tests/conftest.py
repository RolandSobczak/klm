"""Fixtures shared across the suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from klm.store.assets import AssetStore
from klm.store.db import connect, migrate
from klm.store.paths import Paths

Env = tuple[Paths, "object", AssetStore]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[Paths, object, AssetStore]]:
    """A migrated catalog in a throwaway directory, plus its asset store."""
    paths = Paths(tmp_path / "home")
    paths.create()
    conn = connect(paths.db)
    migrate(conn)
    yield paths, conn, AssetStore(paths.assets)
    conn.close()
