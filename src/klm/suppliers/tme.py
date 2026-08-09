"""The TME adapter — the domestic supplier (docs/07 §2), on the **v2** API.

klm targeted v1 until 2026-08-09: `POST /Products/Action.json` with an
HMAC-SHA1 signature over an OAuth-1.0a-style base string. That is the scheme
`docs/14` Q1 documented, and it still answers — but TME's own repository calls
it deprecated, and v2 is a different API in every respect that matters:

* **OAuth 2.0 client credentials.** `POST /auth/token` with
  `Authorization: Basic base64(token:secret)` and `grant_type=client_credentials`
  returns a bearer token that **expires in 300 seconds**, plus a refresh token.
  Five minutes is short enough that a single `klm refresh` over a few hundred
  parts outlives it, so refreshing mid-run is the normal path, not the edge case.
* **REST, and GET.** `/products/search`, `/products/data`, `/products` and
  friends, with query parameters instead of a signed form body.
* **Parametric search that works** — and that filters by *numeric identifiers*,
  never by names or comparisons (`docs/14` Q10). Resolving a human constraint to
  those identifiers is :mod:`klm.suppliers.constraints`, deliberately outside
  this module: it is pure, and it is what keeps the research agent from ever
  handling an ID it could get plausibly wrong.

Migrated before anyone depends on v1, which is the cheap moment to do it.

Two v2 details worth stating because getting either wrong is silent:

* **Prices carry their own `type`, `NET` or `GROSS`.** v1 returned net and klm
  applied VAT for display from config. v2 says which it gave you, so klm reads
  it and converts — applying a configured rate to a gross price would inflate
  every landed-cost comparison by the whole rate, which is the decision those
  numbers exist to inform.
* **`/products` takes `mpns[]` as well as `symbols[]`.** A real MPN lookup,
  where v1 offered only a text search that happened to match MPNs.

TME quirks the adapter still absorbs so nothing above it sees them:

* A TME **symbol** is TME's own part identifier, not an MPN, and may carry a
  packaging suffix. Both the symbol and the MPN it maps to are recorded.
* Stock, prices and product details are two endpoints rather than three, so one
  logical "get me this offer" is still batched — which is why the protocol's
  :meth:`TmeAdapter.get_offers` is the primitive and ``get_offer`` the special
  case of it.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from klm.config import SupplierConfig
from klm.model import Confidence, Offer, Packaging, PriceBreak
from klm.suppliers.base import SearchHit, SupplierNotConfigured, SupplierUnavailable
from klm.suppliers.constraints import ParameterValue, Resolution, SupplierParameter, resolve
from klm.suppliers.http import CachedHttp
from klm.suppliers.matching import strip_packaging

__all__ = [
    "API_ROOT",
    "BATCH_SIZE",
    "TmeAdapter",
    "TokenCache",
]

API_ROOT = "https://api.tme.eu"
DEFAULT_COUNTRY = "PL"
DEFAULT_LANGUAGE = "EN"
#: TME accepts up to this many symbols per call on the batch endpoints.
BATCH_SIZE = 50

#: Product records are effectively static; prices and stock are not.
_PRICE_TTL = 3600.0
_STATIC_TTL = 86400.0

#: Renew this long before the token actually expires. A request that starts
#: valid and arrives expired is indistinguishable from bad credentials, and the
#: cost of being early is one extra call every five minutes.
_TOKEN_MARGIN = 30.0


class TokenCache:
    """Holds an access token and knows when it has stopped being usable.

    Separate from the adapter so the expiry logic can be tested without a clock
    that really takes five minutes.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: float = 0.0

    @property
    def valid(self) -> bool:
        return bool(self.access_token) and self.clock() < self.expires_at - _TOKEN_MARGIN

    def store(self, payload: Mapping[str, Any]) -> None:
        self.access_token = str(payload.get("access_token") or "") or None
        refreshed = payload.get("refresh_token")
        if refreshed:
            self.refresh_token = str(refreshed)
        try:
            lifetime = float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0.0
        self.expires_at = self.clock() + lifetime

    def clear(self) -> None:
        self.access_token = None
        self.expires_at = 0.0


