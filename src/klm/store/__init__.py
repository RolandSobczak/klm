"""Storage: filesystem layout, the content-addressed asset store, and the catalog database.

This layer knows where bytes live and how to read and write them. It must not
know what a footprint *is* — that judgement belongs to the services above it.
"""

from klm.store.assets import AssetKind, AssetStore
from klm.store.db import SCHEMA_VERSION, connect, migrate
from klm.store.paths import Paths, resolve_home

__all__ = [
    "SCHEMA_VERSION",
    "AssetKind",
    "AssetStore",
    "Paths",
    "connect",
    "migrate",
    "resolve_home",
]
