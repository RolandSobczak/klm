# 13 — Roadmap

Ordered by dependency and by how much pain each phase removes per unit of work. The bias
throughout: **build the thing that makes the next thing possible, and get value at the end of
every phase.**

## Phase 0 — Foundations

*Goal: a catalog exists and KiCad can see it.*

- S-expression reader/writer with the lossless round-trip guarantee, plus a canonical formatter
- Property tests: parse→write is byte-identical on a corpus of real KiCad files
- SQLite schema, migrations, YAML export/import with the determinism test
- Content-addressed asset store
- `klm init`, `doctor`, `generate`, `register`
- Import an existing `.kicad_sym` into the catalog as drafts

**Done when:** an existing library imports, generates, registers, and opens correctly in KiCad.

*This phase has no user-visible magic and is the most important. Everything after it assumes
file handling is trustworthy.*

## Phase 1 — Schema and hygiene

*Goal: field-name drift stops being a problem.*

- Canonical field schema, alias map, config
- Value parser/formatter with round-trip property tests
- `klm lint` with the S, V, A rule groups; `--fix`
- Pre-commit hook
- Category taxonomy

**Done when:** the imported library lints clean after `--fix` plus a bounded amount of manual work.

*Solves [P3](01-vision-and-problems.md#p3--field-names-drift-so-boms-never-line-up) on its own.
Deliberately early — every later phase produces data, and it should be produced correctly.*

## Phase 2 — Sourcing

*Goal: know what's buyable, without a browser.*

- Supplier adapter protocol
- TME adapter (auth verified first — [Q1](14-open-questions.md#q1))
- LCSC adapter, plus the manual-entry fallback
- Offer model, HTTP cache, rate limiting, circuit breaker
- MPN↔offer matching with confidence
- `klm refresh`, `klm offers`, lint rule group P

**Done when:** ~~every part in the catalog shows live stock and price from both suppliers~~ — every
part shows stock and price from TME live, and from LCSC as entered.

The original criterion assumed live LCSC data, which [Q2](14-open-questions.md#q2) established klm's
users cannot have: LCSC grants API access per company, not per person. Manual entry is therefore
the primary LCSC path ([ADR-0009](adr/0009-lcsc-manual-first.md)), and a manually entered offer is
a first-class offer — it ages, lints and orders exactly like a fetched one.

## Phase 3 — Asset pipeline

*Goal: adding a part stops being an afternoon.*

- Template generation for passives and standard packages
- KiCad standard-library lookup and reuse
- ~~EasyEDA import~~ — **dropped**, see below
- FreeCAD mesh→STEP conversion
- The QA gate — all three check groups
- `klm part add`, `klm assets *`

**Done when:** ~~`klm part add --lcsc C8734` produces an approvable part~~ —
`klm part add --mpn … --package 0402 --category Passive/Resistor` produces an approvable part with
symbol and footprint, and a STEP model wherever KiCad's libraries carry one.

Two changes, both consequences of decisions taken earlier:

- **EasyEDA import is dropped, not deferred.** [ADR-0009](adr/0009-lcsc-manual-first.md) rules out
  clients against LCSC's unofficial endpoints, and the EasyEDA component API is one.
  [Q4](14-open-questions.md#q4) — redistribution of EasyEDA-derived assets — was checked and is
  still unanswered. Either reason alone was sufficient.
- **`--lcsc C8734` cannot be the entry point** without EasyEDA, because nothing maps an LCSC number
  to an MPN offline. It remains a flag that *records* the number as an offer.

What this costs is narrower than it looks: sources 1–3 (catalog reuse, KiCad's libraries, klm's own
generators) cover passives, chip packages and everything KiCad already carries — the bulk of a
hobby library. What is lost is the long tail of LCSC-only parts, which now needs a hand-drawn
symbol and `klm import --from-kicad`.

*Solves [P2](01-vision-and-problems.md#p2--library-assets-are-a-chore-per-part).*

## Phase 4 — Library sync

*Goal: publish a project without maintaining parts twice.*

- `klm vendor` / `unvendor` with reference rewriting
- Lock file, three-way hash comparison
- `klm sync status` / `pull` / `push` / `resolve` / `adopt`
- `klm promote`
- Determinism test: vendor twice, zero diff

**Done when:** a project vendors, is cloned fresh on another machine, opens correctly with no
setup, and a collaborator's added part promotes back into the catalog.

*Solves [P6](01-vision-and-problems.md#p6--global-libraries-and-project-libraries-pull-in-opposite-directions),
the hardest problem. Sequenced here because it depends on stable identity, assets and hashing —
all of which arrive in phases 0–3.*

## Phase 5 — Fabrication

*Goal: fab packages that need no hand-fixing.*

- BOM extraction with variants and DNP
- `kicad-cli` wrappers for gerbers, drill, position, DRC, ERC
- Rotation correction table, bundled starting data, application at export
- JLCPCB fab profile; assembly BOM and CPL
- Preflight
- `klm fab feedback` — the learning loop

**Done when:** a board is fabricated and assembled from a `klm fab` package with no manual edits,
and a rotation error recorded once never recurs.

*Solves [P5](01-vision-and-problems.md#p5--fabrication-output-needs-hand-fixing).*

## Phase 6 — Repository scaffolding and CI

*Goal: a published project proves it opens for someone else, and publishes its own artifacts.*

- `klm verify --clean-room` — resolution, path and lock checks with **no catalog and no config**
- `klm scaffold` — workflows, `.gitignore`, `.gitattributes`, `klm.toml`, README template
- `klm docs` (schematic PDF, board render, STEP) and `klm report --format github-summary`
- `klm fab --normalize-timestamps` for byte-reproducible fab output
- Verify / artifacts / release workflows, matrixed over variants
- `--format github` annotations so findings land on the right line in a PR

**Done when:** a PR that half-vendors a project fails CI with the offending `lib_id` annotated in
the diff, and a merge to `main` produces a downloadable fab package and schematic PDF.

*Depends on phases 4 and 5 — it has nothing to verify until vendoring exists, and nothing to
publish until fab output does. Sequenced immediately after them because the clean-room check is
what makes the phase-4 guarantee real rather than assumed. See
[ADR-0007](adr/0007-clean-room-verification.md).*

## Phase 7 — Ordering and inventory

*Goal: no more spreadsheets.*

- Build plan → demand → spares → order quantities
- Supplier split: greedy seed plus local search over shipping thresholds
- Cart export per supplier
- Order lifecycle, receiving
- Stock tracking, locations
- Label generation (Data Matrix + text, PDF and PNG)
- `klm cost *`

**Done when:** a multi-project order goes from build plan to two submitted carts in under five
minutes, and arriving parts get labelled and stocked in one pass.

*Solves [P4](01-vision-and-problems.md#p4--ordering-is-a-spreadsheet-exercise).*

## Phase 8 — Desktop app

*Goal: daily use stops requiring the terminal.*

- FastAPI local API with the job/SSE model
- Tauri shell
- Catalog, part detail, add-part wizard
- Symbol and footprint SVG previews
- Projects and sync screens with diff preview
- Order and inventory screens
- Health dashboard

**Done when:** a full add-part → vendor → order → fab cycle is doable without the CLI.

*Deliberately late. Everything it does already works; this phase makes it pleasant. Building it
earlier would mean building UI against APIs that don't exist yet.*

## Phase 9 — AI research agent

*Goal: finding a part stops being the bottleneck.*

- Requirement schema and builder
- Tool definitions with strict schemas
- Research agent on the Anthropic tool runner
- Datasheet fetch, cache, and cited parameter extraction
- Proposal queue and review UI
- Guardrails, logging, cost metering
- Datasheet Q&A; substitute finding

**Done when:** a stated requirement yields three cited, in-stock candidates, and approving one
runs the phase-3 pipeline to a reviewable draft part.

*Solves [P7](01-vision-and-problems.md#p7--finding-the-right-part-in-the-first-place). Last
because it's the phase that most benefits from everything else existing: the agent's best tool is
`catalog_search`, which needs a good catalog; its proposals flow into the phase-3 pipeline; its
value is verified by the phase-2 offer data. Built first, it would be an impressive demo
attached to nothing.*

## Later, unscheduled

- Substitute management with mechanical pin-compatibility checking
- Multi-board projects (panelization, motherboard/daughterboard sets)
- Additional supplier adapters (Mouser/DigiKey as parametric search only; Botland, Kamami)
- KiCad IPC API integration — push a part into a running KiCad session ([Q5](14-open-questions.md#q5))
- Assembly instructions and per-build checklists
- Cost/BOM regression tracking in CI

## Sequencing rationale, in one line each

- **0 before everything**: file safety is the foundation; a corruption bug found in phase 6 is
  catastrophic and unfindable.
- **1 before 2**: data arriving from suppliers should land in a schema that's already enforced.
- **3 before 4**: sync needs stable, hashed, deduplicated assets to compare.
- **4 and 5 before 6**: CI has nothing to verify until vendoring exists and nothing to publish
  until fab output does.
- **4 before 7**: ordering across projects needs projects to be reliably resolvable to catalog parts.
- **8 after 7**: build the UI once the workflows are settled, not while they're moving.
- **9 last**: the agent's value scales with the quality of everything it queries.
