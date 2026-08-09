# 07 — Supplier Integration

Solves [P1](01-vision-and-problems.md#p1--sourcing-is-constrained-to-two-suppliers-and-availability-changes).

## 1. The adapter interface

Every supplier implements one protocol. Services never talk to a supplier-specific type.

```python
class SupplierAdapter(Protocol):
    name: str                     # "tme" | "lcsc"
    currency: str                 # native billing currency

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]: ...
    def search_parametric(self, category: str, filters: dict) -> list[SearchHit]: ...
    def get_offer(self, supplier_pn: str) -> Offer | None: ...
    def get_offers(self, supplier_pns: Sequence[str]) -> dict[str, Offer]: ...  # batched
    def resolve_mpn(self, mpn: str, manufacturer: str | None) -> list[Offer]: ...
    def datasheet_url(self, supplier_pn: str) -> str | None: ...
```

`get_offers` is batched deliberately: refreshing a 200-line BOM as 200 individual calls is both
slow and rude to the API. Adapters that lack a batch endpoint implement it as a rate-limited loop.

Everything returns the normalized `Offer` from [02](02-domain-model.md#offer). Supplier-specific
quirks are absorbed inside the adapter and never leak.

## 2. TME

TME is the domestic supplier: fast delivery, invoicing, PLN pricing, the default for anything
needed this week.

- **API**: a documented REST API requiring registration for an application key and secret.
  Relevant operations: product search, `GetProducts` (details), `GetPrices` / `GetStocks`,
  `GetParameters`, `GetProductsFiles` (datasheets).
- **Authentication**: request signing with the application secret. ⚠️ The original design note
  claimed OAuth 2.0; TME's published scheme is signature-based, not OAuth. **This must be verified
  against current TME developer documentation before implementation** — see
  [14 — Open questions](14-open-questions.md#q1).
- **Pricing**: net vs gross (VAT) matters for a Polish buyer. klm stores net prices and displays
  gross, with the VAT rate configurable.
- **Currency**: PLN natively.
- **Quirks**: TME "symbols" are its own part identifiers, not MPNs, and a symbol may include a
  packaging suffix. The adapter records both the symbol and the manufacturer MPN it maps to.

## 3. LCSC

LCSC is the cheap, slow, imported supplier and — critically — the one whose part numbers JLCPCB's
assembly service consumes.

- **API**: LCSC does not publish an official, documented public API for third-party use. Practical
  options are the community `jlcsearch`-style services and the EasyEDA component endpoints that
  `easyeda2kicad`-class tools already use.
- ⚠️ **This is the single largest technical and legal risk in the project.** Unofficial endpoints
  can change or disappear without notice, and using them may conflict with the operator's terms of
  service. See [14 — Open questions](14-open-questions.md#q2). Mitigations:
  - The LCSC adapter is isolated behind the same protocol as everything else, so it can be
    replaced or degraded without touching services.
  - A **manual mode** is a first-class fallback: paste an LCSC part number and the fields you can
    see on the product page. klm remains fully functional with zero LCSC automation, just less
    convenient.
  - Requests are conservatively rate-limited and aggressively cached.
- **Currency**: USD. Converted for comparison using a rate the user sets or klm fetches; the
  original currency is always retained and shown.
- **Landed cost**: a USD unit price is not comparable to a PLN one. See §6.

## 4. Caching and rate limiting

Offers are a cache of a remote fact, and the freshness required differs by context:

| Context | Acceptable age | Behavior |
|---|---|---|
| Browsing the catalog | 30 days | Serve from cache, show the age |
| Designing / choosing a part | 7 days | Refresh in the background |
| Preparing an order | 1 hour | Force refresh before committing to a cart |
| Agent research | 1 day | Refresh candidates only |

Implementation:

- HTTP responses cached on disk under `cache/suppliers/`, keyed by a hash of the request, with a
  per-endpoint TTL.
- A token-bucket rate limiter per supplier, configurable, defaulting well below any published
  limit.
- Retries with exponential backoff and jitter on 429/5xx; a circuit breaker that stops hammering
  a supplier that's down and reports degraded mode to the UI.
- `klm refresh --stale 7d` refreshes everything past a threshold; `klm refresh --project .`
  refreshes only what a given board uses.

**klm always works offline**, in degraded mode: cached offers are served with a visible staleness
marker, and any operation that requires fresh data (placing an order) refuses rather than
guessing.

## 5. Matching a part to offers

Turning an MPN into supplier offers is the fiddliest part of the adapter layer.

```
MPN "STM32F103C8T6"
  ├─ exact match on supplier's MPN field            → high confidence, auto-link
  ├─ match after stripping packaging suffixes       → medium, auto-link with a note
  │    (-TR, -REEL, -T&R, -ND, /TR, tape-and-reel)
  ├─ match with manufacturer alias resolution       → medium
  │    (ST / STMicro / STMicroelectronics)
  └─ fuzzy / multiple candidates                    → ask the user, never guess
```

Manufacturer aliasing needs its own normalized table; the same company appears under three or
four names across two suppliers.

Confidence is stored on the offer link. `klm lint` rule P00x flags low-confidence links for
review, so a wrong auto-match surfaces before it reaches an order.

## 6. Comparing across suppliers

A raw unit-price comparison between TME (PLN, net, domestic) and LCSC (USD, gross of nothing,
imported) is meaningless. The comparison klm actually makes is **landed cost per unit**:

```
landed = unit_price × qty
       + FX conversion (if not PLN)
       + shipping share
       + customs duty       (if applicable, imports above the duty threshold)
       + import VAT         (charged on imports; IOSS collection may apply at checkout)
```

klm's model:

- Every supplier carries a **cost model** in config: shipping tiers, free-shipping threshold,
  handling fees, and whether import charges apply.
- Duty and VAT rates and thresholds are **configuration, not code**, because they change and
  because klm must not present tax figures as authoritative. They're clearly labelled as
  estimates.
- Non-price factors are shown alongside cost, never folded into it: lead time, stock margin,
  whether the part is needed for JLCPCB assembly (which forces LCSC regardless of price).

The comparison is presented, with its inputs, for the user to decide. klm does not silently pick
a supplier ([10](10-ordering-and-inventory.md) covers the optimizer that proposes a split).

## 7. Extensibility

Adding a supplier means implementing the protocol and adding a config block:

```toml
[suppliers.tme]
enabled = true
api_key = "env:TME_API_KEY"
api_secret = "env:TME_API_SECRET"
currency = "PLN"
free_shipping_above = 250.0
shipping_flat = 15.0
vat_rate = 0.23

[suppliers.lcsc]
enabled = true
mode = "unofficial"          # unofficial | manual
currency = "USD"
shipping_flat = 12.0
import_charges = true
```

Credentials are referenced by environment variable, never stored in the config file, and never
written to `event_log` or any diagnostic bundle.

Plausible future adapters — Mouser and DigiKey (which do publish official APIs, useful for
*parametric research* even when shipping makes them unusable for purchase), Farnell, and Polish
hobby retailers like Botland or Kamami — are enabled by the interface but not committed to.
Using DigiKey purely as a parametric search index while buying from TME is a genuinely useful
pattern the architecture permits.
