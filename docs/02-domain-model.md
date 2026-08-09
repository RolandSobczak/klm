# 02 — Domain Model

The vocabulary the whole system uses. Every other document assumes these definitions.

## 1. The central distinction: Part vs Offer

The single most important modelling decision. A **Part** is the electrically and mechanically
distinct thing that appears in a schematic. An **Offer** is a specific orderable item at a
specific supplier.

```
Part  (STM32F103C8T6, LQFP-48)
 ├── Offer  TME   "STM32F103C8T6"       tray, MOQ 1,  38 in stock,  price breaks in PLN
 └── Offer  LCSC  "C8734"               reel, MOQ 1,  4200 in stock, price breaks in USD
```

Why this split matters here specifically:

- The same part is at both suppliers at very different prices and lead times. Choosing where to
  buy is a *per-order* decision (P4), not a property of the part.
- Packaging suffixes (`-TR`, `-REEL`, `-T&R`, `-ND`) distinguish order codes for what is
  electrically the same die in the same package. Those belong on the Offer, not the Part.
- A part can be in the catalog and usable in a design with **zero** offers — it's simply flagged
  unbuyable until one appears.

## 2. Entities

### Part

The design-time object. One row per distinct component.

| Field | Notes |
|---|---|
| `klm_id` | Opaque, immutable, ULID-like. **Never** changes, never reused. The join key for everything. |
| `mpn` | Manufacturer part number, base form (packaging suffix stripped) |
| `manufacturer` | Normalized against an alias list (`ST`, `STMicro`, `STMicroelectronics` → one) |
| `description` | Human summary, one line |
| `category` | Taxonomy path, e.g. `IC/Power/Regulator/Switching` |
| `package` | `LQFP-48`, `SOT-23-6`, `0402` — the physical package, independent of footprint variant |
| `parameters` | Typed key/value set with units and provenance (see §4) |
| `datasheet_url` | Plus a local cached copy |
| `lifecycle` | `active` / `nrnd` / `obsolete` / `unknown` |
| `assets` | Symbol, footprint, 3D model references (see below) |
| `status` | `draft` / `approved` / `deprecated` — only `approved` parts are usable in designs |

`(manufacturer, mpn)` is a natural uniqueness constraint, but `klm_id` is the key everything
references. Name-based keys break the moment a manufacturer is renamed or an MPN is corrected.

### Offer

| Field | Notes |
|---|---|
| `supplier` | `tme` / `lcsc` |
| `supplier_pn` | TME symbol, or LCSC `C…` number |
| `packaging` | `cut tape` / `reel` / `tray` / `tube` / `bag` |
| `moq`, `multiple` | Minimum order quantity and order increment |
| `stock` | Units available |
| `price_breaks` | `[(qty, unit_price, currency)]` |
| `lead_time_days` | If the supplier reports it |
| `fetched_at` | Offers are a cache of a remote fact and are always stamped |

Offers are **derived data**. They are never hand-edited and never committed to git as truth;
losing them costs one API refresh.

### Asset

A file klm manages on the part's behalf. Three kinds:

| Kind | Format | Notes |
|---|---|---|
| `symbol` | KiCad S-expression symbol | Stored as a fragment, assembled into `.kicad_sym` on generation |
| `footprint` | `.kicad_mod` | Model reference always uses a KiCad env var, never an absolute path |
| `model3d` | `.step` (preferred), `.wrl` | Deduplicated by content hash — many parts share one model |

Every asset carries `content_hash` (SHA-256 of canonical bytes), `source` (`generated` /
`imported:easyeda` / `imported:kicad` / `hand-drawn`), and `license_note` where the origin
imposes one.

**Assets are shared, not owned.** Fifty 0402 resistors reference one footprint asset and one
3D model asset. This is what keeps the repository small and makes a footprint fix propagate.

### Project

A KiCad project klm knows about.

| Field | Notes |
|---|---|
| `path` | Directory containing the `.kicad_pro` |
| `mode` | `linked` (uses global libs) or `vendored` (self-contained) — see [06](06-library-sync.md) |
| `lock` | `klm.lock.json` — which parts at which content hashes were vendored |
| `variants` | Optional assembly variants (populated / DNP sets) |

### BOM line

Derived from a schematic, never stored as truth.

`(klm_id, references[], quantity_per_board, dnp, variant)`

The `references[]` (`R1`, `R2`, `C7`) matter for CPL generation and for telling the user *where*
a problem is.

### Build plan

