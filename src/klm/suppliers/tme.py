"""The TME adapter — the domestic supplier (docs/07 §2).

**Q1 is answered.** The original seed note claimed OAuth 2.0. TME's published
scheme is request signing: an application token and secret, with each request
carrying an HMAC-SHA1 signature over an OAuth-1.0a-style base string. The
signature is base64-encoded and sent as the `ApiSignature` parameter. It is not
OAuth — there is no authorization flow, no bearer token and nothing to refresh.
See `docs/adr/0009` and `docs/14` Q1.

The signature base string is::

    POST&<percent-encoded endpoint URL>&<percent-encoded sorted query string>

with parameters sorted by name and percent-encoded twice in effect — once
inside the query string, once when that whole string is embedded in the base.
Getting this wrong yields `E_INVALID_SIGNATURE` and nothing more useful, which
is why :func:`sign` is a pure function with its own tests rather than something
buried in the request path.

TME quirks the adapter absorbs so nothing above it sees them:

* A TME **symbol** is TME's own part identifier, not an MPN, and may carry a
  packaging suffix. Both the symbol and the MPN it maps to are recorded.
* Prices are **net**; VAT is applied for display, at a rate from config.
* Stock, prices and product details are three different endpoints, so one
  logical "get me this offer" is three calls — batched, which is why the
  protocol's :meth:`get_offers` is the primitive here and ``get_offer`` is the
  special case of it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from klm.config import SupplierConfig
from klm.model import Confidence, Offer, Packaging, PriceBreak
from klm.suppliers.base import SearchHit, SupplierNotConfigured, SupplierUnavailable
from klm.suppliers.http import CachedHttp
from klm.suppliers.matching import strip_packaging

__all__ = ["API_ROOT", "TmeAdapter", "sign", "signature_base"]

API_ROOT = "https://api.tme.eu"
DEFAULT_COUNTRY = "PL"
DEFAULT_LANGUAGE = "EN"
#: TME accepts up to this many symbols per call on the batch endpoints.
BATCH_SIZE = 50


# ---------------------------------------------------------------------------
# Request signing
# ---------------------------------------------------------------------------


def _quote(value: str) -> str:
    """Percent-encode per RFC 3986 — nothing unreserved is escaped, `/` is."""
    return urllib.parse.quote(value, safe="-._~")


def _flatten(params: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested parameters into TME's `Name[0]`, `Name[Key]` form.

    Lists become indexed keys because that is how TME reads repeated values;
    the signature is computed over the flattened form, so this must happen
    before sorting, not after.
    """
    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, Mapping):
            flat.extend(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                flat.append((f"{name}[{index}]", str(item)))
        else:
            flat.append((name, str(value)))
    return flat


def signature_base(url: str, params: Mapping[str, Any], method: str = "POST") -> str:
    """The string that gets signed.

    Exposed because a signature bug is otherwise diagnosable only as
    `E_INVALID_SIGNATURE`, and comparing two base strings is the fastest way
    to find one.
    """
    pairs = sorted(_flatten(params), key=lambda pair: pair[0])
    query = "&".join(f"{_quote(key)}={_quote(value)}" for key, value in pairs)
    return f"{method.upper()}&{_quote(url)}&{_quote(query)}"


def sign(url: str, params: Mapping[str, Any], secret: str, method: str = "POST") -> str:
    """HMAC-SHA1 over :func:`signature_base`, base64-encoded."""
    digest = hmac.new(
        secret.encode("utf-8"), signature_base(url, params, method).encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class TmeAdapter:
    """TME, via its signed REST API."""

    def __init__(
        self,
        config: SupplierConfig,
        http: CachedHttp,
        *,
        country: str = DEFAULT_COUNTRY,
        language: str = DEFAULT_LANGUAGE,
        ttl: float | None = None,
    ) -> None:
        self.name = config.name
        self.currency = config.currency
        self.config = config
        self.http = http
        self.country = country
        self.language = language
        self.ttl = ttl

    # -- plumbing ------------------------------------------------------

    def _call(self, endpoint: str, params: dict[str, Any], *, ttl: float | None = None) -> Any:
        token, secret = self.config.credentials()
        if not token or not secret:
            raise SupplierNotConfigured(
                "TME needs an application token and secret. Register at "
                "developers.tme.eu, then set TME_API_KEY and TME_API_SECRET."
            )

        url = f"{API_ROOT}/{endpoint}.json"
        payload: dict[str, Any] = {
            **params,
            "Token": token,
            "Country": self.country,
            "Language": self.language,
        }
        payload["ApiSignature"] = sign(url, payload, secret)

        body = urllib.parse.urlencode(_flatten(payload)).encode("utf-8")
        response = self.http.request(
            "POST",
            url,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            ttl=self.ttl if ttl is None else ttl,
        )
        try:
            data = json.loads(response.body)
        except ValueError as exc:
            raise SupplierUnavailable(f"tme: {endpoint} returned unparseable JSON") from exc

        if not response.ok or data.get("Status") != "OK":
            status = data.get("Status", response.status)
            # The signature error is worth naming: it is the one failure whose
            # cause is klm's code rather than the network or the credentials.
            hint = (
                " — check the signature base string"
                if status == "E_INVALID_SIGNATURE"
                else ""
            )
            raise SupplierUnavailable(f"tme: {endpoint} failed with {status}{hint}")
        return data.get("Data", {})

    # -- protocol ------------------------------------------------------

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        data = self._call("Products/Search", {"SearchPlain": query, "SearchWithStock": "false"})
        products = data.get("ProductList", []) if isinstance(data, dict) else []
        return [self._hit(product) for product in products[:limit]]

    def search_parametric(self, category: str, filters: dict[str, str]) -> list[SearchHit]:
        params: dict[str, Any] = {"CategoryId": category}
        if filters:
            params["Parameters"] = dict(filters)
        data = self._call("Products/Search", params)
        products = data.get("ProductList", []) if isinstance(data, dict) else []
        return [self._hit(product) for product in products]

    def get_offer(self, supplier_pn: str) -> Offer | None:
        return self.get_offers([supplier_pn]).get(supplier_pn)

    def get_offers(self, supplier_pns: Sequence[str]) -> dict[str, Offer]:
        offers: dict[str, Offer] = {}
        for chunk in _chunks(supplier_pns, BATCH_SIZE):
            products = self._products(chunk)
            prices = self._prices(chunk)
            for symbol, product in products.items():
                offers[symbol] = self._offer(product, prices.get(symbol, {}))
        return offers

    def resolve_mpn(self, mpn: str, manufacturer: str | None = None) -> list[Offer]:
        """Search by MPN, then price the symbols that came back.

        TME's search matches on MPN text, so this is a search followed by a
        batch price call rather than a dedicated endpoint.
        """
        hits = self.search(mpn, limit=BATCH_SIZE)
        if manufacturer:
            narrowed = [h for h in hits if h.manufacturer and h.manufacturer == manufacturer]
            hits = narrowed or hits
        if not hits:
            return []
        return list(self.get_offers([hit.supplier_pn for hit in hits]).values())

    def datasheet_url(self, supplier_pn: str) -> str | None:
        data = self._call("Products/GetProductsFiles", {"SymbolList": [supplier_pn]})
        files = data.get("ProductList", []) if isinstance(data, dict) else []
        for product in files:
            for document in product.get("Files", {}).get("DocumentList", []):
                if document.get("DocumentType") == "DTE":
                    return _absolute(str(document.get("DocumentUrl", ""))) or None
        return None

    # -- endpoint wrappers ---------------------------------------------

    def _products(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        data = self._call("Products/GetProducts", {"SymbolList": list(symbols)})
        products = data.get("ProductList", []) if isinstance(data, dict) else []
        return {str(p.get("Symbol", "")): p for p in products if p.get("Symbol")}

    def _prices(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        # Prices are the volatile half of an offer, so they get a much shorter
        # cache life than the product record, which changes almost never.
        data = self._call(
            "Products/GetPricesAndStocks", {"SymbolList": list(symbols)}, ttl=_PRICE_TTL
        )
        products = data.get("ProductList", []) if isinstance(data, dict) else []
        return {str(p.get("Symbol", "")): p for p in products if p.get("Symbol")}

    # -- normalisation -------------------------------------------------

    def _hit(self, product: Mapping[str, Any]) -> SearchHit:
        return SearchHit(
            supplier=self.name,
            supplier_pn=str(product.get("Symbol", "")),
            mpn=str(product.get("OriginalSymbol") or product.get("Symbol", "")),
            manufacturer=str(product.get("Producer", "")),
            description=str(product.get("Description", "")),
            stock=_int(product.get("InStock")),
            url=_absolute(str(product.get("ProductInformationPage", ""))),
        )

    def _offer(self, product: Mapping[str, Any], price: Mapping[str, Any]) -> Offer:
        symbol = str(product.get("Symbol", ""))
        mpn = str(product.get("OriginalSymbol") or symbol)
        _, packaging = strip_packaging(symbol)
        return Offer(
            supplier=self.name,
            supplier_pn=symbol,
            mpn=mpn,
            manufacturer=str(product.get("Producer", "")),
            description=str(product.get("Description", "")),
            packaging=packaging or Packaging.UNKNOWN,
            moq=_int(price.get("Unit")) or _int(product.get("MinAmount")),
            multiple=_int(product.get("Multiples")),
            stock=_int(price.get("Amount")) or _int(product.get("InStock")),
            currency=str(price.get("PriceCurrency") or self.currency),
            price_breaks=_breaks(price.get("PriceList", [])),
            url=_absolute(str(product.get("ProductInformationPage", ""))),
            match_confidence=Confidence.HIGH,
        )


#: Product records are effectively static; prices and stock are not.
_PRICE_TTL = 3600.0


def _breaks(rows: Iterable[Any]) -> list[PriceBreak]:
    """TME's price ladder, net of VAT.

    Net is stored and gross displayed (docs/07 §2) because the VAT rate is a
    property of the buyer, not of the price, and baking it in makes the stored
    number wrong the moment the rate changes.
    """
    breaks: list[PriceBreak] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        qty = _int(row.get("Amount"))
        price = row.get("PriceValue")
        if qty and price is not None:
            breaks.append(PriceBreak(qty, float(price)))
    return breaks


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _absolute(url: str) -> str | None:
    """TME returns protocol-relative URLs; a bare `//…` is not clickable."""
    if not url:
        return None
    return f"https:{url}" if url.startswith("//") else url


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
