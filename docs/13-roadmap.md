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

**Done when:** ~~a project vendors, is cloned fresh on another machine, opens correctly with no
setup~~ — a project vendors, re-vendors to a zero-byte diff, unvendors back to the byte it started
from, and a collaborator's added part promotes back into the catalog.

The "opens correctly on another machine" half moves to Phase 6, where it belongs and where it can
actually be answered. klm cannot tell `power:GND` — which ships with every KiCad install — from
`Passive:0R_0603` — which is the author's own library — without either a version-dependent list or
a probe of the local environment, and the second is the trap
[ADR-0007](adr/0007-clean-room-verification.md) exists to close. So vendoring claims what it can
prove and reports the rest; `klm verify --clean-room` answers the question by resolving the project
on a machine that has nothing. See [ADR-0010](adr/0010-vendoring-leaves-unmanaged-libraries-linked.md).

One addition the original plan had no answer for: `klm vendor --from-library NICKNAME` is the
adoption path for projects that predate klm and reference their own libraries. Without it the
feature only worked on projects that were klm-linked from the start, which is none of them.

*Solves [P6](01-vision-and-problems.md#p6--global-libraries-and-project-libraries-pull-in-opposite-directions),
the hardest problem. Sequenced here because it depends on stable identity, assets and hashing —
all of which arrive in phases 0–3.*

## Phase 5 — Fabrication

*Goal: fab packages that need no hand-fixing.*

- BOM extraction with variants and DNP
- `kicad-cli` wrappers for gerbers, drill, position, DRC, ERC
- Rotation correction table, ~~bundled starting data~~, application at export
- JLCPCB fab profile; assembly BOM and CPL
- Preflight
- `klm fab feedback` — the learning loop

**Done when:** a board is fabricated and assembled from a `klm fab` package with no manual edits,
and a rotation error recorded once never recurs.

**Bundled rotation data was dropped.** [Q3](14-open-questions.md#q3) found that the one community
table everything else copies is GPL-3.0 (klm is MIT) and is keyed by footprint name, while the
correct orientation is a property of the part's reel — two parts on one land pattern can disagree.
klm records corrections **per part**, which it can do because it has a catalog, and ships none. See
[ADR-0011](adr/0011-rotation-corrections-are-learned-not-bundled.md); the cost is that the first
board of a new package is unprotected, and `klm fab` says so rather than pretending otherwise.

The done-criterion's second half is therefore the one that matters here, and it is the half the
learning loop delivers.

**Verified so far:** the pipeline, preflight, profiles and feedback loop are tested against an
injected `kicad-cli`, and `klm bom` is verified against real boards. What is *not* verified is
`kicad-cli` itself — its exact flags and output — because KiCad is not installed on the development
machine. That is the standing gap for this phase.

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

The first half is done and tested: `klm verify --clean-room --format github` emits an annotation
naming the file and the reference. The second half is written and cannot be *proven* here — KiCad
is not installed on the development machine, so `klm docs` and the export wrappers are exercised
against an injected runner. The first real CI run is what confirms the `kicad-cli` flag surface and
whether `pcb render` still needs `xvfb`; both are the residue of [Q11](14-open-questions.md#q11),
which is otherwise resolved.

Q11 also turned up the thing that would have broken every generated workflow on its first push: the
official KiCad image runs as a non-root user, so a container job needs `options: --user root` or
`actions/checkout` fails on permissions.

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

Verified end to end: plan → cart file → mark-placed → receive → stock → label sheet → scan the
short ID back to the part and its drawer.

**Labels ship without a Data Matrix**, which is a decision rather than an omission —
[Q6](14-open-questions.md#q6) has never been answered and no test here can validate an encoder
without a scanner to read its output. The short ID is readable, resolvable, and the layout reserves
the space. See [10 §7](10-ordering-and-inventory.md#why-there-is-no-barcode-yet).

*Solves [P4](01-vision-and-problems.md#p4--ordering-is-a-spreadsheet-exercise).*

## Phase 8 — Desktop app

*Goal: daily use stops requiring the terminal.*

- FastAPI local API with the job/SSE model
- Desktop shell
- Catalog, part detail, add-part
- Symbol and footprint SVG previews
- Projects and sync screens with diff preview
- Order and inventory screens
- Health dashboard

**Done when:** a full add-part → vendor → order → fab cycle is doable without the CLI.

**The shell is pywebview, not Tauri** ([ADR-0012](adr/0012-pywebview-shell.md) supersedes
[ADR-0005](adr/0005-desktop-shell.md)). Tauri would have meant a Rust toolchain, an npm build, three
CI targets and code-signing certificates — to render views that contain no logic by construction.
pywebview wraps the platform's own webview, ships in the same wheel, and works on Windows, macOS and
Linux from `pip install klm[app]`. The cost is a heavier install than a 5 MB signed binary, and a
Linux box without WebKitGTK falls back to `klm serve`.

**Complete.** The API with the job/SSE model, and the catalog, part detail, add-part, project,
sync-diff, ordering, inventory and health screens. Two things the original list named are
deliberately *not* here:

- **The 3D preview.** A STEP file is a boundary representation, so drawing one means tessellating
  it, and a wrong picture of a 3D model is the failure this project refuses everywhere else. The
  QA gate's bounding-box check catches the real problems; KiCad's viewer is there for the rest.
- **The fab screen.** `klm fab` shells out to KiCad and takes minutes; wrapping it in a window
  before anyone has run the CLI version against a board that came back would be guessing at what
  the screen should say.

Two design decisions the build forced, both recorded in doc 12: the add-part *wizard* collapsed
into one form and a job log — the steps it would have walked through are things klm does, not
things it asks about — and the sync diff shows **two** comparisons rather than the obvious
catalog-versus-vendored one, which is permanently non-empty by design (doc 06 §3).

*Deliberately late. Everything it does already works; this phase makes it pleasant. Building it
earlier would mean building UI against APIs that don't exist yet.*

## Phase 9 — AI research agent

*Goal: finding a part stops being the bottleneck.*

**Q10 was verified first, and moved the starting line.** TME's parametric search is good but keyed
by numeric parameter and value IDs, LCSC has none klm may use, and `search_parametric`'s request
shape was never verified against either API version. So the phase begins in the supplier layer, not
the agent:

- **0. TME v2 parametric search** *(done)* — the `/auth/token` bearer flow,
  `/products/categories/tree`, `scope[]=parameters` discovery, and constraint→value-ID resolution
  through `klm.units`. Nothing above it can be trusted until this is real, and building the agent
  on the guessed shape would mean debugging a model and a request at the same time.

- **Requirement schema and builder** *(done)* — `klm.research.requirement` and `klm research
  check`. Hard constraints and ranking preferences are separate types; an unchecked constraint is
  `unknown` rather than `pass`; numeric constraints are the type the parametric search already
  takes, so a requirement reaches TME without a translation step.
- **Tool definitions with strict schemas** *(done for the four that need nothing later)* —
  `catalog_search`, `supplier_search`, `supplier_get_offer`, `footprint_lookup`, plus `klm
  research tools`. Arguments are validated before a service sees them; the connection is
  read-only. `datasheet_*` and `propose_part` arrive with the datasheet cache and the review
  queue below.
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
