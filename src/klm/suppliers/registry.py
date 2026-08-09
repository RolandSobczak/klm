"""Building the configured adapters, so callers never name a supplier class."""

from __future__ import annotations

from pathlib import Path

from klm.config import Config, SupplierConfig
from klm.suppliers.base import SupplierAdapter
from klm.suppliers.http import CachedHttp, TokenBucket
from klm.suppliers.lcsc import LcscAdapter
from klm.suppliers.tme import TmeAdapter

__all__ = ["BUILDERS", "build_adapter", "build_adapters"]


def _tme(config: SupplierConfig, http: CachedHttp) -> SupplierAdapter:
    return TmeAdapter(config, http)


def _lcsc(config: SupplierConfig, http: CachedHttp) -> SupplierAdapter:
    return LcscAdapter(config, http)


#: Supplier name → constructor. Adding a supplier is a row here plus a module.
BUILDERS = {"tme": _tme, "lcsc": _lcsc}


def build_adapter(
    config: SupplierConfig, cache_dir: Path, *, offline: bool = False
) -> SupplierAdapter | None:
    """One adapter, or ``None`` for a supplier klm has no implementation for."""
    builder = BUILDERS.get(config.name)
    if builder is None:
        return None
    http = CachedHttp(
        config.name,
        cache_dir,
        bucket=TokenBucket(rate=config.rate_per_second, capacity=config.burst),
        offline=offline,
    )
    return builder(config, http)


def build_adapters(
    config: Config, cache_dir: Path, *, offline: bool = False, only: str | None = None
) -> dict[str, SupplierAdapter]:
    """Every enabled supplier klm can talk to, keyed by name."""
    adapters: dict[str, SupplierAdapter] = {}
    for supplier in config.enabled_suppliers():
        if only is not None and supplier.name != only:
            continue
        adapter = build_adapter(supplier, cache_dir, offline=offline)
        if adapter is not None:
            adapters[supplier.name] = adapter
    return adapters
