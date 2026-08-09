# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Phases 0–4 of `docs/13-roadmap.md` are implemented: file handling, the store, the catalog and its
git mirror, library generation and KiCad registration, the field schema, value normalization,
`klm lint` and the pre-commit hook, the supplier layer (TME, LCSC, offers, matching, `klm refresh`
/ `klm offers`, lint group P), the asset pipeline (packages, symbol templates, chip land patterns,
KiCad standard-library reuse, the QA gate, FreeCAD mesh→STEP, `klm part add`, `klm assets *`), and
library sync (`klm vendor` / `unvendor`, the lock file, `klm sync status|pull|push|resolve|adopt`,
`klm promote`), fabrication output (`klm bom`, `klm fab`, preflight, fab profiles, the rotation
correction table and `klm fab feedback`), and repository scaffolding and CI (`klm verify
--clean-room`, `klm scaffold`, `klm docs`, `klm report`, timestamp normalisation), and ordering and
inventory (`klm order plan|export|mark-placed|receive|pin`, `klm stock *`, `klm labels *`). **Q1, Q2, Q3, Q10 and Q11 are resolved; Q4 was checked and is
still open, but no longer blocks anything shipped.** **Q8 is resolved too.** Phase 8 (the desktop
app) is complete: the API, the job model, eight screens, an SVG renderer for symbols and
footprints, and the sync diff. Phase 9 (the AI research agent) is next.

- `README.md` — entry point and documentation map
- `docs/01`–`docs/15` — the design, one concern per document
- `docs/adr/` — decision records
- `src/klm/` — the implementation; `tests/` mirrors it
- `agentic_pcb_researcher.md` — the original seed note that started the project. **Superseded**
  by `docs/`; kept for provenance. Do not treat it as current design.

When implementing, `docs/` is the spec. Update it when the design changes rather than letting
code and docs drift.

## Commands

```bash
.venv/bin/python -m pytest                 # the whole suite
.venv/bin/python -m pytest tests/test_units.py -q
.venv/bin/python -m pytest -k "round_trip" # one test by name
.venv/bin/ruff check src tests             # lint (ruff format is *not* enforced)
.venv/bin/mypy src/klm                     # strict, and part of the gate
```

`klm` itself installs into the same venv (`.venv/bin/klm`). `KLM_HOME` points it at a throwaway
catalog, which is how to try a command without touching the real one.

## What is being built

`klm` — a desktop app plus CLI managing the whole lifecycle of parts for KiCad hobby projects:
sourcing from TME and LCSC (the only viable suppliers in Poland — DigiKey/Mouser shipping is
prohibitive), library asset generation, field-schema enforcement, global↔project library sync,
repository scaffolding with CI, JLCPCB fabrication output, multi-project ordering, inventory, and
AI-assisted part research.

Read `docs/01-vision-and-problems.md` first — it defines problems P1–P7, which every other
document references by number.

## Architecture in one paragraph

A plain Python core (`klm.services.*`) holds all business rules. Three thin adapters sit on top:
the CLI, a localhost FastAPI server, and a pre-commit hook. A Tauri desktop shell is a client of
that API and contains no logic. Below the services: `store` (SQLite + content-addressed asset
files), `kicad` (S-expression read/write, library tables), `suppliers` (TME/LCSC adapters), `cad`
(subprocess wrappers for `kicad-cli` and `freecadcmd`), `llm` (Anthropic SDK).

**If the GUI can do something the CLI cannot, that is a bug in the CLI.**

## Non-obvious constraints

These are the things that will bite an implementer who hasn't read the docs.

- **Lossless S-expression round-trip is the hardest rule in the project.** Parse KiCad files to a
  generic tree, mutate only nodes you understand, re-serialize everything else byte-identically.
  A typed parser that drops unknown nodes silently deletes user data on the next KiCad release.
  See `docs/adr/0002`. Generated files klm owns entirely are the one exception — those use a
  canonical writer for byte-stable diffs.
- **`KLM_ID` is load-bearing.** An opaque immutable ID embedded as a hidden field in every
  generated symbol. It is the only thing that makes library sync exact rather than heuristic
  after renames, MPN corrections, and collaborator edits. See `docs/adr/0003`.
- **Part vs Offer is the central modelling split.** A Part is what goes in a schematic; an Offer
  is a supplier-specific orderable item. One part, many offers. Packaging suffixes (`-TR`,
  `-REEL`) belong on the Offer.
