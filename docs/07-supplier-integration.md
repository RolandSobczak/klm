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

- **API**: the **v2** REST API, registration at `developers.tme.eu` for an application token and
  secret. Operations klm uses: `GET /products/search` (keyword *and* parametric),
  `GET /products` (details, by `symbols[]` or `mpns[]`), `GET /products/data`
  (prices, stock), `GET /products/categories/tree`, `GET /products/files` (datasheets).
- **Authentication**: OAuth 2.0 client credentials. `POST /auth/token` with
  `Authorization: Basic base64(token:secret)` and `grant_type=client_credentials` returns a bearer
  token that **expires in 300 seconds**, plus a refresh token. Five minutes is short enough that a
  single `klm refresh` over a few hundred parts outlives it, so the adapter renews 30 seconds early
  and retries once on a 401 — expiry mid-flight is the normal case, not an error.

  klm targeted **v1** until 2026-08-09: signed form POSTs with an HMAC-SHA1 signature over
  `POST&<encoded URL>&<encoded sorted query>`. That is what [Q1](14-open-questions.md#q1)
  documented, and it still answers — but TME calls it deprecated and v2 differs in every respect.
  Migrated before anyone depended on v1, which is the cheap moment. See the Q1 addendum.
- **Parametric search** filters by **numeric parameter and value identifiers**, not names or
  comparisons ([Q10](14-open-questions.md#q10--parametric-search-quality-at-tme-and-lcsc--resolved-2026-08-09)).
  `klm.suppliers.constraints` turns `{"Vin max": ">=18V"}` into those IDs, using
  `scope[]=parameters` for discovery and `klm.units` for the arithmetic. Nothing above the adapter
  ever handles an identifier — a wrong one returns confidently wrong parts rather than an error.
- **Pricing**: net vs gross (VAT) matters for a Polish buyer. klm stores net and displays gross,
  with the VAT rate configurable. **v2 states which it returned** (`prices.type` is `NET` or
  `GROSS`, with the tax rate beside it), so the adapter converts rather than assuming — v1 always
  returned net, and taking a gross ladder as net would inflate every landed-cost comparison by the
  whole rate.
- **Currency**: PLN natively.
- **Quirks**: TME "symbols" are its own part identifiers, not MPNs, and a symbol may include a
  packaging suffix. The adapter records both the symbol and the manufacturer MPN it maps to.

## 3. LCSC

LCSC is the cheap, slow, imported supplier and — critically — the one whose part numbers JLCPCB's
assembly service consumes.

- **API**: LCSC publishes an official API, but grants it **per company** — the application wants a
  company website, a business licence and an estimated order volume — and its terms forbid
  redistributing either the data or the documentation. klm's user will not be granted it.
  [Q2](14-open-questions.md#q2) is resolved and the answer changed the design; see
  [ADR-0009](adr/0009-lcsc-manual-first.md).
- **Manual mode is the default and the primary path**, not a fallback:

  ```bash
  klm offers STM32F103C8T6 --supplier lcsc --add C8734 --price 1.42 --stock 4200
  ```

  Paste what you can see on the product page. The link is stored at `high` confidence, because a
  human matching a part is a better signal than any string comparison klm can make. Everything
  that needs no API still works automatically: part-number validation, product and datasheet URLs,
  packaging-suffix handling, and the `LCSC` field JLCPCB's assembly service reads.
- In manual mode every lookup **declines quietly** rather than raising. LCSC having no automated
  answer is the expected state, not a failure — a refresh across 200 parts must not print 200
  errors about it.
- **klm ships no code against unofficial endpoints**, in any mode. The community `jlcsearch`-style
  services and the EasyEDA component endpoints work today and may not tomorrow, and bulk-pulling
  pricing from them is squarely what the terms prohibit. Isolating that behind an interface would
  make it *replaceable*, not *permitted*.
- `mode = "api"` accepts a user-supplied client satisfying the same protocol, for someone who has
  been granted access and therefore holds documentation klm's authors may not.
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

- HTTP responses cached on disk under `cache/suppliers/<supplier>/`, keyed by a SHA-256 of method,
  URL and body. **TTL is the caller's decision, per request**, not a property of the cache — that
  is what makes the table above expressible. A corrupt cache entry is a cache miss, not an error:
  the authoritative copy is one request away.
- A token-bucket rate limiter per supplier, configurable, defaulting to 2 req/s. Being throttled is
  a self-inflicted outage.
- Retries with exponential backoff and jitter on 408/425/429/5xx. A 404 is an *answer* and is not
  retried, so a malformed query cannot open the breaker.
- A circuit breaker: after five consecutive failures it opens and fails fast; after the reset
  window **one** request is let through to probe, not a thundering herd.
- `klm refresh --stale 7d` refreshes everything past a threshold; `klm refresh --part <id>` one
  part; `klm refresh --supplier tme` one supplier. (`--project .` arrives with phase 4, which is
  what makes a project resolvable to catalog parts.)

**klm always works offline**, in degraded mode:

- `--offline` serves the HTTP cache and never touches the network.
- When a supplier fails mid-request and a cached response exists, the stale one is served rather
  than the request failing. Stale beats absent; the offer's `fetched_at` says how stale.
- An offer the supplier has stopped listing is **kept, not deleted** — deleting it would look
  identical to never having had one, and it ages into P003 on its own.
- A supplier that is down is reported **once per run**, not once per part.

Clock, sleep and transport are all injected. A rate limiter you cannot test without waiting is a
rate limiter that does not get tested, and one that isn't tested opens in production for the first
time.

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

Confidence is stored on the offer link, together with the reason it was assigned. A `low`
confidence **never auto-links**: the candidate is reported by `klm refresh` and not written to the
database at all, because an offer in the database is one klm is willing to order against. `medium`
and `high` are stored, and `klm lint` rule P007 surfaces anything that later reads as doubtful.

Two rules keep this honest:

- **Fold nothing that carries meaning.** Case and whitespace fold; hyphens, slashes and dots do
  not. `LM317T` and `LM317-T` are not reliably the same device.
- **Refreshing is not re-matching.** A refresh re-reads price and stock for a link a human or a
  confident match already blessed; it never re-adjudicates identity. Otherwise a wrong match could
  appear silently months after the part was approved.

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
rate_per_second = 2.0        # deliberately well below any published limit
burst = 4.0

[suppliers.lcsc]
enabled = true
mode = "manual"              # manual | api
currency = "USD"
shipping_flat = 12.0
import_charges = true
```

Blocks **merge onto the defaults** rather than replacing them, for the same reason field aliases
do: setting one key on TME must not silently reset its VAT rate to zero.

Credentials are referenced by environment variable, never stored in the config file, and never
written to `event_log` or any diagnostic bundle. A literal secret in `api_key` is rejected at load
time — `config.toml` lives in a directory users are encouraged to put under git, and a file format
that invites pasting an API key into it is a file format that leaks API keys.

Plausible future adapters — Mouser and DigiKey (which do publish official APIs, useful for
*parametric research* even when shipping makes them unusable for purchase), Farnell, and Polish
hobby retailers like Botland or Kamami — are enabled by the interface but not committed to.
Using DigiKey purely as a parametric search index while buying from TME is a genuinely useful
pattern the architecture permits.
