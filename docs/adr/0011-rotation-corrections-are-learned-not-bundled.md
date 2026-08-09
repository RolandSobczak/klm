# ADR-0011 — Rotation corrections are learned per part, and klm bundles none

**Status:** accepted · **Phase:** 5 · **Related:** [09](../09-manufacturing-outputs.md),
[Q3](../14-open-questions.md#q3), [ADR-0009](0009-lcsc-manual-first.md)

## Context

JLCPCB's pick-and-place expects a component orientation that differs from KiCad's footprint
convention for many packages. Get it wrong on a polarised part and the board is scrap, not
cosmetically off. [09 §3](../09-manufacturing-outputs.md#3-the-rotation-problem) plans a correction
table keyed by footprint-name pattern, seeded with bundled starting data and improved by
`klm fab feedback`. [Q3](../14-open-questions.md#q3) flagged the bundled data as unverified.

Checking it, August 2026, turned up three things — the third of which invalidates the key, not just
the data.

**JLCPCB publishes nothing.** Their own "How to export BOM and CPL from KiCad" guide does not give
per-package values; it tells the reader to install a third-party plugin and tick "apply automatic
component translations". The authoritative source does not exist.

**Every community table is one table.** `Bouni/kicad-jlcpcb-tools` states plainly that it adopted
the corrections from `matthewlai/JLCKicadTools`. KiBot's notes cite the same project. So the
"multiple sources that disagree" the risk register worried about are in fact one source with
downstream copies — and that source is **GPL-3.0**, while klm is MIT. `kicad-jlcpcb-tools` is
itself MIT and ships data derived from it, which is their problem to have, not one to inherit.

**The key is wrong.** KiBot's write-up puts it in one sentence: *"you can have two components with
the same footprint and different rotations in the same project."* That is not a gap in the data, it
is a category error in the index. The correct orientation is a property of **how the part sits in
its reel**, which the manufacturer chooses per part number. The land pattern is correlated with it,
often strongly, but does not determine it. A table keyed by footprint pattern cannot express the
disagreement, so it silently returns one of the two answers.

There is a fourth wrinkle worth recording: KiCad mirrors bottom-side components and JLCPCB does
not. That is a systematic transform of the whole bottom layer, not a per-package correction, and it
belongs in the fab profile rather than the table.

The route `kicad-jlcpcb-tools` proposes for getting real data — query the EasyEDA API for a
component's fully-qualified footprint name and derive the rotation from it — is closed to klm
independently: it is an unofficial LCSC endpoint, which [ADR-0009](0009-lcsc-manual-first.md) rules
out.

## Options considered

**A. Bundle the community table anyway.** It is what every other tool does, and it is right often
enough to be useful. Rejected on the licence alone — GPL-3.0 data in an MIT project is not a
trade-off to make quietly — and on the key: shipping a table that is right for most 0603s teaches
the user to trust it, and the case it gets wrong is the one that scraps a board.

**B. Bundle a table klm derives itself** from package geometry and IPC/EIA-481 tape orientation
rules. Attractive, and it is what klm does elsewhere (it generates chip land patterns rather than
copying them). Rejected for now: EIA-481 fixes how a part sits in the tape pocket, not how the
manufacturer chose to orient the die within it, so the derivation is sound for two-terminal chips
and guesswork above that. It is the same line klm already draws at chip land patterns, and the same
reasoning — a value that looks right, passes review, and scraps the board is worse than no value.

**C. Ship nothing and learn.** [Q3](../14-open-questions.md#q3) named this as the fallback: *"if no
usable bundled data exists, klm ships with an empty table and learns from run one."*

## Decision

**Option C, with the key corrected.**

1. **klm bundles no rotation data.** The table starts empty. `source = 'bundled'` stays in the
   schema so a user who has validated a table of their own can load one and have it ranked below
   their learned values, but klm ships none.

2. **A correction is recorded per *part* first, per footprint pattern second.** Resolution runs
   most specific to least: per-part correction → per-part `KLM_FAB_ROTATION` field → user or
   learned pattern → bundled pattern → none. klm can key on the part because it has one — a
   `KLM_ID` and, usually, an LCSC number naming the exact reel. The community tools cannot; they
   have only a footprint name, which is why their table is keyed the way it is.

3. **`klm fab feedback` writes the part-level correction**, and marks the *other* references on the
   board `confirmed` at part level too. Generalising a single observation to a footprint pattern
   is offered (`--generalize`) but not automatic, because one board is one data point about one
   reel.

## Consequences

**Good**
- No licence problem, and nothing to keep in step with an upstream project.
- The table says only what a physical board demonstrated. `confirmed_at` means "this exact part
  came back placed correctly", which is a claim worth having.
- The per-part key is correct rather than approximately correct, and klm is unusually well placed
  to use it — the catalog is the thing the community tools lack.
- Two parts on one footprint needing different rotations is representable, so the case that scraps
  boards is the case klm can express.

**Bad**
- **The first board of any new package is unprotected.** klm has nothing to say about a SOT-23-6 it
  has never seen, where a bundled table would have been right most of the time. That cost is real
  and lands on exactly the user this project is for — a hobbyist whose first run of a package is
  also often their only one. The mitigations are weak: `klm fab` reports which references have no
  confirmed correction so the DFM preview gets a careful look, and nothing more.
- Value accrues slowly, over boards, rather than arriving with the install.
- A user who wants the community table must fetch and load it themselves, and reason about
  GPL-3.0 themselves. klm will document that it exists and decline to ship it.

**Neutral**
- The per-part table needs a migration and a second resolution step. Small.
- If EIA-481-based derivation (option B) is ever done for the packages where it is sound, it slots
  in as another `source` below `learned`, changing nothing else.