- **`cad/scripts/obj2step.py` runs under FreeCAD's own interpreter**, not klm's venv. It can
  `import Mesh, Part` but nothing from klm. Contract is argv in, file out, exit code.
- **The KiCad config path is version-pinned** (`~/.config/kicad/<version>/`). Detect the installed
  version; never hardcode it.
- **klm writes outside its own directory** — KiCad's global config, users' project files. Those
  writes back up first, support `--dry-run`, and merge rather than overwrite (klm touches only
  rows it owns in `sym-lib-table` / `fp-lib-table` / `kicad_common.json`).
- **`klm lint` never touches the network.** It runs in a pre-commit hook and in CI, and a linter
  whose result depends on whether TME is up fails randomly and gets switched off. Staleness (P003)
  is measured against what is stored; fetching is `klm refresh`'s job, explicitly.
- **Refreshing an offer is not re-matching it.** A refresh re-reads price and stock for a link a
  human or a confident match already blessed. Re-adjudicating identity on every refresh would let a
  wrong match appear silently months after the part was approved.
- **A low-confidence match is never stored.** `klm refresh` reports it as a proposal instead — an
  offer in the database is one klm is willing to order against.
- **Timestamps in SQL comparisons need `strftime`, not `datetime`.** klm stamps
  `2026-08-09T12:00:00Z`; SQLite's `datetime()` returns `2026-08-09 12:00:00`. Compared as strings
  those disagree wherever the dates are equal, because `T` sorts after a space. See
  `TIMESTAMP_FORMAT` in `services/offers.py`.
- **klm generates chip land patterns and nothing else.** An 0402 is 1.0 x 0.5 mm by definition, so
  its two rectangles are derivable. A fine-pitch QFN's are not; a reconstructed one would look
  right, pass a visual check, and not solder. Everything else comes from KiCad's libraries or from
  a human.
- **A QA check that cannot run is `unchecked`, never `pass`.** A green report meaning "I didn't
  look" is worse than no report. See `assets/qa.py` — the aggregate status is `unchecked` when
  every result was skipped.
- **Symbols are never shared between parts; footprints and 3D models are.** A symbol carries the
  part's own name and `Value`. Catalog reuse applies to the other two kinds, and that is what keeps
  fifty 0402 resistors from creating fifty footprints.
- **`cad/scripts/obj2step.py` is not on klm's import path on purpose.** It runs under FreeCAD's
  interpreter; putting it under `src/klm` would invite someone to import it.
- **The AI agent has no tool that writes to the catalog.** Architectural, not a prompt
  instruction. It proposes into a review queue; a human approves; the result is still only a
  `draft` that must pass asset QA and lint. See `docs/adr/0006`.
- **Generated output must be byte-stable.** Vendor twice with no changes → `git diff --exit-code`
  passes. Fixed float precision, sorted fields, no incidental timestamps.
- **The global library and a vendored one are built by one function.** `services/library.py`
  `build_library` produces both; `generate` and `vendor` only differ in a `Layout`. Sync compares
  the hash of a vendored asset against the same asset rebuilt from the catalog, so if the two
  builders drifted by so much as a field order, every part would read as `conflict` forever.
- **A vendored asset's hash is deliberately not its catalog hash.** The symbol was renamed and
  re-fielded; the footprint's model path points inside the project. Only the *recorded vs current*
  comparison on each side is meaningful — comparing global against vendored is not.
- **A cached `lib_symbols` unit is named after the bare symbol name**, not `LIB:NAME`. Prefixing it
  with the parent's full new name yields `NewLib:Part_Part_1_1`, which KiCad loads and renders as
  nothing. Found on a real schematic, not a fixture; `schematic._rename_cached` is where it lives.
- **Vendoring aborts only on klm's own unresolved symbols.** A `lib_id` from a library klm does not
  manage (`power:GND`, `Device:R`) is reported and left linked, because one real schematic carries
  dozens and aborting would push every user to `--allow-unresolved`. The consequence is that
  `klm vendor` does *not* establish self-containment — `klm verify --clean-room` does. See
  `docs/adr/0010`.
- **klm bundles no pick-and-place rotation data, on purpose.** The community table is GPL-3.0 and
  keyed by footprint name, but the correct orientation belongs to the part's reel — two parts on
  one 0603 land pattern can differ. Corrections are recorded **per part** and learned from boards
  that came back. See `docs/adr/0011`. Do not add a bundled table.
- **`klm bom` must not need KiCad or a catalog.** It reads `.kicad_sch` directly so CI, the
  clean-room check and cost estimates all work without `kicad-cli`. Everything else in the fab
  pipeline does shell out, because gerbers need KiCad's own plotter.