class TmeAdapter:
    """TME, via its v2 REST API."""

    def __init__(
        self,
        config: SupplierConfig,
        http: CachedHttp,
        *,
        country: str = DEFAULT_COUNTRY,
        language: str = DEFAULT_LANGUAGE,
        ttl: float | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = config.name
        self.currency = config.currency
        self.config = config
        self.http = http
        self.country = country
        self.language = language
        self.ttl = ttl
        self.tokens = TokenCache(clock)

    # -- authentication ------------------------------------------------

    def _authenticate(self) -> str:
        """Return a usable bearer token, obtaining or refreshing one if needed."""
        if self.tokens.valid:
            assert self.tokens.access_token is not None
            return self.tokens.access_token

        token, secret = self.config.credentials()
        if not token or not secret:
            raise SupplierNotConfigured(
                "TME needs an application token and secret. Register at "
                "developers.tme.eu, then set TME_API_KEY and TME_API_SECRET."
            )

        grant: dict[str, str] = {"grant_type": "client_credentials"}
        if self.tokens.refresh_token:
            grant = {
                "grant_type": "refresh_token",
                "refresh_token": self.tokens.refresh_token,
            }

        basic = base64.b64encode(f"{token}:{secret}".encode()).decode("ascii")
        response = self.http.request(
            "POST",
            f"{API_ROOT}/auth/token",
            body=urllib.parse.urlencode(grant).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            # Never cached. A cached token is one that outlives its own expiry,
            # and the failure looks like a credentials problem.
            ttl=None,
        )
        if not response.ok:
            if grant["grant_type"] == "refresh_token":
                # The refresh token has gone stale too. Fall back to a fresh
                # client-credentials grant rather than reporting a dead session.
                self.tokens.refresh_token = None
                return self._authenticate()
            raise SupplierUnavailable(
                f"tme: authentication failed with {response.status} — "
                "check TME_API_KEY and TME_API_SECRET"
            )

        try:
            payload = json.loads(response.body)
        except ValueError as exc:
            raise SupplierUnavailable("tme: /auth/token returned unparseable JSON") from exc

        self.tokens.store(payload)
        if not self.tokens.access_token:
            raise SupplierUnavailable("tme: /auth/token returned no access_token")
        return self.tokens.access_token

    # -- plumbing ------------------------------------------------------

    def _get(self, path: str, params: Mapping[str, Any], *, ttl: float | None = None) -> Any:
        """One GET, authenticated, with a single retry after a 401.

        The retry exists because the token lifetime is five minutes: a long run
        *will* cross an expiry, and the alternative to retrying is failing a
        refresh halfway through for a reason the user cannot act on.
        """
        query = urllib.parse.urlencode(
            [*_flatten({"country": self.country, "language": self.language, **params})],
        )
        url = f"{API_ROOT}{path}?{query}"

        for attempt in (1, 2):
            bearer = self._authenticate()
            response = self.http.request(
                "GET",
                url,
                headers={"Authorization": f"Bearer {bearer}"},
                ttl=self.ttl if ttl is None else ttl,
            )
            if response.status == 401 and attempt == 1:
                self.tokens.clear()
                continue
            break

        try:
            data = json.loads(response.body)
        except ValueError as exc:
            raise SupplierUnavailable(f"tme: {path} returned unparseable JSON") from exc

        if not response.ok or data.get("status") != "OK":
            status = data.get("status", response.status)
            raise SupplierUnavailable(f"tme: {path} failed with {status}")
        return data.get("data", {})

    # -- protocol ------------------------------------------------------

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        data = self._get(
            "/products/search",
            {"phrase": query, "scope": ["products"], "limit": min(limit, 100)},
        )
        return [self._hit(p) for p in _elements(data.get("products"))[:limit]]

    def search_parametric(self, category: str, filters: dict[str, str]) -> list[SearchHit]:
        """Filter a category by human-stated constraints.

        `filters` are what a person would write — `{"Vin max": ">=18V"}` — never
        TME's numeric identifiers. Those are discovered here and resolved by
        :func:`klm.suppliers.constraints.resolve`, which is the whole reason a
        model can drive this without being able to invent one.

        A constraint that cannot be mapped is **left out of the request and
        reported**, never silently ignored: ignoring one widens the search and
        then returns the results as though they had been filtered. Callers that
        need to know use :meth:`search_parametric_report`.
        """
        return self.search_parametric_report(category, filters)[0]

    def search_parametric_report(
        self, category: str, filters: dict[str, str], *, limit: int = 50
    ) -> tuple[list[SearchHit], Resolution]:
        """:meth:`search_parametric`, plus what happened to each constraint."""
        available = self.category_parameters(category)
        resolution = resolve(available, filters)

        params: dict[str, Any] = {
            "category_id": category,
            "scope": ["products"],
            "limit": min(limit, 100),
        }
        for index, group in enumerate(resolution.groups):
            params[f"parameters[{index}][id]"] = group.parameter_id
            params[f"parameters[{index}][values]"] = list(group.value_ids)

        data = self._get("/products/search", params)
        return [self._hit(p) for p in _elements(data.get("products"))], resolution

    def category_parameters(self, category: str) -> list[SupplierParameter]:
        """The parameters a category can be filtered on, with their value IDs.

        This is `scope[]=parameters` — TME returns the filters that apply to the
        *result set*, which for a bare category query is the category itself.
        """
        data = self._get(
            "/products/search",
            {"category_id": category, "scope": ["parameters"], "limit": 1},
            ttl=_STATIC_TTL,
        )
        return [
            SupplierParameter(
                parameter_id=str(entry.get("id", "")),
                name=str(entry.get("name", "")),
                values=tuple(
                    ParameterValue(str(v.get("id", "")), str(v.get("value", "")))
                    for v in entry.get("values", [])
                    if isinstance(v, Mapping)
                ),
            )
            for entry in _elements(data.get("parameters"))
            if entry.get("id") is not None
        ]

    def categories(self) -> list[dict[str, Any]]:
        """The category tree, flattened to `{id, name, path, products_count}`.

        Flattened because every caller wants to *find* a category by name, and
        none of them wants to walk a tree to do it.
        """
        data = self._get("/products/categories/tree", {}, ttl=_STATIC_TTL)
        flat: list[dict[str, Any]] = []

        def walk(node: Mapping[str, Any], trail: list[str]) -> None:
            name = str(node.get("name", ""))
            path = [*trail, name] if name else trail
            if node.get("id") is not None:
                flat.append(
                    {
                        "id": str(node["id"]),
                        "name": name,
                        "path": "/".join(path),
                        "products_count": _int(node.get("products_count")) or 0,
                    }
                )
            for child in node.get("children", []) or []:
                if isinstance(child, Mapping):
                    walk(child, path)

        root = data.get("elements")
        for node in root if isinstance(root, list) else [root]:
            if isinstance(node, Mapping):
                walk(node, [])
        return flat

    def get_offer(self, supplier_pn: str) -> Offer | None:
        return self.get_offers([supplier_pn]).get(supplier_pn)

    def get_offers(self, supplier_pns: Sequence[str]) -> dict[str, Offer]:
        offers: dict[str, Offer] = {}
        for chunk in _chunks(supplier_pns, BATCH_SIZE):
            products = self._products(chunk)
            pricing = self._pricing(chunk)
            for symbol, product in products.items():
                offers[symbol] = self._offer(product, pricing.get(symbol, {}))
        return offers

    def resolve_mpn(self, mpn: str, manufacturer: str | None = None) -> list[Offer]:
        """Look an MPN up directly — a v2 endpoint, not a text search.

        v1 had no MPN lookup, so this was a search whose hits happened to match.
        `/products?mpns[]=` asks the question klm actually has, and falls back
        to search only when the direct lookup finds nothing.
        """
        symbols: list[str] = []
        try:
            data = self._get("/products", {"mpns": [mpn]}, ttl=_STATIC_TTL)
            symbols = [
                str(p.get("symbol"))
                for p in _elements(data.get("elements") or data)
                if p.get("symbol")
                and (not manufacturer or _manufacturer(p) == manufacturer)
            ]
        except SupplierUnavailable:
            # An MPN TME does not carry is a normal answer, and on some builds
            # it is a non-OK status rather than an empty list.
            symbols = []

        if not symbols:
            hits = self.search(mpn, limit=BATCH_SIZE)
            if manufacturer:
                narrowed = [h for h in hits if h.manufacturer and h.manufacturer == manufacturer]
                hits = narrowed or hits
            symbols = [hit.supplier_pn for hit in hits]

        if not symbols:
            return []
        return list(self.get_offers(symbols[:BATCH_SIZE]).values())

    def datasheet_url(self, supplier_pn: str) -> str | None:
        data = self._get("/products/files", {"symbols": [supplier_pn]}, ttl=_STATIC_TTL)
        for product in _elements(data.get("elements") or data):
            documents = product.get("documents") or {}
            for document in _elements(documents):
                if str(document.get("type", "")).upper() in _DATASHEET_TYPES:
                    return _absolute(str(document.get("url", ""))) or None
        return None

    # -- endpoint wrappers ---------------------------------------------

    def _products(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        data = self._get("/products", {"symbols": list(symbols)}, ttl=_STATIC_TTL)
        return {
            str(p.get("symbol", "")): dict(p)
            for p in _elements(data.get("elements") or data)
            if p.get("symbol")
        }

    def _pricing(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        data = self._get(
            "/products/data",
            {"symbols": list(symbols), "scope": ["prices", "stock"], "currency": self.currency},
            ttl=_PRICE_TTL,
        )
        return {
            str(p.get("symbol", "")): dict(p)
            for p in _elements(data.get("elements") or data)
            if p.get("symbol")
        }

    # -- normalisation -------------------------------------------------

    def _hit(self, product: Mapping[str, Any]) -> SearchHit:
        return SearchHit(
            supplier=self.name,
            supplier_pn=str(product.get("symbol", "")),
            mpn=_mpn(product),
            manufacturer=_manufacturer(product),
            description=str(product.get("description", "")),
            stock=_int(product.get("stock_quantity")),
            url=_absolute(str(product.get("product_information_page", ""))),
        )

    def _offer(self, product: Mapping[str, Any], pricing: Mapping[str, Any]) -> Offer:
        symbol = str(product.get("symbol", ""))
        _, packaging = strip_packaging(symbol)
        prices = pricing.get("prices") or {}
        return Offer(
            supplier=self.name,
            supplier_pn=symbol,
            mpn=_mpn(product) or symbol,
            manufacturer=_manufacturer(product),
            description=str(product.get("description", "")),
            packaging=packaging or Packaging.UNKNOWN,
            moq=_int(product.get("minimal_amount")),
            multiple=_int(product.get("multiples")),
            stock=_int(pricing.get("stock_quantity")),
            currency=str(prices.get("currency") or self.currency),
            price_breaks=_breaks(prices),
            url=_absolute(str(product.get("product_information_page", ""))),
            match_confidence=Confidence.HIGH,
        )


#: TME's document type for a datasheet. `DTE` is v1's spelling and is accepted
#: too, because the field is free-form enough that assuming one is a guess.
_DATASHEET_TYPES = {"DTE", "DATASHEET", "DS"}


def _elements(node: Any) -> list[dict[str, Any]]:
    """v2 wraps every list as `{"elements": [...]}`. Unwrap, tolerantly."""
    if isinstance(node, Mapping):
        node = node.get("elements", [])
    if not isinstance(node, list):
        return []
    return [dict(item) for item in node if isinstance(item, Mapping)]


def _mpn(product: Mapping[str, Any]) -> str:
    """The manufacturer's own number, which v2 lists rather than names.

    Falls back to the TME symbol: a part with no manufacturer symbol recorded
    still has to be orderable, and an empty MPN would break matching upstream.
    """
    symbols = product.get("manufacturer_symbols")
    if isinstance(symbols, list) and symbols:
        return str(symbols[0])
    return str(product.get("symbol", ""))


def _manufacturer(product: Mapping[str, Any]) -> str:
    manufacturer = product.get("manufacturer")
    if isinstance(manufacturer, Mapping):
        return str(manufacturer.get("name", ""))
    return str(manufacturer or "")


def _breaks(prices: Mapping[str, Any]) -> list[PriceBreak]:
    """TME's price ladder, converted to **net** if it arrived gross.

    klm stores net and displays gross (docs/07 §2), because the VAT rate is a
    property of the buyer rather than of the price. v1 always returned net; v2
    says which it gave you, and taking a gross ladder as net would inflate every
    landed-cost comparison by the whole rate — which is exactly the decision
    those numbers exist to inform.
    """
    divisor = 1.0
    if str(prices.get("type", "")).upper() == "GROSS":
        rate = _float((prices.get("tax") or {}).get("rate"))
        if rate:
            divisor = 1.0 + rate / 100.0

    breaks: list[PriceBreak] = []
    for row in _elements(prices):
        quantity = _int(row.get("amount"))
        price = _float(row.get("price"))
        if quantity and price is not None:
            breaks.append(PriceBreak(quantity, price / divisor))
    return breaks


def _flatten(params: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested parameters into v2's `name[]` / `name[n][key]` query form."""
    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, Mapping):
            flat.extend(_flatten(value, name))
        elif isinstance(value, list | tuple):
            for item in value:
                flat.append((f"{name}[]", str(item)))
        elif value is not None:
            flat.append((name, str(value)))
    return flat


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _absolute(url: str) -> str | None:
    """TME returns protocol-relative URLs; a bare `//…` is not clickable."""
    if not url:
        return None
    return f"https:{url}" if url.startswith("//") else url


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
