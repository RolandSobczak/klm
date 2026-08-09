# 03 — Architecture

## 1. Shape of the system

```
┌───────────────────────────────────────────────────────────────────┐
│  Interfaces                                                       │
│   klm CLI          Desktop app (Tauri shell)      pre-commit hook │
└──────────┬────────────────────┬───────────────────────┬───────────┘
           │                    │ HTTP (localhost)      │
           │                    ▼                       │
           │        ┌───────────────────────┐           │
           └───────▶│  klm.api (FastAPI)    │◀──────────┘
                    └───────────┬───────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│  Services — the only place business rules live                    │
│   catalog    sync    sourcing    assets    fab    ordering   agent │
└──────────┬─────────────┬────────────┬───────────┬────────┬────────┘
           ▼             ▼            ▼           ▼        ▼
┌──────────────┐ ┌─────────────┐ ┌─────────┐ ┌────────┐ ┌──────────┐
│ store        │ │ kicad       │ │suppliers│ │ cad    │ │ llm      │
│ SQLite +     │ │ sexpr rw,   │ │ TME,    │ │FreeCAD,│ │ Anthropic│
│ file layout  │ │ lib tables  │ │ LCSC    │ │kicad-  │ │ SDK      │
│              │ │             │ │         │ │cli     │ │          │
└──────────────┘ └─────────────┘ └─────────┘ └────────┘ └──────────┘
```

**The core is a plain Python library.** The CLI, the HTTP API and the hook are three thin
adapters over the same service functions. No business rule may live in an interface layer —
if the GUI can do something the CLI can't, that's a bug.

## 2. Layer responsibilities

| Layer | Owns | Must not |
|---|---|---|
| `klm.store` | SQLite access, file layout, content hashing, transactions | Know what a footprint *is* |
| `klm.kicad` | Parsing and writing KiCad files, lib tables, `kicad_common.json` | Talk to suppliers or the network |
| `klm.suppliers` | HTTP to TME/LCSC, rate limiting, response caching, offer normalization | Write to the catalog |
| `klm.cad` | Subprocess wrappers for `freecadcmd` and `kicad-cli` | Contain policy about *when* to convert |
| `klm.llm` | Anthropic API calls, tool definitions, structured outputs | Mutate anything — it returns proposals |
| `klm.services.*` | All policy and orchestration | Do raw I/O directly |
| `klm.cli` / `klm.api` | Argument parsing, formatting, HTTP | Anything else |

## 3. The lossless round-trip rule

This is the most important engineering constraint in the project, because klm edits files that
represent hours of a user's work.

**Rule:** parse a KiCad file into a generic S-expression tree, mutate only the specific nodes you
understand, and re-serialize the whole tree preserving every node you didn't touch — including
nodes from a newer KiCad version you've never seen.

```python
tree = sexpr.load(path)                     # generic nodes, nothing typed away
for node in tree.find_all("model"):         # only the nodes we understand
    node[0] = rewrite_model_path(node[0])
sexpr.dump(tree, path)                      # everything else byte-identical
```

Consequences, all deliberate:

- klm survives a KiCad format bump without corrupting files. Unknown nodes pass through.
- Diffs are minimal. Editing one footprint's model path changes one line.
- klm never "normalizes" a user's hand-edited file as a side effect of an unrelated operation.

The one place this is relaxed is **generated** files (`KLM.kicad_sym`, vendored libraries), which
klm owns entirely and writes with a canonical formatter — sorted fields, fixed float precision,
stable ordering — so that regenerating without changes produces a zero-byte diff.

An off-the-shelf typed KiCad parser is tempting but couples klm to one format version and
discards unknown nodes. See [ADR-0002](adr/0002-lossless-sexpr-round-trip.md).

## 4. Storage layout

```
~/.local/share/klm/                 # or $KLM_HOME
├── catalog.db                      # SQLite — operational store
├── catalog/                        # git-versioned export, one file per part
│   └── 01JB4K7Q…/part.yaml
├── assets/
│   ├── symbols/<hash>.sexp
│   ├── footprints/<hash>.kicad_mod
│   └── models3d/<hash>.step        # content-addressed → automatic dedup
├── generated/                      # regenerable; git-ignored
│   ├── KLM.kicad_sym
│   ├── KLM.pretty/
│   └── packages3d/
├── cache/
│   ├── suppliers/                  # HTTP response cache, TTL'd
│   └── datasheets/<sha256>.pdf
└── config.toml
```