- **The fab manifest records a `klm_id` for every placement.** `klm fab feedback` maps a reference
  back to a part from there, not from the schematic — by the time a board returns, the schematic
  has usually moved on.
- **A fab package is written only if preflight passed.** A package that exists is one somebody will
  upload, so a half-checked one is worse than none. `--check` is the same code path, writing nothing.
- **The API translates, it never computes.** Every route in `klm/api/server.py` calls one
  `klm.services.*` function and shapes the result. The moment a route does arithmetic, the GUI and
  the CLI start disagreeing about what klm does. A test asserts the part payload matches the
  service's own object.
- **The desktop shell is pywebview, not Tauri** (`docs/adr/0012` supersedes `0005`). One wheel, three
  platforms, no Rust or npm. `klm app` opens a window; a machine with no usable webview degrades to
  `klm serve` rather than raising.
- **Releases are built on runners, never locally.** PyInstaller does not cross-compile — it
  packages the interpreter's own binaries — so `.github/workflows/release.yml` runs one job per
  platform. It starts the frozen server and fetches `/api/health` and `/static/app.js` before
  packaging, because **uvicorn resolves its protocol implementations by name**: a build with a
  missing hidden import starts fine and dies on the first request, which no import check finds.
- **`klm doctor` exits 1 when a tool is missing**, by design (0 ok, 1 check failed, 2 klm errored).
  CI smoke tests must tolerate 1 and fail only on ≥2, or the release job is red on every run.
- **README screenshots are generated from the running app** (`tools/screenshots.py`: seed a demo
  catalog → `klm serve` → Playwright → framed PNGs in `docs/images/`). Never hand-draw a mock-up
  of a screen; a README showing a UI the code does not produce is a lie with a long half-life.
  They are committed rather than built in CI, because a README whose images come from a workflow
  is broken on every fork. Two real UI bugs have been caught by looking at the output.
- **`packaging/klm.ico` is generated, not committed.** One source image
  (`api/static/logo.png`); `make_icons.py` derives the rest. The tab favicon is a *separate,
  hand-drawn* `static/favicon.svg` — at 16 px the detailed artwork averages to a green square.
- **A job's terminal state is assigned last.** A watcher stops as soon as it sees `done`/`failed`,
  so setting the state before recording the traceback lets it stop mid-write.
- **Previews are rendered by klm, not `kicad-cli`.** `kicad/render.py` draws symbols and
  footprints from the S-expression, because the machine with no KiCad is exactly the one
  reviewing a part it has not seen. It is a *preview, not a plot*: unknown shapes are skipped
  rather than approximated, and only a fixed set of layers is drawn. **STEP is not rendered at
  all** — a wrong picture of a 3D model is the failure this project refuses everywhere else.
- **Symbol space is Y-up; footprint space is Y-down.** Only the symbol renderer negates y. A
  missed flip renders a legible, plausible, mirrored symbol with pin 1 in the wrong corner.
- **A pin's `at` is its connection point and its angle points *towards the body*.** Reversing it
  draws every pin inside the outline, which still looks like a resistor.
- **The sync diff never compares the catalog asset against the vendored one.** They differ
  permanently by design, so that diff never empties. Each side is compared against *its own*
  recorded state. The project side's "before" is rebuilt through `build_library` and only shown
  if it reproduces the recorded hash — otherwise it is a guess, and a guessed diff invites
  someone to resolve a conflict that is not there. See `services/sync.py` `diff_part`.
- **`klm part add` refuses without a manufacturer.** manufacturer + MPN is the catalog's
  uniqueness key and what `find_by_mpn` needs, so a part without one is unaddressable rather than
  half-filled. It used to raise `CatalogError` from three layers down.
- **Vendoring stamps `KLM_ID` onto schematic instances.** KiCad copies library fields onto an
  instance at placement, so klm-built boards carry it already — but an adopted board does not, and
  without the stamp the BOM, ordering and cost all see an empty project.
- **Supplier splitting needs *set* moves, not just single-line moves.** Crossing a free-shipping
  threshold requires several lines to move together, and every intermediate state costs more than
  either end — a one-line-at-a-time hill-climber sits in that valley and reports the greedy answer.
- **Set `vat_rate` the same way on every supplier.** Omitting it on one tilts the landed-cost
  comparison by the whole rate, which is the decision the number exists to inform.
