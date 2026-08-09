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

PARTS AND ASSETS
klm part add --mpn … --mfr … --category … --package … [--value …] [--field N=V] [--lcsc C…]
klm assets acquire <id|mpn> [--overwrite]
klm assets qa [<id|mpn>] [--format json]
klm assets convert-3d <mesh> [--part <id|mpn>] [--tolerance 0.1]
klm assets reuse-check

CATALOG
klm lint [--project .] [--fix] [--select …] [--format json]
klm generate                     Rebuild generated/ from the catalog
klm render <ID_OR_MPN> [--footprint] [--output FILE]   SVG; no KiCad needed
klm export | klm import          SQLite ↔ YAML mirror
klm register [--check]           KiCad lib tables + env vars

SOURCING
klm refresh [--stale 7d] [--part ID|MPN] [--supplier tme] [--no-discover] [--offline]
klm offers [ID|MPN] [--supplier lcsc] [--qty 100] [--format json]
klm offers <ID|MPN> --supplier lcsc --add C8734 --price 1.42 --stock 4200
klm offers --supplier lcsc --remove C8734

SYNC                                                every command takes --project DIR
klm vendor [--with-3d] [--dry-run] [--name N] [--from-library NICK] [--strict]
           [--allow-unresolved] [--no-timestamp]
klm unvendor [--dry-run] [--force]
klm sync status [--exit-code] [--format json]
klm sync diff <PART> [--side both|catalog|project] [--format text|json]
klm sync pull [PART …] [--strategy prefer-global] [--dry-run]
klm sync push [PART …] [--strategy prefer-project] [--dry-run]
klm sync resolve [--strategy prefer-global|prefer-project]
klm sync adopt [--name N] [--dry-run]
klm promote <symbol|mpn|klm_id> [--category PATH]

BUILD & FAB                                         every command takes --project DIR
klm bom [--variant v] [--format text|csv|json]      no KiCad, no catalog needed
klm fab [--variant v] [--profile jlcpcb|generic] [--output DIR]
        [--check] [--no-assembly] [--allow-dirty] [--no-timestamp]
klm fab feedback <dir> --wrong U3:180 [--confirm-rest] [--generalize]
klm fab corrections list | set <pattern> <deg> [--source user] | remove <pattern>

DESKTOP APP                                         see docs/adr/0012
klm app [--port N] [--serve]      native window; falls back to the browser
klm serve [--port N]              URL only, for SSH and containers

REPOSITORY & CI                                     see doc 15 · all take --project DIR
klm scaffold [--preset publish|private] [--check] [--update]
klm verify --clean-room [--format text|json|github] [--require-3d]   no catalog required
klm docs [--all | --pdf --render --step] [--output DIR]
klm report [--variant v] [--package DIR] [--format markdown|github-summary|json]
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
klm order plan --build "5xsensor-board:full,2xpsu-board" [--projects DIR] [--explain] [--save]
klm order export <supplier> --order ID [--output DIR]
klm order list [--state draft|placed|received] | mark-placed ID [--total N]
klm order receive ID [--location "A/12"] [--partial PN:QTY ...]
klm order pin <ID_OR_MPN> [--supplier tme]

klm stock list [--location "A/*"] [--low] | where <ID_OR_MPN>
klm stock adjust <ID_OR_MPN> --location "A/12" [--set N | --delta N]
klm stock consume --build "1xsensor-board:full" | threshold <ID_OR_MPN> N

klm labels print [--order ID] [--location GLOB] [--part ID_OR_MPN] [--format pdf|png]
klm labels scan <SHORTID>

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

FastAPI, bound to `127.0.0.1` and started by the user's own session. **No authentication**: a
login on a single-user local tool is security theatre, and the thing that would actually be a
mistake — binding to an address something else can reach — is what the host constant refuses.

Every route is a *translation* of one `klm.services.*` function: read the arguments, call the
service, shape the result. The moment a route does arithmetic, the GUI and the CLI start
disagreeing about what klm does; a test asserts the part payload is the service's own object.

```
GET    /api/health                          tools, catalog, part counts
GET    /api/parts?q=&status=
GET    /api/parts/{klm_id}                  + offers, stock
POST   /api/parts                           → job; the add-part wizard
POST   /api/parts/{klm_id}/status           approve / deprecate
GET    /api/parts/{klm_id}/symbol.svg       rendered by klm, not kicad-cli
GET    /api/parts/{klm_id}/footprint.svg
GET    /api/lint?select=

GET    /api/projects?path=                  mode, BOM, sync status
GET    /api/projects/diff?path=&klm_id=     both sides, see §4
GET    /api/projects/verify?path=           clean-room; takes no catalog
POST   /api/projects/vendor | /unvendor     → job

POST   /api/orders/plan                     demand → split, with the reasoning
GET    /api/orders | /api/orders/{id}
POST   /api/orders/{id}/receive
GET    /api/stock?location=
POST   /api/generate                        → job

GET    /api/jobs | /api/jobs/{id}
GET    /api/jobs/{id}/events                SSE; the backlog is replayed first
```

