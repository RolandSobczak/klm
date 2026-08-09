"""The supplier adapter protocol (docs/07 §1).

Services never talk to a supplier-specific type. Every adapter returns the
normalized :class:`~klm.model.Offer`, and every supplier-specific quirk — TME
symbols that are not MPNs, LCSC's `C…` numbers, net-versus-gross pricing — is
absorbed inside the adapter.

The protocol is deliberately small. Anything an adapter cannot do it declines
by returning nothing, rather than by raising: a supplier that has no parametric
search is a supplier with fewer features, not a broken one. Raising is reserved
for a supplier that is *configured* and *failing*, which is a different thing
the caller must be able to distinguish.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from klm.model import Offer

__all__ = [
    "SearchHit",
    "SupplierAdapter",
    "SupplierError",
    "SupplierNotConfigured",
    "SupplierUnavailable",
]


class SupplierError(Exception):
    """A supplier operation failed."""


class SupplierNotConfigured(SupplierError):
    """Credentials or a mode the adapter needs are absent.

    Separate from :class:`SupplierUnavailable` because the remedy is different:
    one is "add your API key", the other is "wait, or work offline".
    """


class SupplierUnavailable(SupplierError):
    """The supplier is configured but unreachable, rate-limited or degraded.

    klm always works offline in degraded mode (docs/07 §4), so callers catch
    this and fall back to cached offers with a visible staleness marker. Only
    operations that genuinely require fresh data — committing to an order —
    let it propagate.
    """


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A candidate from a supplier's search, before it is matched to a part.

    Not an ``Offer``: a hit is something the supplier showed us, and turning it
    into an offer means deciding it is the *same part*, which is
    :mod:`klm.suppliers.matching`'s job and carries a confidence.
    """

    supplier: str
    supplier_pn: str
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    package: str | None = None
    stock: int | None = None
    url: str | None = None
    datasheet_url: str | None = None


@runtime_checkable
class SupplierAdapter(Protocol):
    """What every supplier must implement."""

    name: str
    """``tme`` | ``lcsc`` — the key used in config, the database and the CLI."""
    currency: str
    """The supplier's native billing currency. Never silently converted."""

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        """Free-text search. Empty list when the supplier has nothing."""
        ...

    def search_parametric(self, category: str, filters: dict[str, str]) -> list[SearchHit]:
        """Category-and-filters search, or an empty list where unsupported."""
        ...

    def get_offer(self, supplier_pn: str) -> Offer | None:
        """One offer by the supplier's own part number."""
        ...

    def get_offers(self, supplier_pns: Sequence[str]) -> dict[str, Offer]:
        """Batched :meth:`get_offer`.

        Batched deliberately: refreshing a 200-line BOM as 200 individual calls
        is both slow and rude to the API. Adapters without a batch endpoint
        implement this as a rate-limited loop, which keeps the rudeness inside
        the adapter instead of spreading it through the services.
        """
        ...

    def resolve_mpn(self, mpn: str, manufacturer: str | None = None) -> list[Offer]:
        """Every offer this supplier has for a manufacturer part number."""
        ...

    def datasheet_url(self, supplier_pn: str) -> str | None:
        """The supplier's datasheet link, if it publishes one."""
        ...