- **Receiving is the only operation that increments stock**, and only a genuine count stamps
  `last_counted`. Ordering discounts an old count rather than trusting or ignoring it.
- **Labels carry no barcode on purpose** (Q6): no scanner here can validate an ECC200 encoder, and
  a barcode that passes a visual check and does not scan is the failure mode this project refuses
  everywhere else. Do not add one without hardware to test against.
- **`klm verify --clean-room` takes a project and nothing else — no connection, no config, no
  `Paths`.** The signature is the guarantee, and a test asserts it. A checker that could reach the
  catalog would pass on the one machine where passing means nothing. It deliberately does *not* run
  ERC/DRC either, so it works in a pre-commit hook on a machine with no KiCad; `klm fab --check`
  owns those.
- **"A machine that has nothing" still has KiCad.** Stock libraries ship with KiCad, so `power:GND`
  legitimately resolves in CI (the workflow pins the `-full` image). Verification answers this by
  *trying to resolve*, never by a list of library names.
- **Generated workflows need `options: --user root`.** The official KiCad image ends with
  `USER kicad`; without it `actions/checkout` fails on permissions. See `docs/14` Q11.
- **KiCad ignores `SOURCE_DATE_EPOCH`.** Gerber timestamps are rewritten by klm after export, and
  *replaced* with a fixed valid value rather than blanked — a well-formed field carries no parser
  risk. It must happen before the gerbers are zipped.
- **`klm verify --clean-room` must run with no catalog and no configuration.** It is a separate
  code path from `klm lint` (which assumes the catalog), because its whole job is to behave like a
  stranger's machine. Self-containment is defined by that check passing in CI, not by the project
  opening locally — your machine has the global libraries registered and will silently resolve a
  half-vendored project. See `docs/adr/0007`.

## Before building anything

`docs/14-open-questions.md` is the risk register. Q1, Q2, Q3, Q8, Q10 and Q11 are resolved
(2026-08-09). Q4–Q7, Q9 and Q12 remain; none blocks Phase 9.

- **Q10 — resolved, and it changed Phase 9.** TME v2 has real parametric search
  (`/products/search` with `parameters[n][id]` + `values[]`, `scope[]=parameters` for discovery),
  but it filters by **numeric parameter and value IDs**, not names or comparisons. So klm resolves
  a human constraint to IDs and **the agent never sees one** — a wrong parameter ID returns
  confidently wrong parts rather than an error, which is the invented-MPN failure again. LCSC has
  no search klm may use at all (ADR-0009), so `supplier_search` is TME-only. The DigiKey/Mouser
  parametric-index fallback is dropped: it was for a weak TME, and TME is not weak.
- **TME v1 vs v2 — a live risk, found while answering Q10.** klm implements v1 (HMAC-SHA1
  signature, `Products/Action`). v2 is OAuth2 `client_credentials` with a **300-second** bearer
  token and REST paths. Both answer today; TME calls v1 deprecated. `search_parametric`'s request
  shape was never verified against either and must be rebuilt on v2 *before* the agent stands on
  it. See the Q1 addendum.

- **Q1 — resolved.** TME's auth is signature-based, not OAuth: HMAC-SHA1 over
  `POST&<enc URL>&<enc sorted query>`, base64, sent as `ApiSignature`. Still unverified: published
  rate limits, and whether the v2 API differs — `developers.tme.eu` needs a login to check.
- **Q2 — resolved, and it changed the design.** LCSC's official API is granted per *company*, and
  its terms forbid redistributing the data or the documentation. klm's users won't get it, so
  **manual entry is the primary LCSC path** and klm ships no unofficial-endpoint client in any
  mode. See `docs/adr/0009`. Do not add one.
- **Q4 — checked, still open, and the finding is that there is no finding.** Nothing published
  addresses redistribution of EasyEDA-derived library assets. An absent answer is not a permissive
  one, so **the EasyEDA importer was dropped from Phase 3** rather than written and disabled —
  ADR-0009 independently rules it out anyway. Do not add it without answering Q4 first. It stopped
  blocking Phase 4 for the same reason: vendoring copies catalog assets into a public repository,
  but there are no unclear-status assets to copy — everything klm holds comes from KiCad's
  libraries or its own generators.

## Roadmap

`docs/13-roadmap.md` has the full sequencing and the reasoning for it. Short version: file-safety
foundations → field schema → sourcing → assets → sync → fab → ordering → desktop app → AI agent.
The agent is last deliberately: its most valuable tool is `catalog_search`, which needs a good
catalog to search.