Long operations are jobs with an SSE event stream, because vendoring, asset acquisition and agent
research take tens of seconds to minutes and a progress-less UI for that is unusable. A watcher
that arrives late gets the whole log, and a job's terminal state is assigned *last* — so "state is
terminal" means "everything is recorded", including the traceback.

## 4. Desktop app

**pywebview shell + web UI, in one process.** The window wraps the platform's own webview
(WebView2 / WebKit / WebKitGTK) around the local UI, which talks to the FastAPI app. See
[ADR-0012](adr/0012-pywebview-shell.md), which supersedes [ADR-0005](adr/0005-desktop-shell.md)
and records why Tauri was dropped — and [ADR-0005] for the alternatives weighed before either
(PySide6, Electron, browser-only).

### Screens

| Screen | Built | Purpose |
|---|---|---|
| **Catalog** | ✓ | Searchable, filterable part table. Filters on status and free text; columns and stock/supplier filters are not there yet. The screen you live in. |
| **Part detail** | ✓ | One part: fields, symbol and footprint previews, offers, stock, approve/deprecate. Parameters with their sources and where-used are not there yet. |
| **Add part** | ✓ | One form, then a job log. The steps the pipeline runs — identify → offers → assets → QA — are things klm *does*, not things it asks about, so the only screen with a question on it is the form. |
| **Research** | — | Phase 9. Requirement builder, live agent stream, ranked candidates, approve/edit/reject. |
| **Projects** | ✓ | Open a project: mode, BOM, sync status, vendor, clean-room verify. A registered-project list is not there yet. |
| **Sync detail** | ✓ | Per-part drift, both sides, as a coloured patch. Conflict *resolution* is still CLI-only. |
| **Order** | ✓ | Build plan → demand → supplier split, with the reasoning per line. Pinning and cart export are CLI-only. |
| **Inventory** | ✓ | Stock by location. Count-entry mode and label printing are CLI-only. |
| **Fab** | — | Preflight checklist, package generation, feedback entry after a run. |
| **Health** | ✓ | External tools and what their absence disables, catalog counts. Stale offers, QA failures and duplicate candidates are not there yet. |

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

- **Symbol and footprint** — built, in `klm.kicad.render`. The S-expression is rendered to SVG
  directly rather than through `kicad-cli`, for the same reason `klm bom` reads `.kicad_sch`
  itself: a capability that needs an external tool is a capability that disappears, and the
  machine with no KiCad is exactly the one reviewing a part it has never seen. Reachable from the
  CLI as `klm render` and from the API as `/api/parts/{id}/symbol.svg`.

  It is a **preview, not a plot**: it draws the geometry that carries meaning — outlines, pads,
  pins — and not KiCad's full graphical vocabulary. A pad in the wrong place is visible here; a
  label two millimetres off is not a defect this drawing exists to find. Unknown shapes are
  skipped rather than approximated, so a future KiCad primitive reads as "something is missing"
  instead of "this is what you get". The QA gate remains the thing that *checks*.

- **3D** — not built. A STEP file is a boundary representation, so a picture means tessellating
  it, and a wrong picture of a 3D model is the failure this project refuses everywhere else. The
  QA gate's bounding-box check catches the real problems without one; KiCad's own 3D viewer is
  there for the rest.

### The sync diff, and the comparison it refuses to make

The obvious screen — the catalog's asset beside the vendored one — is the wrong screen. The
vendored symbol was renamed and re-fielded on the way in and its footprint points inside the
project, so the two differ permanently and by design (docs/06 §5). A diff of them never empties,
and a diff that never empties teaches its reader to ignore it.

`klm sync diff` shows **two comparisons, each against its own recorded state**: what moved in the
catalog since this project vendored it, and what moved in this project since klm wrote it. That
is the same pair `sync status` decides `clean` / `global-ahead` / `project-ahead` / `conflict`
from, spelled out to the line.

The catalog side is easy — the store is content-addressed, so the asset as-recorded is still
there. The project side is not: klm keeps the vendored copy's *hash*, never its bytes. So the
"before" is rebuilt through `build_library`, the same function that wrote it, from the catalog
assets the lock recorded — and then checked against the recorded hash. If the rebuild does not
reproduce that hash it is a guess, and it is reported as unavailable rather than shown. A guessed
diff invites someone to resolve a conflict that is not there.

## 5. Packaging

| Target | Approach |
|---|---|
| All three | `pip install 'klm[app]'`, then `klm app`. One wheel; the webview is the platform's own |
| Linux | Needs WebKitGTK (`gir1.2-webkit2-4.1`). Absent, `klm app` degrades to `klm serve` and says why |
| CLI alone | `pipx install klm` — the core has no runtime dependencies at all |

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
