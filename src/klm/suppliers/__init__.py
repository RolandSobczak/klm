"""Supplier adapters. See docs/07-supplier-integration.md."""

from klm.suppliers.base import (
    SearchHit,
    SupplierAdapter,
    SupplierError,
    SupplierNotConfigured,
    SupplierUnavailable,
)
from klm.suppliers.matching import MatchResult, match_mpn
from klm.suppliers.registry import build_adapter, build_adapters

__all__ = [
    "MatchResult",
    "SearchHit",
    "SupplierAdapter",
    "SupplierError",
    "SupplierNotConfigured",
    "SupplierUnavailable",
    "build_adapter",
    "build_adapters",
    "match_mpn",
]