The input to ordering: `[(project, board_count, variant)]`, plus a spares policy.

### Order

A prepared purchase, per supplier: lines, quantities, prices, shipping estimate, state
(`draft` → `placed` → `received`). Receiving an order is what increments stock.

### Stock item

`(klm_id, location, quantity, last_counted_at)`. Location is a path: `Cabinet A / Drawer 12 / Bag 3`.

## 3. Relationships

```
                    ┌──────────────┐
                    │   Category   │
                    └──────┬───────┘
                           │
   ┌────────┐ 0..n  ┌──────┴───────┐ 1..n   ┌───────────┐
   │ Offer  ├───────┤     Part     ├────────┤ Parameter │
   └────────┘       └───┬───┬───┬──┘        └───────────┘
                        │   │   │
              symbol ───┘   │   └─── model3d ──┐
                            │                  │  (shared by content hash)
                        footprint ─────────────┘
                            │
                  ┌─────────┴─────────┐
                  │       Asset       │
                  └───────────────────┘

   Project ──uses──▶ Part          (via lib_id in .kicad_sch, resolved to klm_id)
   BuildPlan ──▶ Project × count
   BuildPlan ──▶ Demand ──minus Stock──▶ Order lines ──choose Offer──▶ Order
   Order received ──▶ Stock
```

## 4. Parameters and provenance

A parameter is not a bare value. It is:

```
{ "name": "vout_max", "value": 3.3, "unit": "V", "tolerance": null,
  "source": {"kind": "datasheet", "url": "...", "page": 4, "quote": "..."},
  "confidence": "high", "extracted_at": "2026-08-09" }
```

Sources, in descending trust:

1. `user` — a human typed it.
2. `datasheet` — extracted from a PDF, with page and quoted snippet.
3. `supplier` — a TME/LCSC parametric field. Often wrong or unit-ambiguous.
4. `inferred` — derived by the agent from other parameters. Always flagged.

When a datasheet and a supplier disagree, the datasheet wins and the conflict is recorded, not
silently resolved. This is what makes the AI agent's output auditable ([11](11-ai-research-agent.md)).

## 5. Identity, in detail

Three identifiers coexist, deliberately.

| Identifier | Audience | Stability |
|---|---|---|
| `klm_id` (`01JB4K7Q…`) | Machines | Immutable forever |
| `(manufacturer, mpn)` | Humans, suppliers | Stable in practice, occasionally corrected |
| Symbol name (`STM32F103C8T6`) | KiCad, humans reading a schematic | May be renamed |

`klm_id` is embedded as a field in **every generated symbol**. That single decision is what makes
library sync tractable: a schematic that has drifted, been renamed, or been edited by a
collaborator can still be matched back to the catalog. Without it, sync degrades to fuzzy
name matching.

Rules:

- `klm_id` is generated once, on part creation, and never regenerated.
- Merging two parts discovered to be duplicates keeps the older `klm_id` and records the other
  as an alias, so old schematics still resolve.
- Deleting a part is a soft delete (`status: deprecated`). Hard deletion would orphan schematics.

## 6. State machines

**Part status**

```
draft ──approve──▶ approved ──deprecate──▶ deprecated
  │                    │                        │
  └──discard──▶ ✗      └──◀── un-deprecate ─────┘
```

A `draft` part is one the agent proposed or an import produced. It cannot be used in a design
and is not exported to the generated libraries. Approval requires: all required fields present,
assets present and QA-passing, at least one offer or an explicit "no supplier" acknowledgement.

**Order**

```
draft ──▶ placed ──▶ partially_received ──▶ received
   └──▶ cancelled
```

**Project mode**

```
linked ──vendor──▶ vendored ──unvendor──▶ linked
              ▲         │
              └──sync───┘
```

## 7. Units and normalization

Everything numeric is stored in SI base units as a float, plus a display hint. `100n`, `0.1uF`,
`100nF` and `100 nF` all store `1e-7 F` and display as `100nF`. The parser accepts:

- SI prefixes `p n u µ m k M G` (`µ` and `u` equivalent)
- R-notation: `4k7` → 4700, `1R2` → 1.2, `2n2` → 2.2e-9
- Ω / R / ohm suffixes, case-insensitive
- Ranges (`-40..85`) and tolerances (`±1%`, `1%`, `0.01`)

Round-tripping is a property test: `format(parse(s))` must be stable, and `parse(format(v)) == v`
within float tolerance. Value normalization is where BOM-merging correctness actually lives, so
it gets tested harder than anything else in the codebase.