- `catalog.db` is the working store — indexed, queryable, transactional.
- `catalog/*.yaml` is the git-friendly mirror, exported deterministically. `klm export` and
  `klm import` round-trip between them. This is what makes the catalog backup-able and
  diff-able without making every query a file scan. See [ADR-0001](adr/0001-sqlite-as-source-of-truth.md).
- `generated/` is a build artifact. It can always be deleted and rebuilt from the catalog.
  It is what KiCad's global library tables point at.

Content-addressed assets mean a footprint fix is a new hash and an explicit part-by-part
migration, not a silent mutation of everything referencing it.

## 5. Processes and external tools

| Tool | Invoked how | Used for |
|---|---|---|
| `kicad-cli` | subprocess | Gerber/drill/pos/BOM export, DRC, ERC |
| `freecadcmd` | subprocess, running a script under FreeCAD's own interpreter | Mesh → solid STEP conversion |
| `easyeda2kicad` / equivalent | subprocess or in-process | EasyEDA → KiCad symbol/footprint/model |
| Anthropic API | HTTPS | Research agent, datasheet extraction |

Two rules for all of them:

1. **Version-check at startup.** Record the discovered version of every external tool; refuse to
   run operations whose output depends on a version klm hasn't been tested against, with an
   override flag.
2. **The FreeCAD script is a separate world.** `cad/scripts/obj2step.py` runs under FreeCAD's
   Python, not klm's venv. It can `import Mesh, Part` but not `import klm`. It takes argv,
   writes a file, exits with a status code. Nothing else crosses that boundary.

## 6. Data flow: adding a part

```
requirement ──▶ agent ──▶ candidate MPNs
                            │
                    ┌───────┴────────┐
                    ▼                ▼
              TME search        LCSC search        ← suppliers layer
                    └───────┬────────┘
                            ▼
                     datasheet fetch + parameter extraction
                            ▼
                     DRAFT part (parameters with provenance)
                            ▼
                     asset acquisition ──▶ QA gate ──▶ fail ──▶ report, stay draft
                            ▼ pass
                     human review (diff of everything)
                            ▼ approve
                     catalog write + regenerate KLM.kicad_sym
```

Note where the human sits: after everything automatic, before anything durable. The agent never
writes to the catalog ([11 §5](11-ai-research-agent.md)).

## 7. Data flow: build to fabrication

```
project.kicad_sch ──parse──▶ BOM lines ──resolve klm_id──▶ Parts
       │                                                     │
       ▼                                                     ▼
project.kicad_pcb ──kicad-cli──▶ gerbers, drill, raw CPL   offers
       │                              │                      │
       │                    rotation corrections             │
       │                              ▼                      ▼
       └──────────────────────▶ fab package: gerbers.zip, bom.csv, cpl.csv
```

## 8. Technology choices, briefly

- **Python 3.11+.** The whole surrounding ecosystem is Python: KiCad's own scripting,
  `kicad-cli`'s companions, FreeCAD, `easyeda2kicad`. Any other language means shelling out
  to Python anyway.
- **SQLite** for the store. Single file, no server, transactional, fast enough at this scale by
  three orders of magnitude.
- **FastAPI** for the local API, because the desktop shell needs one and it doubles as a
  scripting surface.
- **Tauri** for the desktop shell — small binary, web UI, no bundled Chromium. The shell is
  deliberately thin so it can be replaced (with PySide6, or with nothing) without touching logic.
  See [ADR-0005](adr/0005-desktop-shell.md).

## 9. Concurrency and safety

Single user, but not single process — the GUI, a CLI invocation and a KiCad instance can all be
live at once.

- Writes to `catalog.db` go through SQLite in WAL mode with a short busy timeout.
- Long operations (asset acquisition, supplier refresh) run in a worker with progress reported
  over the API; they take a named advisory lock so two refreshes can't interleave.
- Any operation that writes **outside** the klm directory — KiCad's global config, a project's
  files — first takes a backup copy next to the original (`file.kicad_sch.klm-bak`) and reports
  what it will change before doing it. `--dry-run` is supported everywhere and is the default
  for destructive operations in the GUI.
- KiCad's `sym-lib-table`, `fp-lib-table` and `kicad_common.json` are read-modify-write with
  merge semantics: klm adds and updates only rows it owns (marked with a `KLM` prefix or an
  adjacent registry) and never rewrites the file wholesale.
