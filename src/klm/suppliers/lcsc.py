"""The LCSC adapter — the cheap, slow, imported supplier (docs/07 §3).

**Q2 is answered, and the answer changes the design.** LCSC now publishes an
official API, but access is granted *per company*: the application asks for a
company website, a business licence and an estimated order volume, and the
terms forbid redistributing the data or the API documentation. A hobbyist
building boards in Poland will not be granted it, and klm's whole audience is
that hobbyist.

So the decision recorded in `docs/adr/0009` is:

* **Manual mode is the default and the primary path**, not a fallback. Paste an
  LCSC part number and the handful of fields visible on the product page, and
  klm is fully functional — ordering, JLCPCB assembly, BOM export — just
  without live stock.
* **klm ships no code against unofficial endpoints.** The community
  `jlcsearch`-style services and the EasyEDA component endpoints work today and
  may not tomorrow, and using them to bulk-pull pricing is squarely what
  LCSC's terms prohibit. Isolating that behind an interface would make it
  *replaceable*, not *permitted*.
* **`mode = "api"` is a seam, not a stub with guessed shapes.** A user who has
  been granted access holds documentation klm's authors have not seen and are
  not permitted to see. They supply a client; klm supplies everywhere it plugs
  in.

What manual mode still does automatically, because none of it needs an API:
part-number validation, product and datasheet URLs, and the packaging suffix
handling every supplier shares.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from klm.config import SupplierConfig
from klm.model import Confidence, Offer, Packaging, PriceBreak
from klm.suppliers.base import SearchHit, SupplierNotConfigured
from klm.suppliers.http import CachedHttp

__all__ = ["LcscAdapter", "is_lcsc_pn", "normalize_lcsc_pn", "product_url"]

PRODUCT_URL = "https://www.lcsc.com/product-detail/{pn}.html"
SEARCH_URL = "https://www.lcsc.com/search?q={query}"

#: `C` followed by digits. LCSC has used nothing else for the part numbers that
#: matter here, and JLCPCB's assembly service accepts nothing else.
_LCSC_PN = re.compile(r"^C\d+$", re.IGNORECASE)


def normalize_lcsc_pn(value: str) -> str:
    return value.strip().upper()


def is_lcsc_pn(value: str) -> bool:
    return bool(_LCSC_PN.match(normalize_lcsc_pn(value)))


def product_url(supplier_pn: str) -> str:
    return PRODUCT_URL.format(pn=normalize_lcsc_pn(supplier_pn))


class LcscAdapter:
    """LCSC in manual mode, or delegating to a user-supplied API client.

    In manual mode every lookup declines rather than raising: LCSC having no
    automated answer is the *expected* state, not a failure, and a refresh
    across a 200-part catalog must not print 200 errors about it.
    """

    def __init__(
        self,
        config: SupplierConfig,
        http: CachedHttp | None = None,
        *,
        client: object | None = None,
    ) -> None:
        self.name = config.name
        self.currency = config.currency
        self.config = config
        self.http = http
        self.client = client
        """An object supplied by a user who has been granted API access."""

    @property
    def manual(self) -> bool:
        return self.config.manual or self.client is None

    def _require_client(self) -> object:
        if self.client is None:
            raise SupplierNotConfigured(
                "LCSC is in manual mode. Its API is granted per company, so klm ships no "
                "client for it — record offers with `klm offers <part> --supplier lcsc "
                "--add C12345`, or set "
                "mode = \"api\" and supply a client if you have been granted access."
            )
        return self.client

    # -- protocol ------------------------------------------------------

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        if self.manual:
            return []
        return list(self._require_client().search(query, limit=limit))  # type: ignore[attr-defined]

    def search_parametric(self, category: str, filters: dict[str, str]) -> list[SearchHit]:
        if self.manual:
            return []
        client = self._require_client()
        return list(client.search_parametric(category, filters))  # type: ignore[attr-defined]

    def get_offer(self, supplier_pn: str) -> Offer | None:
        if self.manual:
            return None
        return self._require_client().get_offer(supplier_pn)  # type: ignore[attr-defined,no-any-return]

    def get_offers(self, supplier_pns: Sequence[str]) -> dict[str, Offer]:
        if self.manual:
            return {}
        return dict(self._require_client().get_offers(supplier_pns))  # type: ignore[attr-defined]

    def resolve_mpn(self, mpn: str, manufacturer: str | None = None) -> list[Offer]:
        if self.manual:
            return []
        return list(self._require_client().resolve_mpn(mpn, manufacturer))  # type: ignore[attr-defined]

    def datasheet_url(self, supplier_pn: str) -> str | None:
        """LCSC's datasheets sit behind the product page, not a stable URL.

        Returning the product page is honest: it is where the datasheet is, and
        it is better than a constructed link that 404s.
        """
        return product_url(supplier_pn) if is_lcsc_pn(supplier_pn) else None

    # -- manual entry --------------------------------------------------

    def manual_offer(
        self,
        supplier_pn: str,
        *,
        mpn: str = "",
        manufacturer: str = "",
        description: str = "",
        stock: int | None = None,
        unit_price: float | None = None,
        moq: int = 1,
        packaging: Packaging = Packaging.UNKNOWN,
    ) -> Offer:
        """Build an offer from what a human read off the product page.

        The confidence is ``high`` because a human matched it, which is the
        most reliable signal in the whole matching pipeline — better than any
        exact string comparison klm could make.
        """
        pn = normalize_lcsc_pn(supplier_pn)
        if not is_lcsc_pn(pn):
            raise ValueError(f"{supplier_pn!r} is not an LCSC part number (expected C12345)")
        return Offer(
            supplier=self.name,
            supplier_pn=pn,
            mpn=mpn.strip() or None,
            manufacturer=manufacturer.strip() or None,
            description=description.strip(),
            packaging=packaging,
            moq=moq,
            stock=stock,
            currency=self.currency,
            price_breaks=[PriceBreak(moq, unit_price)] if unit_price is not None else [],
            url=product_url(pn),
            datasheet_url=product_url(pn),
            match_confidence=Confidence.HIGH,
        )
