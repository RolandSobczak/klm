"""Build the KiCad libraries KiCad actually reads, from the catalog.

``generated/`` is a build artifact: it can be deleted and rebuilt at any time,
and it is what the global library tables point at. Generation is idempotent and
byte-stable, so the desktop app can run it after every edit without churn
(docs/04 §5).

Only ``approved`` parts are generated. A draft is not usable in a design, and
putting it in the library would make it usable by accident.

The building itself lives in :mod:`klm.services.library`, shared with
``klm vendor`` — see the note there on why that sharing is load-bearing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from klm import __version__
from klm.model import Part, PartStatus
from klm.services.catalog import list_parts
from klm.services.library import (
    DEFAULT_FORMAT_VERSION,
    GENERATOR,
    MODEL_ENV_VAR,
    Layout,
    NameMap,
    assign_names,
    build_library,
    write_if_changed,
    write_library,
)
from klm.store.assets import AssetStore
from klm.store.paths import Paths

__all__ = [
    "DEFAULT_FORMAT_VERSION",
    "GENERATOR",
    "MODEL_ENV_VAR",
    "GenerateResult",
    "generate",
    "global_layout",
]

LIBRARY_NAME = "KLM"


def global_layout(paths: Paths) -> Layout:
    """Where the global library lands, and what its paths refer to."""
    return Layout(
        symbol_file=paths.generated_symbols,
        footprint_dir=paths.generated_footprints,
        model_dir=paths.generated_models3d,
        footprint_lib=LIBRARY_NAME,
        model_env_var=MODEL_ENV_VAR,
    )


@dataclass
class GenerateResult:
    symbols: int = 0
    footprints: int = 0
    models: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(klm_id, reason)`` for parts that could not be generated."""
    changed: bool = False
    """False when every output file already held the bytes we would write."""

    @property
    def ok(self) -> bool:
        return not self.skipped


def generate(conn: sqlite3.Connection, paths: Paths) -> GenerateResult:
    """Rebuild ``generated/`` from every approved part."""
    store = AssetStore(paths.assets)
    parts = list_parts(conn, status=PartStatus.APPROVED)
    names = assign_names(parts, store)
    build = build_library(parts, store, global_layout(paths), names)

    changed = write_library(build, store, prune=True)
    changed |= write_if_changed(paths.generated / "manifest.json", _manifest(parts, names))

    return GenerateResult(
        symbols=len(build.symbols()),
        footprints=len(build.footprints()),
        models=len(build.models()),
        skipped=build.skipped,
        changed=changed,
    )


def _manifest(parts: list[Part], names: NameMap) -> str:
    """Record what produced this build, so a library can be traced back."""
    payload = {
        "klm_version": __version__,
        "generator": GENERATOR,
        "parts": [
            {
                "klm_id": part.klm_id,
                "symbol": names.symbols.get(part.klm_id),
                "footprint": names.footprints.get(part.klm_id),
                "symbol_hash": part.symbol_hash,
                "footprint_hash": part.footprint_hash,
                "model3d_hash": part.model3d_hash,
            }
            for part in sorted(parts, key=lambda p: p.klm_id)
        ],
        "models": dict(sorted(names.models.items())),
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
