# 10 — Ordering and Inventory

Solves [P4](01-vision-and-problems.md#p4--ordering-is-a-spreadsheet-exercise), and covers the
physical-storage roadmap item from the original design note.

## 1. From build plan to order

```
Build plan            "5 × sensor-board (full), 2 × psu-board (basic)"
   │
   ▼ expand BOMs per variant
Gross demand          {klm_id → qty}
   │
   ▼ subtract stock on hand
Net demand
   │
   ▼ apply spares policy
Target quantities
   │
   ▼ round up to MOQ and order multiples, choose price breaks
Order quantities
   │
   ▼ choose a supplier per line  (the interesting step)
Per-supplier carts    TME cart + LCSC cart + total landed cost
```

Each stage is inspectable. `klm order plan --explain` prints the reasoning per line, because a
number you can't justify is a number you won't trust enough to spend money on.

## 2. Spares policy

Buying exactly the quantity needed is wrong for hobby work: one dropped 0402 means a stalled
build and another two-week wait. Policy is per-category and configurable:

```toml
[spares]
default        = { extra_pct = 10, min_extra = 2 }
"passive/0402" = { extra_pct = 100, min_extra = 25 }   # they're pennies and they vanish
"passive/0603" = { extra_pct = 50,  min_extra = 20 }
"ic/*"         = { extra_pct = 0,   min_extra = 1 }
"connector/*"  = { extra_pct = 20,  min_extra = 1 }
"expensive"    = { extra_pct = 0,   min_extra = 0, applies_above = 50.0 }
```

`applies_above` (a unit price threshold in PLN) means expensive parts never get automatic spares
regardless of category — a rule that pays for itself the first time it prevents ordering two
spare €18 MCUs.

## 3. Supplier split

Once quantities are fixed, each line is assigned to a supplier. This is a real optimization
problem because per-supplier shipping and thresholds couple the lines together:

```
minimize   Σ_lines (unit_price(line, supplier) × qty)
         + Σ_suppliers shipping(supplier, cart_total)
         + Σ_suppliers import_charges(supplier, cart_total)

subject to  stock(line, supplier) ≥ qty
            qty respects MOQ and order multiples
            JLCPCB-assembly parts are pinned to LCSC
            lines with a user pin stay where pinned
```

Shipping is a step function (free above a threshold), so the problem is non-convex and greedy
assignment gets it wrong in a specific, predictable way: it splits a cart just below a
free-shipping threshold.

The approach — appropriate to a problem with tens of lines, not thousands:

1. Greedy seed: assign each line to its cheapest landed source.
2. Local search: repeatedly try moving a line **and sets of lines** to the other supplier, keeping
   improvements. The sets are not an optimisation — they are required. Crossing a free-shipping
   threshold needs several lines to move together, and every intermediate state (some moved, some
   not) costs *more* than either end, so a hill-climber taking one step at a time sits in that
   valley and reports the greedy answer. klm tries the cheapest k lines to relocate, for every k.
3. Report the result *with the alternatives*: "LCSC saves 4.20 PLN on this line but adds 3 weeks
   of lead time" — the sort of trade-off only the user can settle.

Hard constraints that override cost:

- Parts destined for JLCPCB assembly must come from LCSC.
- A user pin (`klm order pin <klm_id> --supplier tme`) is absolute.
- A line whose only in-stock source is one supplier has no choice.

## 4. Cart export

klm does not place orders. It produces carts a human reviews and submits.

Every figure is an **estimate**, labelled as one, with its assumptions printed beside it. Rates are
configuration and never code, which is not fastidiousness: the EU's €150 duty exemption ended on
1 July 2026 and a €3 per-item duty replaced it, so anything hardcoded in June was wrong in July
([Q8](14-open-questions.md#q8)). One consequence worth stating — **set `vat_rate` the same way on
every supplier.** Omitting it on one tilts the comparison toward that supplier by the whole rate,
which is exactly the decision the number exists to inform.

| Supplier | Export format |
|---|---|
| TME | CSV / plain `symbol;quantity` list suitable for the bulk-add form |
| LCSC | CSV in LCSC's BOM-upload column layout |
| JLCPCB assembly | The assembly BOM from [09](09-manufacturing-outputs.md) |

Every export is accompanied by a human-readable summary: line count, subtotal, estimated
shipping, estimated import charges, estimated total, and a list of every assumption made.

## 5. Order lifecycle and receiving

```
klm order plan --build "5×sensor-board:full,2×psu-board:basic"   → draft
klm order export tme                                              → cart file
klm order mark-placed <order-id> --total 312.40                   → placed
klm order receive <order-id>                                      → received, stock incremented
klm order receive <order-id> --partial R1234:50                   → partially_received
```

Receiving is the only operation that increments stock, which keeps inventory honest. Discrepancies
(fewer parts arrived than ordered, or a substitution was shipped) are recorded on the line rather
than silently reconciled.

## 6. Inventory

```sql
stock_item (klm_id, location, quantity, last_counted)
```

Locations are hierarchical paths: `Cabinet A / Drawer 12 / Bag 3`. Deliberately just a string —
imposing a schema on someone's physical storage is a losing game.

Operations:

- `klm stock list --low` — parts below a per-part reorder threshold.
- `klm stock adjust <klm_id> --location "A/12" --set 143` — a physical count.
- `klm stock consume --build "1×sensor-board:full"` — decrement after actually building.
- `klm stock where <klm_id>` — the question you ask most often: *where did I put those?*

Stock is advisory, not authoritative: it drifts from reality the moment you take a part out
without recording it. `last_counted` makes staleness visible, and ordering treats old counts
conservatively (an un-counted-for-a-year location contributes less confidence to "we already
have these").

## 7. Labels for 3D-printed drawers

The roadmap item from the original design note, and the piece that closes the loop between the
catalog and the physical bench.

**Constraint**: labels around 14×14 mm for 3D-printed SMD drawers. That's small — a QR code
carrying a URL is unreadable at that size by a phone camera; a short opaque ID is not.

Design:

- Encode **only a short identifier** — a Crockford-Base32 shortening of `klm_id`, ~8 characters,
  collision-checked against the catalog. Not a URL.
- ~~**Data Matrix** rather than QR~~ — **not implemented**, see below.
- The human-readable portion carries what you actually need while standing at the drawer:
  value and package (`100nF 0402 X7R`), not the MPN — you can look that up, but you can't tell
  two drawers of ceramics apart without it.

```
┌──────────────┐
│ 100nF        │   two text lines + the short ID
│ 0402 X7R     │
│ ABC12345     │
└──────────────┘
```

### Why there is no barcode yet

[Q6](14-open-questions.md#q6) records that nobody has confirmed a Data Matrix at ~6x6 mm is
reliably scannable by the phone or scanner actually in use, and its own fallback position is "a
human-readable short ID and no barcode, which is a smaller loss than it sounds". An ECC200 encoder
is a few hundred lines of Reed-Solomon that **no test here can validate** — there is no scanner on
this machine to read the output — and a barcode that looks right, passes a visual check and does not
scan is the same failure this project refuses for chip land patterns, for `unchecked` QA results and
for bundled rotation data.

So the label carries the short ID in readable text, `klm labels scan ABC12345` resolves it, and the
layout reserves the space. When a printer and a scanner exist to test against, the encoder drops in
and nothing else changes.

Output formats:

- **PDF sheets** for a standard label sheet, with configurable grid and margins.
- **PNG** at a specified DPI for direct printing.
- **Thermal printer** formats. Niimbot and Brother QL devices are common in this space; klm
  emits an image the vendor tooling can consume rather than implementing printer protocols.
  ⚠️ Direct printer support is unspecified until a target device is chosen — see
  [14 — Open questions](14-open-questions.md#q6).

```bash
klm labels print --location "Cabinet A/*"     # everything in a cabinet
klm labels print --order <order-id>           # everything that just arrived  ← the common case
klm labels print --klm-id 01JB…FA --count 3
klm labels scan 01JB4K7Q                      # decode a scanned ID → part page
```

`--order` is the workflow that matters: parts arrive, you print exactly the labels for what
arrived, stick them on drawers, and record locations in one pass.

## 8. Approved substitutions

`klm substitutes` (docs/11 §8) answers a mechanical question: same land pattern, same pinout.
Whether the part actually fits *this* circuit is a human's judgement, and once made it is worth
recording rather than re-deriving each time a supplier runs out:

```bash
klm substitutes RC0402FR-0710KL --approve CRCW040210K0FKED --reason "same 1% 10k" --by rs
klm substitutes RC0402FR-0710KL --approved     # with a re-check against today's assets
klm substitutes RC0402FR-0710KL --revoke CRCW040210K0FKED
```

Four properties, each of them a decision:

- **A reason is required**, as it is for a rejected proposal. An approval nobody explained cannot
  be reviewed a year later, which makes it indistinguishable from a mistake.
- **The mechanical verdict at approval time is stored**, differences and all. Approving something
  klm called `differs` is legitimate — a human overruling the geometry is exactly what this record
  is for — but keeping what was overruled is what stops the approval reading as agreement. Listing
  re-runs the comparison and flags a verdict that has since changed, because assets move.
- **It is directional.** Approving B in place of A says nothing about A in place of B: the part
  with the tighter specification is not interchangeable in both directions.
- **Ordering reports, it never swaps.** When no enabled supplier can fill a line, `klm order plan`
  names any approved substitute that *is* orderable. It stops there. klm knows the substitution
  was approved; it does not know it was approved for this build, and quietly ordering a part the
  BOM does not name is how the wrong component reaches a board.

## 9. Cost reporting

Useful side effect of having all of this in one database:

```bash
klm cost project --project sensor-board --qty 5   # parts cost per board at qty 5
klm cost history                                  # spend, by month, supplier and category
```

Per-board cost at a given quantity is the number that decides whether a design change is worth
it, and it's tedious enough to compute by hand that it usually doesn't get computed. The quantity
is not a multiplier: each line is priced at the break its own run quantity reaches, which is
where building five instead of one actually changes the answer.

It is an estimate, and it is honest about what it leaves out:

- **A line with no offer is reported, never counted as free.** A total quietly missing the
  expensive connector is worse than no total; `--exit-code` makes an incomplete one fail CI.
- **Currencies are never summed.** Totals come out per currency, because adding PLN to EUR
  produces a figure wrong by the exchange rate and right-looking.
- **Stock is not subtracted, and fabrication is not included.** This is what the parts cost, not
  what this build costs you given the shelf — that is `klm order plan`'s question, and answering
  both in one number answers neither.

`klm cost history` counts placed orders only. A draft is a plan, and counting plans as spend makes
the figure useless for the one thing it is for. Quantities are what was *ordered*: a discrepancy
on receiving adjusts stock and is recorded on the line, but the money left the account either way.

*Not built: comparing two revisions of a board (`klm cost compare`). It needs project history
checked out at each revision, which is a different problem from pricing one.*
