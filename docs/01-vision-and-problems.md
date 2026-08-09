# 01 — Vision and Problems

## 1. Who this is for

A single engineer doing study and hobby PCB work in KiCad, based in Poland, who:

- prototypes each new feature on its own small board, then folds proven blocks into one large project;
- buys from TME (domestic, fast, invoiced) and LCSC/JLCPCB (cheap, slow, imported), because
  DigiKey/Mouser shipping is prohibitive for order sizes in the tens of złoty;
- fabricates and sometimes assembles at JLCPCB;
- wants to publish some projects on GitHub and collaborate;
- stores physical parts in 3D-printed drawers and would like them labelled.

Everything below follows from that. This is explicitly **not** a multi-user PLM system. Where a
choice exists between "correct for a 200-person company" and "correct for one person with 900
parts and 30 projects", take the latter.

## 2. The seven concrete problems

### P1 — Sourcing is constrained to two suppliers, and availability changes

A part that isn't at TME or LCSC effectively doesn't exist. Datasheet-perfect parts are useless
if they're out of stock, and stock moves. Today this means opening a browser tab per candidate
and checking manually, then re-checking at order time because weeks have passed.

*What good looks like:* every part in the catalog carries live offers from both suppliers —
stock, price breaks, MOQ, packaging — refreshed on demand and before every order.

### P2 — Library assets are a chore per part

A usable part needs a schematic symbol, a PCB footprint, and ideally a 3D model. Drawing these
by hand is slow; scavenging them from EasyEDA/SnapEDA is faster but leaves inconsistent naming,
absolute file paths in `.kicad_mod` model references, and mesh-based 3D models where KiCad wants
solid STEP.

*What good looks like:* one command takes an LCSC part number or an MPN and produces a symbol,
footprint and STEP model that pass a mechanical QA gate before they're allowed into the catalog.

### P3 — Field names drift, so BOMs never line up

Even with discipline, symbols accumulate `MPN`, `mpn`, `Manufacturer_Part_Number`, `Part Number`.
Values drift too: `100n`, `0.1uF`, `100nF`, `100 nF`. Every BOM export then needs hand-fixing,
and cross-project aggregation is impossible.

*What good looks like:* a canonical field schema, an alias map that recognises the historical
spellings, and a linter that can mechanically fix the drift — runnable as a pre-commit hook.

### P4 — Ordering is a spreadsheet exercise

Building five of project A and two of project B means merging BOMs, subtracting what's already
in the drawers, adding spares for the 0402s, splitting lines between TME and LCSC on price,
respecting MOQs and price breaks, and keeping both orders above their free-shipping thresholds.
Done by hand, in a spreadsheet, every time.

*What good looks like:* declare the build plan, get two ready-to-paste supplier carts and a
total cost, with the reasoning for each supplier choice visible.

### P5 — Fabrication output needs hand-fixing

JLCPCB wants Gerbers, drill files, a BOM with LCSC part numbers, and a CPL (pick-and-place) file.
The CPL is the problem: JLCPCB's expected component orientation differs from KiCad's footprint
convention for many packages, so parts come back rotated 90° or 180°.

*What good looks like:* a corrections database applied at export, plus the ability to record
"this part came back rotated" after a real run so the same mistake never happens twice.

### P6 — Global libraries and project libraries pull in opposite directions

Working style demands **global** libraries: a part proven on a test board must be instantly
available to the next board without copying anything. Publishing demands **project-local**
libraries: a repository a collaborator clones must render correctly with no external setup.
Satisfying both by hand means maintaining every part twice and manually re-syncing when either
copy changes.

*What good looks like:* the global catalog is the source of truth; a project can be *vendored*
(a self-contained copy generated into the repo, with all references rewritten) and later
*re-synced*, with drift on either side reported rather than silently overwritten. See
[06 — Library sync](06-library-sync.md); this is the hardest problem in the project.

### P7 — Finding the right part in the first place

"I need a 5 V→3.3 V buck, ≥1 A, small package, cheap, in stock locally" is a research task:
search two suppliers, read datasheets, compare parameters, check the footprint exists. It is
the single most time-consuming step and the one an LLM is genuinely good at — provided every
claim is traceable to a datasheet page or an API response, and a human approves before anything
enters the catalog.

*What good looks like:* state the requirement, get three ranked candidates with cited parameters
and live pricing, approve one, and have its assets built automatically.

## 3. Goals

1. **One source of truth for parts.** A part exists once, with one identity, one set of fields,
   one symbol, one footprint, one model — and many supplier offers.
2. **Never lose user data.** klm edits KiCad files that represent real work. Any file it writes
   must round-trip losslessly; anything it doesn't understand is preserved verbatim.
3. **Reviewable automation.** Every automatic action produces a diff or a draft a human approves.
   The AI agent proposes; it never commits to the catalog.
4. **Scriptable first, graphical second.** Everything is a CLI command over a plain Python core;
   the desktop app is a client of that core, not the place logic lives.
5. **Local-first.** The catalog, assets and inventory are files on disk under the user's control,
   versionable in git. No account, no cloud dependency for core function.
6. **Deterministic output.** Generated files are byte-stable given the same inputs, so git diffs
   are meaningful and re-running a command is a no-op.

## 4. Non-goals

- Multi-user concurrency, permissions, approval workflows.
- Being a general EDA tool. klm never opens a schematic editor; KiCad does that.
- Supporting every supplier. TME and LCSC are first-class; the adapter interface exists so
  others *can* be added, but none are promised.
- Cloud sync or a hosted service.
- Replacing KiCad's own libraries. klm's catalog sits alongside the stock KiCad libraries and
  is expected to hold only parts the user actually buys.
- Automatic ordering. klm prepares carts; a human places the order.

## 5. Success criteria

The project has succeeded when, for a typical small board:

| Measure | Today | Target |
|---|---|---|
| Time from "I need a part like X" to a usable symbol+footprint+3D in the library | 30–90 min | < 10 min, most of it review |
| Field-schema violations across the whole catalog | unknown, many | zero, enforced in CI |
| Time to prepare a combined multi-project order | ~1 hour of spreadsheet | < 5 min |
| Manual edits to JLCPCB CPL after export | several per board | zero |
| Effort to publish a project as a self-contained repo | manual re-creation of parts | one `klm vendor` |
| Boards fabricated with a rotation error | occasional | zero after the first occurrence of any given package |

## 6. Guiding principles

- **The catalog is small and curated.** Prefer reusing an existing part to adding a near-duplicate.
  Part count is a cost — in ordering, in stocking, in review.
- **Provenance beats confidence.** A parameter is worth having only if you can say where it came
  from. Datasheet page, API response, or user-entered — always recorded.
- **Fail loudly on ambiguity, silently on the routine.** A part with two plausible footprints
  should stop and ask. A part with one obvious answer should not.
- **The physical world is the final authority.** If a fabricated board says the rotation was
  wrong, the database is wrong. Feed reality back in.
