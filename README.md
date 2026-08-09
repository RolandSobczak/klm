# klm — KiCad Library Manager

A desktop application for hobby and study PCB projects that turns "find a part, draw it,
buy it, fab it" from a day of manual work into a reviewed, mostly-automatic pipeline.

**Status: design phase.** No code yet. This repository currently contains the design
documentation. Read [`docs/01-vision-and-problems.md`](docs/01-vision-and-problems.md) first.

## The problem, in one paragraph

Sourcing parts in Poland means TME and LCSC — DigiKey and Mouser shipping (~80 PLN) makes them
unusable for a hobby order. So every part has to be checked for local availability by hand.
Once a part is chosen, its symbol, footprint and 3D model have to be made or scavenged, and no
matter how careful you are the field names drift (`MPN` here, `Manufacturer_Part_Number` there),
so BOMs never line up. Ordering means hand-built spreadsheets. Fabrication means hand-fixing
JLCPCB's rotation quirks. And because every feature gets prototyped on its own little board
before being folded into the big project, parts live in *global* libraries — which makes any
repository you push to GitHub incomplete for a collaborator, so you end up maintaining the same
part twice.

## What klm does about it

| Pain | What klm does |
|---|---|
| Local availability | One catalog of parts, each with live TME/LCSC offers, stock and price breaks |
| Asset drudgery | Automated symbol + footprint + STEP acquisition, with a QA gate before anything lands |
| Field-name drift | One canonical field schema, enforced by a linter with `--fix` |
| Order spreadsheets | Multi-project BOM consolidation → per-supplier carts, MOQ and shipping aware |
| JLCPCB output | Gerbers, drill, BOM and CPL with rotation/offset corrections applied and *learned* |
| Global ↔ project libs | `klm vendor` copies used parts into the project and rewrites references; `klm promote` goes the other way; a lock file tracks drift |
| "Will it open for someone else?" | `klm scaffold` generates CI that verifies self-containment on a clean checkout and publishes PDFs, BOMs and fab zips on merge |
| Finding parts at all | An AI research agent that searches, reads datasheets, and proposes a part for you to approve |

## Documentation map

| Document | What's in it |
|---|---|
| [01 — Vision and problems](docs/01-vision-and-problems.md) | Problem statement, goals, non-goals, success criteria |
| [02 — Domain model](docs/02-domain-model.md) | Part, Offer, Asset, Project, BOM, Order, Stock — and part identity |
| [03 — Architecture](docs/03-architecture.md) | Layers, processes, the lossless S-expression rule, data flow |
| [04 — Catalog and storage](docs/04-catalog-and-storage.md) | SQLite schema, git-versioned export, generated `.kicad_sym` |
| [05 — Field schema and linting](docs/05-field-schema-and-linting.md) | Canonical fields, aliases, value normalization, lint rules |
| [06 — Library sync](docs/06-library-sync.md) | The flagship problem: vendoring, promotion, drift, conflicts |
| [07 — Supplier integration](docs/07-supplier-integration.md) | TME and LCSC adapters, caching, the offer model, legal caveats |
| [08 — Asset pipeline](docs/08-asset-pipeline.md) | Symbol/footprint/3D acquisition, FreeCAD conversion, QA checks |
| [09 — Manufacturing outputs](docs/09-manufacturing-outputs.md) | Gerbers, drill, BOM, CPL, rotation corrections, fab feedback |
| [10 — Ordering and inventory](docs/10-ordering-and-inventory.md) | Demand aggregation, supplier split, stock, drawer labels |
| [11 — AI research agent](docs/11-ai-research-agent.md) | Requirement spec, tools, guardrails, provenance, approval |
| [12 — CLI and desktop app](docs/12-cli-and-desktop-app.md) | Command surface, screens, the local API |
| [13 — Roadmap](docs/13-roadmap.md) | Phased delivery, what to build first and why |
| [14 — Open questions](docs/14-open-questions.md) | Everything that needs verifying before it's built on |
| [15 — Scaffolding and CI/CD](docs/15-project-scaffolding-and-ci.md) | Repo starter, clean-room verification, artifact workflows |
| [ADRs](docs/adr/) | Decision records with the alternatives that were rejected |

## Naming

The tool is `klm`. The Python package is `klm`. The global library set it generates is
`KLM` (symbols `KLM:<name>`, footprints `KLM:<name>`).
