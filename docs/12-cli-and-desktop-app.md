# 12 — CLI and Desktop App

## 1. CLI first, and why

Every capability is a CLI command over the service layer. The desktop app is a client of the same
API. This ordering is a deliberate constraint, not an accident of build order:

- **Testable.** A CLI command is a function call with a string in and a string out. GUI logic is not.
- **Scriptable.** Batch operations, CI hooks and one-off fixes need a command line.
- **Composable.** `klm bom --format json | jq …` solves problems no one anticipated.
- **Survivable.** If the desktop shell is abandoned or rewritten, nothing is lost.

The rule: if the GUI can do something the CLI cannot, that is a bug in the CLI.

## 2. Command surface

```
klm init                         Set up the catalog, register with KiCad
klm doctor                       Check external tools, config, KiCad registration

PARTS
klm part add --lcsc C8734 | --mpn X --mfr Y | --interactive
klm part show <klm_id|mpn>
klm part search <query> [--category …] [--in-stock] [--supplier tme]
klm part edit <klm_id>
klm part approve <klm_id>
klm part deprecate <klm_id> [--successor <klm_id>]
klm part merge <from> <into>

ASSETS
klm assets acquire <klm_id>
klm assets qa <klm_id>
klm assets convert-3d <file.obj>
klm assets reuse-check

CATALOG
klm lint [--project .] [--fix] [--select …] [--format json]
klm generate                     Rebuild generated/ from the catalog
klm export | klm import          SQLite ↔ YAML mirror
klm register [--check]           KiCad lib tables + env vars

SOURCING
klm refresh [--stale 7d] [--part ID|MPN] [--supplier tme] [--no-discover] [--offline]
klm offers [ID|MPN] [--supplier lcsc] [--qty 100] [--format json]
klm offers <ID|MPN> --supplier lcsc --add C8734 --price 1.42 --stock 4200
klm offers --supplier lcsc --remove C8734

SYNC
klm vendor [--with-3d] [--dry-run]
klm unvendor
klm sync status [--exit-code]
klm sync pull | push | resolve | adopt
klm promote <symbol-or-klm_id>

BUILD & FAB
klm bom --project . [--variant v] [--format csv|json]
klm fab [--variant v] [--check] [--no-assembly] [--normalize-timestamps]
klm fab feedback <dir> --wrong U3:180 --confirm-rest
klm fab corrections list|set

REPOSITORY & CI                                     see doc 15
klm scaffold [--preset publish|private] [--check] [--update]
klm verify --clean-room [--format json|github]      no catalog required
klm docs --pdf --render --bom --out DIR
klm report --format github-summary|markdown|json

ORDERING & STOCK
klm order plan --build "5×sensor:full,2×psu:basic"
klm order export <supplier>
klm order mark-placed | receive
klm stock list|adjust|consume|where
klm labels print [--order …|--location …|--klm-id …]

AGENT
klm research --spec req.yaml [--interactive]
klm research review               Work the proposal queue
klm ask <klm_id> "<question>"     Datasheet Q&A

COST
klm cost project <name> --qty N
klm cost history
```

Conventions applied uniformly:

- `--dry-run` on everything that writes; it is the *default* for destructive operations in the GUI.
- `--format json` on everything that outputs data.
- `--exit-code` on everything checkable, for CI.
- Exit codes: `0` success, `1` the checked condition failed, `2` klm errored.
- `KLM_HOME` overrides the catalog location; `--catalog` overrides per-invocation.

## 3. The local API

FastAPI over localhost, bound to `127.0.0.1` with a token generated at startup and handed to the
shell — so a stray browser tab can't drive the user's library.

```
GET    /parts?q=&category=&status=
GET    /parts/{klm_id}
POST   /parts                      → draft
PATCH  /parts/{klm_id}
POST   /parts/{klm_id}/approve

GET    /projects
POST   /projects/{id}/vendor
GET    /projects/{id}/sync-status

POST   /jobs/refresh               → job id
POST   /jobs/acquire-assets
POST   /jobs/research
GET    /jobs/{id}                  → status, progress, result
GET    /jobs/{id}/events           → SSE stream
```

