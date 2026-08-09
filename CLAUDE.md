# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Greenfield, design phase. No source tree, no `pyproject.toml`, no tests, no build/lint commands,
and not yet a git repository. The repository currently contains only documentation.

- `README.md` — entry point and documentation map
- `docs/01`–`docs/15` — the design, one concern per document
- `docs/adr/` — decision records
- `agentic_pcb_researcher.md` — the original seed note that started the project. **Superseded**
  by `docs/`; kept for provenance. Do not treat it as current design.

When implementing, `docs/` is the spec. Update it when the design changes rather than letting
code and docs drift.

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
- **The AI agent has no tool that writes to the catalog.** Architectural, not a prompt
  instruction. It proposes into a review queue; a human approves; the result is still only a
  `draft` that must pass asset QA and lint. See `docs/adr/0006`.
- **Generated output must be byte-stable.** Vendor twice with no changes → `git diff --exit-code`
  passes. Fixed float precision, sorted fields, no incidental timestamps.
- **`klm verify --clean-room` must run with no catalog and no configuration.** It is a separate
  code path from `klm lint` (which assumes the catalog), because its whole job is to behave like a
  stranger's machine. Self-containment is defined by that check passing in CI, not by the project
  opening locally — your machine has the global libraries registered and will silently resolve a
  half-vendored project. See `docs/adr/0007`.

## Before building anything

`docs/14-open-questions.md` is the risk register. Two items block real work:

- **Q1** — TME's auth is signature-based, not the OAuth 2.0 the original note claimed. Verify
  before writing the adapter.
- **Q2** — LCSC has no official public API. This is the highest risk in the project. Verify the
  terms before building against unofficial endpoints; manual entry is designed as a first-class
  fallback precisely because this may not work out.

## Roadmap

`docs/13-roadmap.md` has the full sequencing and the reasoning for it. Short version: file-safety
foundations → field schema → sourcing → assets → sync → fab → ordering → desktop app → AI agent.
The agent is last deliberately: its most valuable tool is `catalog_search`, which needs a good
catalog to search.