Long operations are jobs with an SSE event stream, because asset acquisition and agent research
take tens of seconds to minutes and a progress-less UI for that is unusable.

## 4. Desktop app

**Tauri shell + web UI + Python sidecar.** The shell is a thin window around a local web UI that
talks to the FastAPI process. See [ADR-0005](adr/0005-desktop-shell.md) for the alternatives
considered (PySide6, Electron, browser-only).

### Screens

| Screen | Purpose |
|---|---|
| **Catalog** | Searchable, filterable part table. Columns configurable; filters on stock, supplier, category, lint status. The screen you live in. |
| **Part detail** | Everything about one part: fields, parameters with their sources, symbol and footprint previews, 3D preview, offers with price-break table, stock, where it's used. |
| **Add part** | The acquisition wizard — identify → offers → assets → QA report → review → approve. Each step shows what klm found and lets you correct it. |
| **Research** | Requirement builder, live agent stream, ranked candidates with constraint checks, approve/edit/reject. |
| **Projects** | Registered projects, their mode, sync status at a glance. Vendor/sync actions with diff preview. |
| **Sync detail** | Per-part drift, side-by-side diffs of global vs vendored assets, conflict resolution. |
| **Order** | Build plan → demand → supplier split, with the reasoning shown per line and the ability to pin lines. Cart export. |
| **Inventory** | Stock by location, low-stock list, count-entry mode, label printing. |
| **Fab** | Preflight checklist, package generation, feedback entry after a run. |
| **Health** | Catalog-wide lint results, stale offers, QA failures, duplicate candidates. |

### Design principles

- **Show the diff before writing.** Every mutating action previews exactly what will change on
  disk. This is non-negotiable for the sync screens.
- **Long jobs never block.** Progress, cancellable, results delivered to a queue.
- **Provenance is always one click away.** Any parameter shows its source; any agent-derived
  value is visually marked as such.
- **Offline-honest.** Stale data is labelled with its age rather than presented as current.
- **Keyboard-first.** Command palette, `/` to search, `j`/`k` in tables — this is a tool used
  daily by one person, not a consumer app.

### Previews

Symbol, footprint and 3D previews are genuinely useful for review and non-trivial to build:

- **Symbol and footprint**: render the S-expression to SVG directly. klm already parses these
  fully; a 2D renderer for the subset that appears in symbols and footprints is a contained
  amount of work and avoids depending on KiCad being installed for a preview.
- **3D**: render the STEP via a small three.js viewer after conversion to a web-friendly mesh.
  Lower priority — the QA gate's bounding-box check catches most real problems without a picture.

## 5. Packaging

| Target | Approach |
|---|---|
| Linux | AppImage or a `.deb`; also `pipx install klm` for the CLI alone |
| Windows | Tauri MSI bundling the Python sidecar |
| macOS | Not targeted initially |

External tools (`kicad-cli`, `freecadcmd`) are **not** bundled — they're large and already
installed by anyone who needs them. `klm doctor` reports what's missing and what each missing
tool disables:

```
$ klm doctor
✓ kicad-cli      9.0.1     /usr/bin/kicad-cli
✗ freecadcmd     not found
    → 3D mesh→STEP conversion unavailable. Models will be stored as meshes.
    → install: apt install freecad
✓ KiCad config   9.0       ~/.config/kicad/9.0
✓ KLM registered sym-lib-table, fp-lib-table, KLM_LIBS, KLM_3DMODELS
⚠ Anthropic API  no key
    → agent features unavailable. Set ANTHROPIC_API_KEY.
✓ catalog        912 parts, 847 approved, 3 drafts
⚠ offers         214 parts have offers older than 30 days  (klm refresh --stale 30d)
```

Every degradation is named, with its consequence and its fix. klm should never fail with a stack
trace because an optional tool is absent.
